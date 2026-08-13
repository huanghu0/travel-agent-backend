import json
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from app.evaluation.fault_reporting import (
    build_fault_suite_report,
    write_fault_report_json,
    write_fault_report_junit,
)
from app.evaluation.orchestrator_faults import (
    RECOVERABLE_ORCHESTRATOR_FAULT_CASES,
    TERMINAL_ORCHESTRATOR_FAULT_CASES,
    run_orchestrator_fault_case,
)


class OrchestratorFaultReportingTests(unittest.TestCase):
    """验证结构化报告可供本地审计和 CI 稳定消费。"""

    @classmethod
    def setUpClass(cls):
        cases = (
            RECOVERABLE_ORCHESTRATOR_FAULT_CASES
            + TERMINAL_ORCHESTRATOR_FAULT_CASES
        )
        cls.report = build_fault_suite_report(
            [run_orchestrator_fault_case(case) for case in cases]
        )

    def test_suite_report_contains_recovery_and_terminal_rates(self):
        self.assertEqual(self.report.total_case_count, 14)
        self.assertEqual(self.report.recovery_case_count, 7)
        self.assertEqual(self.report.terminal_case_count, 7)
        self.assertEqual(self.report.passed_case_count, 14)
        self.assertEqual(self.report.failed_case_count, 0)
        self.assertEqual(self.report.recovery_pass_rate, 1.0)
        self.assertEqual(self.report.terminal_pass_rate, 1.0)
        self.assertEqual(self.report.overall_pass_rate, 1.0)

    def test_case_report_contains_budget_fault_and_termination_details(self):
        route_case = next(
            case
            for case in self.report.cases
            if case.case_id == "route-segments-unavailable-continuous"
        )
        self.assertEqual(route_case.actual_outcome, "failed_safely")
        self.assertEqual(
            route_case.termination_code,
            "route_unavailable_after_recovery",
        )
        self.assertIn("route.unavailable", route_case.issue_codes)
        self.assertGreater(route_case.fault_count, 0)
        self.assertFalse(route_case.budget_exceeded)
        self.assertLessEqual(route_case.physical_steps, route_case.max_physical_steps)
        self.assertLessEqual(route_case.tool_calls, route_case.max_tool_calls)
        self.assertLessEqual(route_case.llm_calls, route_case.max_llm_calls)
        self.assertEqual(route_case.failed_check_codes, [])

    def test_json_and_junit_reports_are_written_and_parseable(self):
        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "faults.json"
            junit_path = Path(directory) / "faults.junit.xml"
            write_fault_report_json(self.report, json_path)
            write_fault_report_junit(self.report, junit_path)

            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["suite_name"], self.report.suite_name)
            self.assertEqual(payload["total_case_count"], 14)
            self.assertEqual(len(payload["cases"]), 14)

            suite = ET.parse(junit_path).getroot()
            self.assertEqual(suite.tag, "testsuite")
            self.assertEqual(suite.attrib["tests"], "14")
            self.assertEqual(suite.attrib["failures"], "0")
            self.assertEqual(len(suite.findall("testcase")), 14)


if __name__ == "__main__":
    unittest.main()
