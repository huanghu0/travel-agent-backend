"""Full Orchestrator fault-recovery acceptance harness.

The harness uses the real deterministic runtime, execution policy, tool registry,
SQLite state store, validators and optimizers. Only Amap and planner boundaries
are replaced by deterministic in-memory implementations.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agent_runtime import AgentRuntimeError, AgentState, TripOrchestrator
from app.agent_runtime.checkpoint_policy import CheckpointPolicy
from app.evaluation.fault_injection import (
    FaultInjectingProxy,
    FaultInjector,
    FaultMode,
    FaultRule,
)
from app.memory import SQLiteAgentStateStore
from app.providers.amap.models import (
    AttractionCandidate,
    AttractionSearchResult,
    HotelSearchResult,
    RouteEstimate,
    RouteEstimateResult,
    WeatherSearchResult,
)
from app.schemas.trip_schema import TripRequest
from app.tools import build_trip_tool_registry


@dataclass(frozen=True)
class OrchestratorFaultCase:
    """One deterministic external-boundary failure and its expected outcome."""

    case_id: str
    description: str
    rules: tuple[FaultRule, ...]
    recoverable: bool = True
    include_route_legs: bool = False
    partial_route_failure: bool = False
    route_optimization_attempts: int = 0


@dataclass
class OrchestratorFaultResult:
    """Auditable outcome of a full Orchestrator execution."""

    case: OrchestratorFaultCase
    state: AgentState
    persisted_state: AgentState | None
    injector: FaultInjector
    exception: AgentRuntimeError | None = None
    resume_state: AgentState | None = None
    planner_generate_calls: int = 0
    provider_calls: dict[str, int] = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        return self.state.status == "completed" and self.state.finished

    @property
    def persisted(self) -> bool:
        return self.persisted_state is not None


class DeterministicAmapProvider:
    """Small, typed Amap substitute with stable POIs and route estimates."""

    def __init__(
        self,
        *,
        injector: FaultInjector | None = None,
        partial_route_failure: bool = False,
    ):
        self.calls: dict[str, int] = {}
        self.injector = injector
        self.partial_route_failure = partial_route_failure

    def _record(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    @staticmethod
    def attraction_candidates() -> list[AttractionCandidate]:
        return [
            AttractionCandidate(
                poi_id="hangzhou-west-lake",
                name="West Lake",
                address="West Lake Scenic Area, Hangzhou",
                location={"longitude": 120.148, "latitude": 30.244},
                category="scenic area",
                rating=4.9,
            ),
            AttractionCandidate(
                poi_id="hangzhou-grand-canal",
                name="Grand Canal",
                address="Gongshu District, Hangzhou",
                location={"longitude": 120.300, "latitude": 30.244},
                category="historic waterfront",
                rating=4.6,
            ),
            AttractionCandidate(
                poi_id="hangzhou-botanical-garden",
                name="Hangzhou Botanical Garden",
                address="Taoyuanling, Hangzhou",
                location={"longitude": 120.121, "latitude": 30.255},
                category="park",
                rating=4.7,
            ),
        ]

    def search_attractions(self, *, city: str, keywords: str) -> AttractionSearchResult:
        self._record("search_attractions")
        candidates = self.attraction_candidates()
        return AttractionSearchResult(
            query_city=city,
            keywords=keywords,
            total_received=len(candidates),
            candidates=candidates,
        )

    def get_weather(self, city: str) -> WeatherSearchResult:
        self._record("get_weather")
        return WeatherSearchResult(query_city=city, city=city, forecasts=[])

    def search_hotels(self, *, city: str, keywords: str) -> HotelSearchResult:
        self._record("search_hotels")
        return HotelSearchResult(
            query_city=city,
            keywords=keywords,
            total_received=0,
            candidates=[],
        )

    def estimate_routes(
        self,
        *,
        city: str,
        plan_fingerprint: str,
        legs: list[Any],
    ) -> RouteEstimateResult:
        self._record("estimate_routes")
        routes: list[RouteEstimate] = []
        for leg in legs:
            def build_available_route() -> RouteEstimate:
                return RouteEstimate(
                    day_index=leg.day_index,
                    leg_index=leg.leg_index,
                    leg_type=leg.leg_type,
                    date=leg.date,
                    origin_name=leg.origin.name,
                    destination_name=leg.destination.name,
                    mode=leg.mode,
                    distance_meters=1800,
                    duration_seconds=900,
                )

            try:
                route = (
                    self.injector.invoke(
                        "estimate_route_segment",
                        build_available_route,
                    )
                    if self.partial_route_failure and self.injector is not None
                    else build_available_route()
                )
            except TimeoutError as exc:
                # Amap route clients preserve successful legs when one segment fails.
                route = RouteEstimate(
                    day_index=leg.day_index,
                    leg_index=leg.leg_index,
                    leg_type=leg.leg_type,
                    date=leg.date,
                    origin_name=leg.origin.name,
                    destination_name=leg.destination.name,
                    mode=leg.mode,
                    available=False,
                    error_code="AMAP_TIMEOUT",
                    error_message=str(exc),
                )
            routes.append(route)
        return RouteEstimateResult(
            plan_fingerprint=plan_fingerprint,
            requested_legs=len(legs),
            evaluated_legs=len(legs),
            truncated_legs=0,
            failed_legs=sum(not route.available for route in routes),
            routes=routes,
        )


class DeterministicPlannerAgent:
    """Typed planner substitute; invalid outputs are injected above this boundary."""

    def __init__(self, *, include_route_legs: bool = False):
        self.include_route_legs = include_route_legs
        self.generate_calls = 0
        self.repair_calls = 0

    def _plan(self, request: TripRequest) -> dict[str, Any]:
        attractions: list[dict[str, Any]] = []
        if self.include_route_legs:
            attractions = [
                {
                    "name": "West Lake",
                    "address": "West Lake Scenic Area, Hangzhou",
                    "location": {"longitude": 120.148, "latitude": 30.244},
                    "visit_duration": 120,
                    "description": "Walk along the lakeside scenic area.",
                    "category": "scenic area",
                    "poi_id": "hangzhou-west-lake",
                },
                {
                    "name": "Grand Canal",
                    "address": "Gongshu District, Hangzhou",
                    "location": {"longitude": 120.300, "latitude": 30.244},
                    "visit_duration": 120,
                    "description": "Walk along the historic waterfront.",
                    "category": "historic waterfront",
                    "poi_id": "hangzhou-grand-canal",
                },
                {
                    "name": "Hangzhou Botanical Garden",
                    "address": "Taoyuanling, Hangzhou",
                    "location": {"longitude": 120.121, "latitude": 30.255},
                    "visit_duration": 120,
                    "description": "Visit the garden and woodland paths.",
                    "category": "park",
                    "poi_id": "hangzhou-botanical-garden",
                },
            ]
        return {
            "city": request.city,
            "start_date": request.start_date,
            "end_date": request.end_date,
            "days": [
                {
                    "date": request.start_date,
                    "day_index": 0,
                    "description": "Deterministic acceptance itinerary.",
                    "transportation": request.transportation,
                    "accommodation": request.accommodation,
                    "attractions": attractions,
                    "meals": [],
                }
            ],
            "weather_info": [],
            "overall_suggestions": "Reserve time buffers and verify opening hours.",
            "budget": None,
        }

    def generate_plan(self, request, attractions, weather, hotels):
        self.generate_calls += 1
        return self._plan(request)

    def repair_plan(
        self,
        request,
        current_plan,
        validation_result,
        attractions,
        weather,
        hotels,
    ):
        self.repair_calls += 1
        return self._plan(request)


def make_orchestrator_fault_request() -> TripRequest:
    """Return the fixed request shared by all recovery cases."""

    return TripRequest(
        city="Hangzhou",
        start_date="2026-08-12",
        end_date="2026-08-12",
        travel_days=1,
        transportation="public transit",
        accommodation="budget hotel",
        preferences=["leisure", "nature"],
    )


FIXED_ORCHESTRATOR_FAULT_CASES: tuple[OrchestratorFaultCase, ...] = (
    OrchestratorFaultCase(
        case_id="attraction-timeout-once",
        description="The first attraction query times out and succeeds on retry.",
        rules=(FaultRule(target="search_attractions", mode=FaultMode.TIMEOUT),),
    ),
    OrchestratorFaultCase(
        case_id="hotel-rate-limit-once",
        description="The first hotel query returns a retryable 429.",
        rules=(FaultRule(target="search_hotels", mode=FaultMode.RATE_LIMIT),),
    ),
    OrchestratorFaultCase(
        case_id="planner-invalid-output-once",
        description="The first planner output fails TripPlan validation.",
        rules=(
            FaultRule(
                target="generate_plan",
                mode=FaultMode.INVALID_OUTPUT,
                injected_output={"invalid": "trip_plan"},
            ),
        ),
    ),
    OrchestratorFaultCase(
        case_id="authorization-failure",
        description="Authorization failure is terminal and is not retried.",
        rules=(FaultRule(target="search_attractions", mode=FaultMode.AUTHORIZATION),),
        recoverable=False,
    ),
    OrchestratorFaultCase(
        case_id="sqlite-locked-once",
        description="The second checkpoint save is locked and succeeds on policy retry.",
        rules=(
            FaultRule(
                target="sqlite.save_state",
                mode=FaultMode.SQLITE_LOCKED,
                call_numbers=[2],
            ),
        ),
    ),
    OrchestratorFaultCase(
        case_id="route-partial-failure",
        description="One route segment fails, then deterministic reordering is re-evaluated.",
        rules=(FaultRule(target="estimate_route_segment", mode=FaultMode.TIMEOUT),),
        include_route_legs=True,
        partial_route_failure=True,
        route_optimization_attempts=1,
    ),
)


def get_orchestrator_fault_case(case_id: str) -> OrchestratorFaultCase:
    for case in FIXED_ORCHESTRATOR_FAULT_CASES:
        if case.case_id == case_id:
            return case
    raise KeyError(f"unknown orchestrator fault case: {case_id}")


def run_orchestrator_fault_case(
    case: OrchestratorFaultCase,
    *,
    database_path: str | Path | None = None,
) -> OrchestratorFaultResult:
    """Execute one case through real runtime components without network access."""

    temporary_directory = None
    if database_path is None:
        temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(temporary_directory.name) / "orchestrator-faults.db"

    injector = FaultInjector(list(case.rules))
    provider = DeterministicAmapProvider(
        injector=injector,
        partial_route_failure=case.partial_route_failure,
    )
    planner = DeterministicPlannerAgent(include_route_legs=case.include_route_legs)
    registry = build_trip_tool_registry(
        planner_agent=planner,
        map_provider=provider,
        call_injector=injector,
    )
    store = SQLiteAgentStateStore(database_path)
    injected_store = FaultInjectingProxy(store, injector, prefix="sqlite")
    checkpoint_policy = CheckpointPolicy(
        max_attempts=3,
        base_delay_seconds=0,
        max_delay_seconds=0,
        sleep_fn=lambda _: None,
    )
    orchestrator = TripOrchestrator(
        tool_registry=registry,
        state_store=injected_store,
        checkpoint_policy=checkpoint_policy,
        max_steps=24,
        max_attempts_per_action=2,
        max_route_optimization_attempts=case.route_optimization_attempts,
        max_schedule_optimization_attempts=0,
        max_constraint_optimization_attempts=0,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
        retry_jitter_seconds=0,
        max_tool_calls=16,
        max_llm_calls=4,
    )

    exception: AgentRuntimeError | None = None
    try:
        state = orchestrator.run(
            make_orchestrator_fault_request(),
            session_id=f"fault-{case.case_id}",
        )
    except AgentRuntimeError as exc:
        exception = exc
        state = exc.state

    persisted_state: AgentState | None = None
    try:
        persisted_state = store.get_state(state.session_id)
    except Exception:
        persisted_state = None

    resume_state: AgentState | None = None
    if persisted_state is not None and persisted_state.status == "completed":
        resume_state = orchestrator.resume(persisted_state.model_copy(deep=True))

    result = OrchestratorFaultResult(
        case=case,
        state=state,
        persisted_state=persisted_state,
        injector=injector,
        exception=exception,
        resume_state=resume_state,
        planner_generate_calls=planner.generate_calls,
        provider_calls=dict(provider.calls),
    )
    if temporary_directory is not None:
        temporary_directory.cleanup()
    return result
