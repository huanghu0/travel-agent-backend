import json
import unittest

from app.agents.planner_agent import PlannerAgent
from app.schemas.trip_schema import TripPlan, TripRequest
from app.validation import TripValidationResult, ValidationIssue, ValidationSeverity


class RecordingLLM:
    def __init__(self):
        self.response_model = None
        self.instructions = None
        self.input_text = None

    def invoke(self, instructions, input_text, response_model=None):
        self.instructions = instructions
        self.input_text = input_text
        self.response_model = response_model
        return json.dumps(
            {
                "city": "test-city",
                "start_date": "2026-08-10",
                "end_date": "2026-08-10",
                "days": [],
                "weather_info": [],
                "overall_suggestions": "test-tip",
                "budget": None,
            }
        )


class PlannerStructuredOutputTests(unittest.TestCase):
    def test_planner_requests_trip_plan_response_model(self):
        llm = RecordingLLM()
        planner = PlannerAgent.__new__(PlannerAgent)
        planner.prompt = "planner-system-prompt"
        planner.llm = llm
        request = TripRequest(
            city="test-city",
            start_date="2026-08-10",
            end_date="2026-08-10",
            travel_days=1,
            transportation="public-transit",
            accommodation="hotel",
            preferences=[],
        )

        result = planner.generate_plan(request, {}, {}, {})

        self.assertIs(llm.response_model, TripPlan)
        self.assertEqual(result["city"], "test-city")
        self.assertIn("overall_suggestions", result)
        self.assertIn("days", result)

    def test_repair_plan_sends_structured_validation_and_requests_trip_plan(self):
        llm = RecordingLLM()
        planner = PlannerAgent.__new__(PlannerAgent)
        planner.prompt = "planner-system-prompt"
        planner.llm = llm
        request = TripRequest(
            city="test-city",
            start_date="2026-08-10",
            end_date="2026-08-10",
            travel_days=1,
            transportation="public-transit",
            accommodation="hotel",
            preferences=[],
        )
        current_plan = TripPlan.model_validate(json.loads(llm.invoke("", "")))
        validation = TripValidationResult.from_issues([
            ValidationIssue(
                code="days.count_mismatch",
                severity=ValidationSeverity.ERROR,
                path="days",
                message="missing day",
                repair_hint="add one day",
            )
        ])

        result = planner.repair_plan(
            request,
            current_plan,
            validation,
            {},
            {},
            {},
        )

        self.assertIs(llm.response_model, TripPlan)
        self.assertIn("days.count_mismatch", llm.input_text)
        self.assertEqual(result["city"], "test-city")


if __name__ == "__main__":
    unittest.main()
