"""行程草稿的差异分析、增量路线查询和确定性重新评估服务。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.agent_runtime.state import AgentState
from app.constraints import constraint_plan_fingerprint
from app.persistence.exceptions import DraftConflictError
from app.persistence.interfaces import AgentStateStore, TripVersionStore
from app.plan_content.rebuilder import plan_content_source_fingerprint
from app.providers.amap.models import RouteEstimate, RouteEstimateResult, RouteLegRequest
from app.routing import build_route_legs, evaluate_route_quality, plan_route_fingerprint
from app.schemas.trip_draft_schema import (
    ConfirmDraftResponse,
    DraftEvaluationResponse,
    TripDraft,
    TripDraftCreate,
    TripDraftUpdate,
    TripPlanDiff,
    TripPlanVersion,
    TripVersionEvaluation,
)
from app.schemas.trip_schema import TripPlan
from app.tools.registry import ToolRegistry


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _leg_key(leg: RouteLegRequest) -> tuple[int, str, int]:
    return leg.day_index, leg.leg_type, leg.leg_index


def _leg_signature(leg: RouteLegRequest) -> tuple[object, ...]:
    """路线复用必须同时匹配端点、坐标和交通方式，不能只看数组下标。"""

    return (
        leg.day_index,
        leg.leg_type,
        leg.leg_index,
        leg.origin.name,
        round(leg.origin.location.longitude, 6),
        round(leg.origin.location.latitude, 6),
        leg.destination.name,
        round(leg.destination.location.longitude, 6),
        round(leg.destination.location.latitude, 6),
        leg.mode,
    )


def _route_key_text(leg: RouteLegRequest) -> str:
    return f"{leg.day_index}:{leg.leg_type}:{leg.leg_index}:{leg.origin.name}->{leg.destination.name}"


class TripDraftService:
    """在不调用 LLM 的前提下完成编辑草稿的增量重新评估。"""

    def __init__(
        self,
        *,
        state_store: AgentStateStore,
        version_store: TripVersionStore,
        tool_registry: ToolRegistry,
        orchestrator,
    ) -> None:
        self.state_store = state_store
        self.version_store = version_store
        self.tool_registry = tool_registry
        self.orchestrator = orchestrator

    def _evaluate_plan(
        self,
        state: AgentState,
        plan: TripPlan,
        route_result: RouteEstimateResult,
    ) -> tuple[TripPlan, TripVersionEvaluation]:
        """依次重建时间轴、餐饮、约束、校验与最终质量分。"""

        # 第一次时间轴为餐饮时间窗口提供锚点；内容重建不会改变路线输入。
        preliminary_schedule = self.orchestrator.schedule_evaluator.evaluate(
            state.request, plan, route_result
        )
        rebuilt_plan = self.orchestrator.plan_consistency_rebuilder.rebuild(
            state.request,
            plan,
            route_estimates=route_result,
            schedule_quality_report=preliminary_schedule,
            restaurants=state.restaurants,
        )
        schedule_report = self.orchestrator.schedule_evaluator.evaluate(
            state.request, rebuilt_plan, route_result
        )
        # 用最终时间轴再同步一次餐次时间与营业状态。
        rebuilt_plan = self.orchestrator.plan_consistency_rebuilder.rebuild(
            state.request,
            rebuilt_plan,
            route_estimates=route_result,
            schedule_quality_report=schedule_report,
            restaurants=state.restaurants,
        )
        schedule_report = self.orchestrator.schedule_evaluator.evaluate(
            state.request, rebuilt_plan, route_result
        )
        route_quality = evaluate_route_quality(rebuilt_plan, route_result)
        commute_report = self.orchestrator.commute_evaluator.evaluate(
            state.request, rebuilt_plan, route_result
        )
        constraint_report = self.orchestrator.constraint_evaluator.evaluate(
            state.request,
            rebuilt_plan,
            schedule_report,
            attractions=state.attractions,
            weather=state.weather,
        )
        validation = self.orchestrator.validator.validate(
            state.request,
            rebuilt_plan,
            attractions=state.attractions,
            weather=state.weather,
            hotels=state.hotels,
            route_estimates=route_result,
            schedule_quality_report=schedule_report,
            constraint_report=constraint_report,
        )
        evaluation_state = state.model_copy(deep=True)
        evaluation_state.trip_plan = rebuilt_plan
        evaluation_state.route_estimates = route_result.model_dump(mode="json")
        evaluation_state.route_quality_report = route_quality
        evaluation_state.schedule_quality_report = schedule_report
        evaluation_state.commute_report = commute_report
        evaluation_state.constraint_report = constraint_report
        acceptance = self.orchestrator.partial_acceptance_policy.evaluate(
            evaluation_state, validation
        )
        return rebuilt_plan, TripVersionEvaluation(
            route_estimates=route_result,
            route_quality_report=route_quality,
            schedule_quality_report=schedule_report,
            commute_report=commute_report,
            constraint_report=constraint_report,
            validation_result=validation,
            acceptance_report=acceptance,
        )

    def _evaluation_from_state(
        self, state: AgentState
    ) -> tuple[TripPlan, TripVersionEvaluation]:
        if state.trip_plan is None or state.route_estimates is None:
            raise DraftConflictError("当前会话尚未生成可编辑的完整行程")
        route_result = RouteEstimateResult.model_validate(state.route_estimates)
        # 原始版本只做确定性重算，不覆盖历史会话。
        return self._evaluate_plan(state, state.trip_plan, route_result)

    def ensure_original_version(self, state: AgentState) -> TripPlanVersion:
        confirmed = self.version_store.get_confirmed_version(state.session_id)
        if confirmed is not None:
            return confirmed
        if state.trip_plan is None:
            raise DraftConflictError("当前会话没有可编辑行程")
        original_plan, original_evaluation = self._evaluation_from_state(state)
        version = TripPlanVersion(
            version_id=str(uuid4()),
            session_id=state.session_id,
            version_number=1,
            status="confirmed",
            source="original",
            trip_plan=original_plan,
            evaluation=original_evaluation,
            confirmed_at=_utc_now(),
        )
        return self.version_store.save_version(version)

    def create_draft(self, session_id: str, payload: TripDraftCreate) -> TripDraft:
        state = self.state_store.get_state(session_id)
        confirmed = self.ensure_original_version(state)
        base_number = payload.base_version or confirmed.version_number
        if base_number != confirmed.version_number:
            raise DraftConflictError(
                f"基线版本已过期：请求 v{base_number}，当前确认版本为 v{confirmed.version_number}"
            )
        self._validate_identity(state, payload.trip_plan)
        return self.version_store.save_draft(
            TripDraft(
                draft_id=str(uuid4()),
                session_id=session_id,
                base_version=base_number,
                trip_plan=payload.trip_plan.model_copy(deep=True),
            )
        )

    def update_draft(
        self, session_id: str, draft_id: str, payload: TripDraftUpdate
    ) -> TripDraft:
        state = self.state_store.get_state(session_id)
        draft = self.version_store.get_draft(session_id, draft_id)
        if draft.status == "confirmed":
            raise DraftConflictError("已确认草稿不能继续修改，请创建新草稿")
        self._validate_identity(state, payload.trip_plan)
        self.version_store.supersede_candidate(draft.candidate_version_id)
        draft.trip_plan = payload.trip_plan.model_copy(deep=True)
        draft.status = "editing"
        draft.diff = None
        draft.candidate_version_id = None
        draft.updated_at = _utc_now()
        return self.version_store.save_draft(draft)

    @staticmethod
    def _validate_identity(state: AgentState, plan: TripPlan) -> None:
        request = state.request
        if (plan.city, plan.start_date, plan.end_date) != (
            request.city,
            request.start_date,
            request.end_date,
        ):
            raise DraftConflictError("草稿不能修改城市或起止日期")
        if len(plan.days) != request.travel_days:
            raise DraftConflictError("草稿天数必须与原请求一致")

    @staticmethod
    def _diff_plans(before: TripPlan, after: TripPlan) -> TripPlanDiff:
        changed_fields: list[str] = []
        changed_days: list[int] = []
        changed_attractions: list[str] = []
        changed_hotels: list[int] = []
        changed_meals: list[int] = []
        for field in ("city", "start_date", "end_date", "overall_suggestions", "weather_info", "budget"):
            if getattr(before, field) != getattr(after, field):
                changed_fields.append(field)
        max_days = max(len(before.days), len(after.days))
        for position in range(max_days):
            old = before.days[position] if position < len(before.days) else None
            new = after.days[position] if position < len(after.days) else None
            day_index = new.day_index if new is not None else (old.day_index if old is not None else position)
            if old is None or new is None or old != new:
                changed_days.append(day_index)
            old_attractions = old.attractions if old else []
            new_attractions = new.attractions if new else []
            if old_attractions != new_attractions:
                changed_attractions.extend(
                    f"第{day_index + 1}天：{item.name}"
                    for item in new_attractions
                )
            if (old.hotel if old else None) != (new.hotel if new else None):
                changed_hotels.append(day_index)
            if (old.meals if old else []) != (new.meals if new else []):
                changed_meals.append(day_index)
        return TripPlanDiff(
            changed_fields=changed_fields,
            changed_days=sorted(set(changed_days)),
            changed_attractions=changed_attractions,
            changed_hotels=sorted(set(changed_hotels)),
            changed_meals=sorted(set(changed_meals)),
        )

    def _incremental_routes(
        self,
        state: AgentState,
        before: TripPlanVersion,
        after_plan: TripPlan,
        diff: TripPlanDiff,
    ) -> RouteEstimateResult:
        old_legs = build_route_legs(
            state.request, before.trip_plan, attractions=state.attractions, hotels=state.hotels
        )
        new_legs = build_route_legs(
            state.request, after_plan, attractions=state.attractions, hotels=state.hotels
        )
        old_routes = {
            (item.day_index, item.leg_type, item.leg_index): item
            for item in before.evaluation.route_estimates.routes
        }
        reusable_by_signature: dict[tuple[object, ...], RouteEstimate] = {}
        for leg in old_legs:
            route = old_routes.get(_leg_key(leg))
            if route is not None:
                reusable_by_signature[_leg_signature(leg)] = route

        reused: dict[tuple[int, str, int], RouteEstimate] = {}
        affected: list[RouteLegRequest] = []
        for leg in new_legs:
            route = reusable_by_signature.get(_leg_signature(leg))
            if route is None:
                affected.append(leg)
            else:
                reused[_leg_key(leg)] = route.model_copy(deep=True)

        queried: dict[tuple[int, str, int], RouteEstimate] = {}
        cache_hits = cache_misses = failed = 0
        if affected:
            action = self.tool_registry.execute(
                "estimate_routes",
                {
                    "city": state.request.city,
                    "plan_fingerprint": plan_route_fingerprint(state.request, after_plan),
                    "legs": [item.model_dump(mode="json") for item in affected],
                },
            )
            if not action.success:
                raise RuntimeError(f"受影响路线重新查询失败：{action.error or '未知错误'}")
            result = RouteEstimateResult.model_validate(action.data)
            queried = {
                (item.day_index, item.leg_type, item.leg_index): item
                for item in result.routes
            }
            cache_hits = result.cache_hits
            cache_misses = result.cache_misses
            failed = result.failed_legs

        ordered_routes: list[RouteEstimate] = []
        for leg in new_legs:
            route = reused.get(_leg_key(leg)) or queried.get(_leg_key(leg))
            if route is None:
                # 供应商遗漏某一分段时显式生成不可用结果，让质量评估继续完成。
                failed += 1
                route = RouteEstimate(
                    day_index=leg.day_index,
                    leg_index=leg.leg_index,
                    leg_type=leg.leg_type,
                    date=leg.date,
                    origin_name=leg.origin.name,
                    destination_name=leg.destination.name,
                    mode=leg.mode,
                    available=False,
                    error_code="ROUTE_RESULT_MISSING",
                    error_message="路线供应商未返回该分段",
                )
            ordered_routes.append(route)
        diff.affected_route_keys = [_route_key_text(item) for item in affected]
        diff.reused_route_legs = len(reused)
        diff.queried_route_legs = len(affected)
        return RouteEstimateResult(
            plan_fingerprint=plan_route_fingerprint(state.request, after_plan),
            requested_legs=len(new_legs),
            evaluated_legs=len(ordered_routes),
            cache_hits=cache_hits,
            cache_misses=cache_misses,
            failed_legs=failed,
            routes=ordered_routes,
        )

    def evaluate_draft(self, session_id: str, draft_id: str) -> DraftEvaluationResponse:
        state = self.state_store.get_state(session_id)
        draft = self.version_store.get_draft(session_id, draft_id)
        confirmed = self.version_store.get_confirmed_version(session_id)
        if confirmed is None:
            confirmed = self.ensure_original_version(state)
        if draft.base_version != confirmed.version_number:
            raise DraftConflictError(
                f"草稿基线 v{draft.base_version} 已过期，当前确认版本为 v{confirmed.version_number}"
            )
        self.version_store.supersede_candidate(draft.candidate_version_id)
        diff = self._diff_plans(confirmed.trip_plan, draft.trip_plan)
        route_result = self._incremental_routes(state, confirmed, draft.trip_plan, diff)
        rebuilt_plan, evaluation = self._evaluate_plan(state, draft.trip_plan, route_result)
        candidate = TripPlanVersion(
            version_id=str(uuid4()),
            session_id=session_id,
            version_number=self.version_store.next_version_number(session_id),
            status="candidate",
            source="draft",
            source_draft_id=draft.draft_id,
            trip_plan=rebuilt_plan,
            evaluation=evaluation,
        )
        self.version_store.save_version(candidate)
        draft.trip_plan = rebuilt_plan.model_copy(deep=True)
        draft.diff = diff
        draft.candidate_version_id = candidate.version_id
        draft.status = "evaluated"
        draft.updated_at = _utc_now()
        self.version_store.save_draft(draft)
        return DraftEvaluationResponse(
            draft=draft,
            candidate_version=candidate,
            before=confirmed.quality_snapshot(),
            after=candidate.quality_snapshot(),
            diff=diff,
        )

    def confirm_draft(self, session_id: str, draft_id: str) -> ConfirmDraftResponse:
        state = self.state_store.get_state(session_id)
        draft = self.version_store.get_draft(session_id, draft_id)
        confirmed = self.version_store.get_confirmed_version(session_id)
        if confirmed is None or draft.base_version != confirmed.version_number:
            raise DraftConflictError("草稿基线已过期，请重新创建草稿")
        if draft.status != "evaluated" or not draft.candidate_version_id:
            raise DraftConflictError("草稿尚未完成重新评估")
        version = self.version_store.get_version_by_id(draft.candidate_version_id)
        version = self.version_store.confirm_version(version)

        # 只有用户确认后才同步 AgentState，execution-view 会自然展示新版本。
        evaluation = version.evaluation
        state.trip_plan = version.trip_plan.model_copy(deep=True)
        state.route_estimates = evaluation.route_estimates.model_dump(mode="json")
        state.route_plan_fingerprint = evaluation.route_estimates.plan_fingerprint
        state.route_quality_report = evaluation.route_quality_report
        state.route_quality_plan_fingerprint = evaluation.route_quality_report.plan_fingerprint
        state.schedule_quality_report = evaluation.schedule_quality_report
        state.schedule_quality_plan_fingerprint = evaluation.schedule_quality_report.plan_fingerprint
        state.commute_report = evaluation.commute_report
        state.commute_plan_fingerprint = evaluation.commute_report.plan_fingerprint
        state.constraint_report = evaluation.constraint_report
        state.constraint_plan_fingerprint = constraint_plan_fingerprint(
            state.request, state.trip_plan
        )
        state.last_validation_result = evaluation.validation_result
        state.acceptance_report = evaluation.acceptance_report
        state.plan_consistency_fingerprint = plan_content_source_fingerprint(
            state.request,
            state.trip_plan,
            state.route_estimates,
            state.schedule_quality_report,
            state.restaurants,
        )
        state.updated_at = _utc_now()
        self.state_store.save_state(state)

        draft.status = "confirmed"
        draft.updated_at = _utc_now()
        self.version_store.save_draft(draft)
        return ConfirmDraftResponse(draft=draft, confirmed_version=version)
