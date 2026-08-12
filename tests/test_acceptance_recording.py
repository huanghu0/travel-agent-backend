import json
import tempfile
import unittest
from pathlib import Path

from app.evaluation import (
    FIXED_ACCEPTANCE_SCENARIOS,
    create_acceptance_recording,
    load_acceptance_recording,
    load_acceptance_recording_suite,
    sanitize_agent_state,
    write_acceptance_recording_suite,
)
from app.evaluation.sample_factory import build_synthetic_acceptance_state
from app.tools.models import ActionResult


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "fixed_acceptance" / "v1"


class AcceptanceRecordingTests(unittest.TestCase):
    def test_checked_in_suite_is_complete_and_replayable(self):
        states = load_acceptance_recording_suite(
            FIXTURE_DIR,
            FIXED_ACCEPTANCE_SCENARIOS,
            require_manifest=True,
            allowed_sources={"synthetic"},
            allow_legacy=False,
        )

        self.assertEqual(len(states), 15)
        self.assertEqual(
            {state.request.city for state in states},
            {"杭州", "北京", "上海", "成都", "西安"},
        )
        self.assertTrue(all(state.status == "completed" for state in states))

    def test_recursive_redaction_removes_keys_bearer_tokens_and_query_keys(self):
        state = build_synthetic_acceptance_state(FIXED_ACCEPTANCE_SCENARIOS[0])
        state.errors = [
            "Authorization: Bearer top-secret-token",
            "https://example.test/place?key=secret-value&city=杭州",
            "sdk token sk-abcdefghijklmnopqrstuvwxyz123456",
        ]
        state.last_action_result = ActionResult(
            tool_name="test",
            success=False,
            data={
                "api_key": "plain-secret",
                "token": "plain-token",
                "completion_tokens": 42,
                "safe": "visible",
            },
            error="Bearer another-secret-token",
        )

        sanitized, paths = sanitize_agent_state(state)
        rendered = sanitized.model_dump_json()

        self.assertNotIn("top-secret-token", rendered)
        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123456", rendered)
        self.assertNotIn("plain-secret", rendered)
        self.assertNotIn("plain-token", rendered)
        self.assertEqual(sanitized.last_action_result.data["completion_tokens"], 42)
        self.assertIn("[REDACTED]", rendered)
        self.assertIn("state.last_action_result.data.api_key", paths)

    def test_synthetic_state_factory_is_deterministic(self):
        scenario = FIXED_ACCEPTANCE_SCENARIOS[0]

        first = build_synthetic_acceptance_state(scenario)
        second = build_synthetic_acceptance_state(scenario)

        self.assertEqual(
            first.model_dump(mode="json"),
            second.model_dump(mode="json"),
        )

    def test_checksum_tampering_is_rejected(self):
        scenario = FIXED_ACCEPTANCE_SCENARIOS[0]
        recording = create_acceptance_recording(
            scenario,
            build_synthetic_acceptance_state(scenario),
            source="synthetic",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            write_acceptance_recording_suite(directory, [recording])
            path = directory / f"{scenario.case_id}.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["state"]["current_step"] = 23
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                load_acceptance_recording(path, scenario=scenario)

    def test_manifest_source_policy_rejects_synthetic_when_live_is_required(self):
        with self.assertRaisesRegex(ValueError, "source is not allowed"):
            load_acceptance_recording_suite(
                FIXTURE_DIR,
                FIXED_ACCEPTANCE_SCENARIOS,
                require_manifest=True,
                allowed_sources={"live"},
                allow_legacy=False,
            )

    def test_manifest_is_authoritative_and_ignores_unlisted_files(self):
        first, second = FIXED_ACCEPTANCE_SCENARIOS[:2]
        first_recording = create_acceptance_recording(
            first, build_synthetic_acceptance_state(first), source="synthetic"
        )
        second_recording = create_acceptance_recording(
            second, build_synthetic_acceptance_state(second), source="synthetic"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            write_acceptance_recording_suite(directory, [first_recording])
            (directory / f"{second.case_id}.json").write_text(
                second_recording.model_dump_json(indent=2), encoding="utf-8"
            )

            states = load_acceptance_recording_suite(
                directory,
                [first, second],
                require_manifest=True,
                allowed_sources={"synthetic"},
                allow_legacy=False,
            )

        self.assertEqual(len(states), 1)
        self.assertEqual(states[0].request.city, first.request.city)

    def test_manifest_rejects_recording_path_outside_suite_directory(self):
        scenario = FIXED_ACCEPTANCE_SCENARIOS[0]
        recording = create_acceptance_recording(
            scenario, build_synthetic_acceptance_state(scenario), source="synthetic"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "suite"
            manifest = write_acceptance_recording_suite(directory, [recording])
            manifest.records[0].file_name = "../outside.json"
            (directory / "manifest.json").write_text(
                manifest.model_dump_json(indent=2), encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "escapes suite directory"):
                load_acceptance_recording_suite(
                    directory,
                    [scenario],
                    require_manifest=True,
                    allowed_sources={"synthetic"},
                    allow_legacy=False,
                )


if __name__ == "__main__":
    unittest.main()
