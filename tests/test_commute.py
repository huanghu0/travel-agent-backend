import unittest

from app.agent_runtime import AgentAction, AgentState, TripOrchestrator
from app.commute import (
    CommuteCandidatePoolSupplementer,
    CommuteConstraintEvaluator,
    CommuteConstraintReport,
    CommuteSegmentIssue,
    RemoteAttractionReplacementOptimizer,
)
from app.constraints import ConstraintEvaluator, constraint_plan_fingerprint
from app.providers.amap.errors import AmapErrorKind, AmapProviderError
from app.providers.amap.models import (
    AttractionCandidate,
    AttractionSearchResult,
    NearbyAttractionSearchResult,
    RouteEstimate,
    RouteEstimateResult,
)
from app.routing import evaluate_route_quality, plan_route_fingerprint
from app.scheduling import ScheduleTimelineEvaluator
from app.tools import build_trip_tool_registry
from app.tools.models import ActionResult
from app.tools.registry import ToolRegistry
from app.schemas.trip_schema import Attraction, DayPlan, Hotel, TripPlan, TripRequest


def make_request(transportation="公共交通"):
    return TripRequest(
        city="杭州",
        start_date="2026-08-10",
        end_date="2026-08-10",
        travel_days=1,
        transportation=transportation,
        accommodation="经济型酒店",
        preferences=["自然风光"],
    )


def attraction(name, longitude, *, poi_id="", category="自然风光"):
    return Attraction(
        name=name,
        address=f"{name}地址",
        location={"longitude": longitude, "latitude": 30.25},
        visit_duration=120,
        description=name,
        category=category,
        poi_id=poi_id,
    )


def make_plan(items):
    return TripPlan(
        city="杭州",
        start_date="2026-08-10",
        end_date="2026-08-10",
        days=[
            DayPlan(
                date="2026-08-10",
                day_index=0,
                description="测试",
                transportation="公共交通",
                accommodation="经济型酒店",
                hotel=Hotel(
                    name="蜂窝酒店",
                    address="酒店地址",
                    location={"longitude": 120.16, "latitude": 30.25},
                ),
                attractions=items,
            )
        ],
        overall_suggestions="测试",
    )


def route_result(request, plan, *, duration=8580, distance=63800):
    return RouteEstimateResult(
        plan_fingerprint=plan_route_fingerprint(request, plan),
        requested_legs=2,
        evaluated_legs=2,
        routes=[
            RouteEstimate(
                day_index=0,
                leg_index=0,
                leg_type="hotel_departure",
                origin_name="蜂窝酒店",
                destination_name=plan.days[0].attractions[0].name,
                mode="transit",
                duration_seconds=duration,
                distance_meters=distance,
            ),
            RouteEstimate(
                day_index=0,
                leg_index=0,
                leg_type="hotel_return",
                origin_name=plan.days[0].attractions[-1].name,
                destination_name="蜂窝酒店",
                mode="transit",
                duration_seconds=duration,
                distance_meters=distance,
            ),
        ],
    )


class CommuteConstraintEvaluatorTests(unittest.TestCase):
    def test_transit_leg_over_ninety_minutes_is_reported(self):
        request = make_request()
        plan = make_plan([attraction("桐洲岛", 120.75, poi_id="far")])
        report = CommuteConstraintEvaluator().evaluate(
            request,
            plan,
            route_result(request, plan),
        )

        self.assertEqual(report.excessive_segment_count, 2)
        self.assertTrue(report.optimization_recommended)
        self.assertEqual(report.issues[0].target_attraction_name, "桐洲岛")
        self.assertEqual(report.issues[0].limit_seconds, 90 * 60)

    def test_mode_specific_limit_is_used(self):
        request = make_request("步行")
        plan = make_plan([attraction("西湖", 120.17)])
        routes = route_result(request, plan, duration=46 * 60, distance=3000)
        routes.routes[0].mode = "walking"
        routes.routes[1].mode = "walking"

        report = CommuteConstraintEvaluator().evaluate(request, plan, routes)

        self.assertEqual(report.excessive_segment_count, 2)
        self.assertEqual(report.issues[0].limit_seconds, 45 * 60)


class RemoteAttractionReplacementOptimizerTests(unittest.TestCase):
    def test_replaces_remote_attraction_with_nearest_unused_candidate(self):
        request = make_request()
        plan = make_plan([attraction("桐洲岛", 120.75, poi_id="far")])
        report = CommuteConstraintEvaluator().evaluate(
            request,
            plan,
            route_result(request, plan),
        )
        payload = {
            "candidates": [
                {
                    "poi_id": "far",
                    "name": "桐洲岛",
                    "address": "远",
                    "location": {"longitude": 120.75, "latitude": 30.25},
                    "category": "自然风光",
                },
                {
                    "poi_id": "medium",
                    "name": "较远公园",
                    "address": "中",
                    "location": {"longitude": 120.30, "latitude": 30.25},
                    "category": "自然风光",
                },
                {
                    "poi_id": "near",
                    "name": "西湖公园",
                    "address": "近",
                    "location": {"longitude": 120.17, "latitude": 30.25},
                    "category": "自然风光",
                    "rating": 4.8,
                },
            ]
        }
        baseline = plan.model_dump(mode="json")

        candidate = RemoteAttractionReplacementOptimizer().optimize(
            request,
            plan,
            report,
            attractions=payload,
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.replaced_attraction_name, "桐洲岛")
        self.assertEqual(candidate.replacement_attraction_name, "西湖公园")
        self.assertEqual(len(candidate.plan.days[0].attractions), 1)
        self.assertEqual(plan.model_dump(mode="json"), baseline)

    def test_excluded_candidate_is_not_reused(self):
        request = make_request()
        plan = make_plan([attraction("桐洲岛", 120.75, poi_id="far")])
        report = CommuteConstraintEvaluator().evaluate(
            request,
            plan,
            route_result(request, plan),
        )
        payload = {
            "candidates": [
                {
                    "poi_id": "near",
                    "name": "西湖公园",
                    "address": "近",
                    "location": {"longitude": 120.17, "latitude": 30.25},
                    "category": "自然风光",
                }
            ]
        }

        candidate = RemoteAttractionReplacementOptimizer().optimize(
            request,
            plan,
            report,
            attractions=payload,
            excluded_candidate_identities={"id:near"},
        )

        self.assertIsNone(candidate)


class CommuteCandidatePoolSupplementerTests(unittest.TestCase):
    @staticmethod
    def report_for(plan, target_index):
        target = plan.days[0].attractions[target_index]
        issue = CommuteSegmentIssue(
            day_index=0,
            leg_index=max(0, target_index - 1),
            leg_type="between_attractions" if len(plan.days[0].attractions) > 1 else "hotel_departure",
            origin_name="Anchor A",
            destination_name=target.name,
            mode="transit",
            duration_seconds=6000,
            distance_meters=30000,
            limit_seconds=5400,
            excess_seconds=600,
            target_attraction_name=target.name,
            target_attraction_index=target_index,
        )
        return CommuteConstraintReport(
            plan_fingerprint="fingerprint",
            total_segments=1,
            excessive_segment_count=1,
            max_duration_seconds=6000,
            total_excess_seconds=600,
            optimization_recommended=True,
            issues=[issue],
        )

    def test_single_attraction_query_uses_hotel_as_both_anchors(self):
        plan = make_plan([attraction("Remote Island", 120.75, poi_id="far")])
        supplementer = CommuteCandidatePoolSupplementer()

        query = supplementer.build_query(
            make_request(),
            plan,
            self.report_for(plan, 0),
            search_index=0,
        )

        self.assertIsNotNone(query)
        self.assertAlmostEqual(query.center.longitude, 120.16)
        self.assertAlmostEqual(query.center.latitude, 30.25)
        self.assertEqual(len(query.anchor_names), 2)
        self.assertEqual(len(set(query.anchor_names)), 1)
        self.assertEqual(query.radius_meters, 5000)

    def test_middle_attraction_query_uses_neighbor_midpoint(self):
        plan = make_plan(
            [
                attraction("Previous", 120.10, poi_id="previous"),
                attraction("Remote", 120.80, poi_id="remote"),
                attraction("Following", 120.20, poi_id="following"),
            ]
        )
        supplementer = CommuteCandidatePoolSupplementer()

        query = supplementer.build_query(
            make_request(),
            plan,
            self.report_for(plan, 1),
            search_index=0,
        )

        self.assertIsNotNone(query)
        self.assertAlmostEqual(query.center.longitude, 120.15)
        self.assertAlmostEqual(query.center.latitude, 30.25)
        self.assertEqual(query.anchor_names, ["Previous", "Following"])

    def test_search_radius_doubles_until_configured_cap(self):
        plan = make_plan([attraction("Remote", 120.75, poi_id="far")])
        report = self.report_for(plan, 0)
        supplementer = CommuteCandidatePoolSupplementer(
            initial_radius_meters=5000,
            max_radius_meters=20000,
        )

        radii = [
            supplementer.build_query(
                make_request(), plan, report, search_index=index
            ).radius_meters
            for index in range(4)
        ]

        self.assertEqual(radii, [5000, 10000, 20000, 20000])

    def test_merge_deduplicates_replaces_better_rating_and_crops_without_mutation(self):
        existing = {
            "provider": "amap",
            "query_city": "Hangzhou",
            "keywords": "nature",
            "total_received": 2,
            "candidates": [
                {
                    "poi_id": "duplicate",
                    "name": "Duplicate",
                    "address": "Old address",
                    "location": {"longitude": 120.16, "latitude": 30.25},
                    "rating": 4.0,
                },
                {
                    "poi_id": "old",
                    "name": "Old",
                    "address": "Old",
                    "location": {"longitude": 120.17, "latitude": 30.25},
                },
            ],
        }
        original = {
            **existing,
            "candidates": [dict(item) for item in existing["candidates"]],
        }
        incoming = AttractionSearchResult(
            query_city="Hangzhou",
            keywords="nature",
            total_received=3,
            candidates=[
                AttractionCandidate(
                    poi_id="duplicate",
                    name="Duplicate",
                    address="Better address",
                    location={"longitude": 120.16, "latitude": 30.25},
                    rating=4.9,
                ),
                AttractionCandidate(
                    poi_id="new",
                    name="New",
                    address="New",
                    location={"longitude": 120.18, "latitude": 30.25},
                ),
                AttractionCandidate(
                    poi_id="cropped",
                    name="Cropped",
                    address="Cropped",
                    location={"longitude": 120.19, "latitude": 30.25},
                ),
            ],
        )

        result = CommuteCandidatePoolSupplementer(
            pool_max_candidates=3
        ).merge(existing, incoming)

        self.assertEqual(existing, original)
        self.assertEqual(result.received_candidates, 3)
        self.assertEqual(result.duplicate_candidates, 1)
        self.assertEqual(result.added_candidates, 1)
        self.assertEqual(result.final_candidates, 3)
        self.assertEqual(
            [item["poi_id"] for item in result.pool["candidates"]],
            ["duplicate", "old", "new"],
        )
        self.assertEqual(result.pool["candidates"][0]["rating"], 4.9)


class NearbyProvider:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    @staticmethod
    def search_attractions(*, city, keywords):
        return AttractionSearchResult(query_city=city, keywords=keywords)

    @staticmethod
    def search_hotels(*, city, keywords):
        from app.providers.amap.models import HotelSearchResult

        return HotelSearchResult(query_city=city, keywords=keywords)

    @staticmethod
    def get_weather(city):
        from app.providers.amap.models import WeatherSearchResult

        return WeatherSearchResult(query_city=city)

    def search_nearby_attractions(
        self,
        *,
        city,
        keywords,
        center,
        radius_meters,
        page,
        page_size,
    ):
        self.calls.append(radius_meters)
        if self.error is not None:
            raise self.error
        candidates = self.responses.pop(0) if self.responses else []
        return NearbyAttractionSearchResult(
            query_city=city,
            keywords=keywords,
            total_received=len(candidates),
            candidates=candidates,
            center=center,
            radius_meters=radius_meters,
            page=page,
            page_size=page_size,
        )


class CommuteOrchestratorTests(unittest.TestCase):
    def make_state(self):
        request = make_request()
        plan = make_plan([attraction("Remote Island", 120.75, poi_id="far")])
        routes = route_result(request, plan)
        schedule = ScheduleTimelineEvaluator().evaluate(request, plan, routes)
        state = AgentState.create(request, max_commute_replacement_attempts=2)
        state.attractions = {
            "candidates": [
                {
                    "poi_id": "near",
                    "name": "West Lake Park",
                    "address": "Near hotel",
                    "location": {"longitude": 120.17, "latitude": 30.25},
                    "category": "nature",
                    "rating": 4.8,
                }
            ]
        }
        state.weather = {"forecasts": []}
        state.hotels = {"candidates": []}
        state.trip_plan = plan
        state.route_estimates = routes.model_dump(mode="json")
        state.route_plan_fingerprint = routes.plan_fingerprint
        state.route_quality_report = evaluate_route_quality(plan, routes)
        state.route_quality_plan_fingerprint = routes.plan_fingerprint
        state.route_optimization_status = "skipped"
        state.commute_report = CommuteConstraintEvaluator().evaluate(
            request, plan, routes
        )
        state.commute_plan_fingerprint = routes.plan_fingerprint
        state.schedule_quality_report = schedule
        state.schedule_quality_plan_fingerprint = routes.plan_fingerprint
        state.constraint_report = ConstraintEvaluator().evaluate(
            request, plan, schedule, attractions=state.attractions, weather=state.weather
        )
        state.constraint_plan_fingerprint = constraint_plan_fingerprint(request, plan)
        return state

    def attach_candidate_reports(self, state, *, duration=600):
        routes = route_result(
            state.request,
            state.trip_plan,
            duration=duration,
            distance=3000 if duration < 5400 else 63800,
        )
        orchestrator = self.orchestrator
        orchestrator._apply_tool_result(
            state,
            AgentAction.ESTIMATE_ROUTES,
            ActionResult(tool_name="estimate_routes", success=True, data=routes),
        )
        orchestrator.execute_action(state, AgentAction.EVALUATE_COMMUTE)
        orchestrator.execute_action(state, AgentAction.EVALUATE_CONSTRAINTS)

    def setUp(self):
        self.orchestrator = TripOrchestrator(tool_registry=ToolRegistry())

    @staticmethod
    def orchestrator_with_provider(provider):
        registry = build_trip_tool_registry(
            planner_agent=object(),
            map_provider=provider,
        )
        return TripOrchestrator(tool_registry=registry)

    def test_missing_local_candidate_triggers_search_then_replacement(self):
        provider = NearbyProvider(
            responses=[
                [
                    AttractionCandidate(
                        poi_id="dynamic-near",
                        name="Dynamic Nearby Park",
                        address="Near hotel",
                        location={"longitude": 120.17, "latitude": 30.25},
                        category="nature",
                        rating=4.8,
                    )
                ]
            ]
        )
        self.orchestrator = self.orchestrator_with_provider(provider)
        state = self.make_state()
        state.attractions = {"candidates": []}

        self.orchestrator.execute_action(
            state, AgentAction.REPLACE_REMOTE_ATTRACTION
        )
        self.assertEqual(state.commute_optimization_status, "supplement_needed")
        self.assertIsNotNone(state.commute_supplement_query)

        self.orchestrator.execute_action(state, AgentAction.SUPPLEMENT_ATTRACTIONS)
        self.assertEqual(state.commute_supplement_search_count, 1)
        self.assertEqual(state.commute_supplement_history[-1].status, "completed")
        self.assertEqual(state.commute_optimization_status, "not_started")
        self.assertEqual(
            state.attractions["candidates"][0]["poi_id"],
            "dynamic-near",
        )

        self.orchestrator.execute_action(
            state, AgentAction.REPLACE_REMOTE_ATTRACTION
        )
        self.assertEqual(state.commute_optimization_status, "candidate_pending")
        self.assertEqual(
            state.trip_plan.days[0].attractions[0].name,
            "Dynamic Nearby Park",
        )

    def test_empty_supplement_expands_radius_on_next_attempt(self):
        provider = NearbyProvider(responses=[[]])
        self.orchestrator = self.orchestrator_with_provider(provider)
        state = self.make_state()
        state.attractions = {"candidates": []}

        self.orchestrator.execute_action(
            state, AgentAction.REPLACE_REMOTE_ATTRACTION
        )
        first_radius = state.commute_supplement_query.radius_meters
        self.orchestrator.execute_action(state, AgentAction.SUPPLEMENT_ATTRACTIONS)
        self.orchestrator.execute_action(
            state, AgentAction.REPLACE_REMOTE_ATTRACTION
        )

        self.assertEqual(first_radius, 5000)
        self.assertEqual(state.commute_supplement_search_count, 1)
        self.assertEqual(state.commute_supplement_history[-1].status, "empty")
        self.assertEqual(state.commute_supplement_query.radius_meters, 10000)

    def test_permanent_supplement_failure_keeps_plan_and_skips_optional_optimization(self):
        provider = NearbyProvider(
            error=AmapProviderError(
                "invalid nearby query",
                kind=AmapErrorKind.INVALID_INPUT,
                retryable=False,
            )
        )
        self.orchestrator = self.orchestrator_with_provider(provider)
        state = self.make_state()
        state.attractions = {"candidates": []}
        baseline = state.trip_plan.model_dump(mode="json")

        self.orchestrator.execute_action(
            state, AgentAction.REPLACE_REMOTE_ATTRACTION
        )
        self.orchestrator.execute_action(state, AgentAction.SUPPLEMENT_ATTRACTIONS)

        self.assertNotEqual(state.status, "failed")
        self.assertEqual(state.trip_plan.model_dump(mode="json"), baseline)
        self.assertEqual(state.commute_optimization_status, "skipped")
        self.assertEqual(state.commute_supplement_search_count, 1)
        self.assertEqual(state.commute_supplement_history[-1].status, "failed")
        self.assertIn("invalid nearby query", state.commute_supplement_history[-1].error)

    def test_candidate_is_accepted_after_real_route_schedule_and_constraint_checks(self):
        state = self.make_state()

        self.orchestrator.execute_action(
            state, AgentAction.REPLACE_REMOTE_ATTRACTION
        )
        self.assertEqual(state.commute_optimization_status, "candidate_pending")
        self.assertEqual(
            state.trip_plan.days[0].attractions[0].name,
            "West Lake Park",
        )

        self.attach_candidate_reports(state, duration=600)
        self.orchestrator.execute_action(
            state, AgentAction.REPLACE_REMOTE_ATTRACTION
        )

        self.assertEqual(state.commute_optimization_status, "completed")
        self.assertEqual(state.commute_replacement_history[-1].status, "accepted")
        self.assertEqual(state.commute_report.excessive_segment_count, 0)
        self.assertEqual(len(state.trip_plan.days[0].attractions), 1)

    def test_failed_candidate_is_reverted_and_excluded(self):
        state = self.make_state()
        baseline = state.trip_plan.model_dump(mode="json")

        self.orchestrator.execute_action(
            state, AgentAction.REPLACE_REMOTE_ATTRACTION
        )
        self.attach_candidate_reports(state, duration=8580)
        self.orchestrator.execute_action(
            state, AgentAction.REPLACE_REMOTE_ATTRACTION
        )

        self.assertEqual(state.trip_plan.model_dump(mode="json"), baseline)
        self.assertEqual(state.commute_replacement_history[-1].status, "reverted")
        self.assertIn("id:near", state.commute_excluded_candidate_identities)
        self.assertEqual(state.commute_optimization_status, "not_started")


if __name__ == "__main__":
    unittest.main()
