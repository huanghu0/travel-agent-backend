"""有界、确定性的跨日行程时间轴优化。"""

from __future__ import annotations

from app.schemas.trip_schema import TripPlan, TripRequest
from app.scheduling.models import ScheduleOptimizationCandidate, ScheduleQualityReport
from app.scheduling.timeline import ScheduleTimelineEvaluator


class DeterministicScheduleOptimizer:
    """优先跨日移动景点，确实无法容纳时再确定性削减负载。

    跨日移动能够保留全部景点，因此优先级最高；如果所有日期都已经超载，
    且移动无法降低近似成本，则从最严重超时日逐个移除景点，直到时间轴可行
    或耗尽候选预算。无论采用哪种策略，编排器都会在接受前重新查询真实路线。
    """

    def __init__(
        self,
        *,
        evaluator: ScheduleTimelineEvaluator | None = None,
        max_candidates: int = 6,
        min_move_improvement_percent: float = 0.0,
    ):
        if max_candidates < 1:
            raise ValueError("max_candidates must be at least 1")
        if min_move_improvement_percent < 0:
            raise ValueError("min_move_improvement_percent cannot be negative")
        self.evaluator = evaluator or ScheduleTimelineEvaluator()
        self.max_candidates = max_candidates
        self.min_move_improvement_percent = min_move_improvement_percent

    def optimize(
        self,
        request: TripRequest,
        plan: TripPlan,
        report: ScheduleQualityReport,
    ) -> ScheduleOptimizationCandidate | None:
        overloaded = self._overloaded_days(report)
        if not overloaded:
            return None

        # 使用稳定的 Haversine 距离模型为本地候选评分；真实路线数据统一由
        # 编排器在后续验证阶段查询，避免优化器内部产生隐藏的网络调用。
        approximate_baseline = self.evaluator.evaluate(request, plan, None)
        baseline_cost = approximate_baseline.optimization_cost

        move_candidate = self._find_cross_day_move(
            request,
            plan,
            overloaded[0].day_index,
            baseline_cost,
        )
        if move_candidate is not None:
            return move_candidate

        # 当所有日期都超载时，跨日移动只会转移冲突，因此直接停止本轮优化。
        # 只移除让日程恢复可执行所必需的最小确定性景点集合，
        # 比反复要求 LLM 重写整个行程更加稳定和安全。
        return self._build_overload_reduction(
            request,
            plan,
            baseline_cost,
        )

    def _find_cross_day_move(
        self,
        request: TripRequest,
        plan: TripPlan,
        source_day_index: int,
        baseline_cost: float,
    ) -> ScheduleOptimizationCandidate | None:
        source_position = self._day_position(plan, source_day_index)
        if source_position is None or not plan.days[source_position].attractions:
            return None
        if len(plan.days) < 2:
            return None

        moved = plan.days[source_position].attractions[-1]
        target_positions = list(range(source_position + 1, len(plan.days))) + list(
            range(0, source_position)
        )
        best: tuple[float, int, int, TripPlan, int] | None = None
        considered = 0
        for target_position in target_positions:
            target_count = len(plan.days[target_position].attractions)
            for insertion_index in range(target_count + 1):
                if considered >= self.max_candidates:
                    break
                candidate_plan = plan.model_copy(deep=True)
                moved_copy = candidate_plan.days[source_position].attractions.pop()
                candidate_plan.days[target_position].attractions.insert(
                    insertion_index,
                    moved_copy,
                )
                candidate_report = self.evaluator.evaluate(request, candidate_plan, None)
                considered += 1
                candidate_cost = candidate_report.optimization_cost
                if candidate_cost + 0.01 >= baseline_cost:
                    continue
                improvement_percent = self._improvement_percent(
                    baseline_cost,
                    candidate_cost,
                )
                # 编排器会使用相同阈值复验真实路线。近似收益不足的移动
                # 不应提前占用唯一一次优化预算，继续尝试确定性的负载削减。
                if improvement_percent < self.min_move_improvement_percent:
                    continue
                comparison = (
                    candidate_cost,
                    target_position,
                    insertion_index,
                    candidate_plan,
                    considered,
                )
                if best is None or comparison[:3] < best[:3]:
                    best = comparison
            if considered >= self.max_candidates:
                break

        if best is None:
            return None
        candidate_cost, target_position, insertion_index, candidate_plan, _ = best
        return ScheduleOptimizationCandidate(
            plan=candidate_plan,
            source_day_index=self._stable_day_index(plan, source_position),
            target_day_index=self._stable_day_index(plan, target_position),
            moved_attraction_name=moved.name,
            target_insertion_index=insertion_index,
            strategy="move_last_attraction_to_lower_cost_day",
            baseline_cost=round(baseline_cost, 2),
            candidate_cost=round(candidate_cost, 2),
            approximate_improvement_percent=self._improvement_percent(
                baseline_cost,
                candidate_cost,
            ),
            considered_candidates=considered,
        )

    def _build_overload_reduction(
        self,
        request: TripRequest,
        plan: TripPlan,
        baseline_cost: float,
    ) -> ScheduleOptimizationCandidate | None:
        candidate_plan = plan.model_copy(deep=True)
        removed_names: list[str] = []
        first_source_day_index: int | None = None
        considered = 0
        current_report = self.evaluator.evaluate(request, candidate_plan, None)

        while current_report.optimization_recommended and considered < self.max_candidates:
            overloaded = self._overloaded_days(current_report)
            if not overloaded:
                break
            source_position = self._day_position(
                candidate_plan,
                overloaded[0].day_index,
            )
            if source_position is None:
                break
            source_day = candidate_plan.days[source_position]
            if not source_day.attractions:
                break

            # 逐个评估从最严重超时日移除景点后的成本，选择总成本最低的方案；
            # 成本相同时使用原始索引打破平局，保证结果稳定可复现。
            best: tuple[float, int, TripPlan, ScheduleQualityReport, str] | None = None
            for attraction_index, attraction in enumerate(source_day.attractions):
                if considered >= self.max_candidates:
                    break
                reduced = candidate_plan.model_copy(deep=True)
                reduced.days[source_position].attractions.pop(attraction_index)
                reduced_report = self.evaluator.evaluate(request, reduced, None)
                considered += 1
                comparison = (
                    reduced_report.optimization_cost,
                    attraction_index,
                    reduced,
                    reduced_report,
                    attraction.name,
                )
                if best is None or comparison[:2] < best[:2]:
                    best = comparison

            if best is None or best[0] + 0.01 >= current_report.optimization_cost:
                break
            _, _, candidate_plan, current_report, removed_name = best
            if first_source_day_index is None:
                first_source_day_index = self._stable_day_index(plan, source_position)
            removed_names.append(removed_name)

        candidate_cost = current_report.optimization_cost
        if not removed_names or candidate_cost + 0.01 >= baseline_cost:
            return None
        assert first_source_day_index is not None
        return ScheduleOptimizationCandidate(
            plan=candidate_plan,
            source_day_index=first_source_day_index,
            target_day_index=None,
            moved_attraction_name=removed_names[0],
            target_insertion_index=None,
            removed_attraction_names=removed_names,
            strategy="remove_attractions_from_overloaded_days",
            baseline_cost=round(baseline_cost, 2),
            candidate_cost=round(candidate_cost, 2),
            approximate_improvement_percent=self._improvement_percent(
                baseline_cost,
                candidate_cost,
            ),
            considered_candidates=considered,
        )

    @staticmethod
    def _overloaded_days(report: ScheduleQualityReport):
        return sorted(
            (day for day in report.days if day.overtime_minutes > 0),
            key=lambda day: (-day.overtime_minutes, day.day_index),
        )

    @staticmethod
    def _improvement_percent(baseline_cost: float, candidate_cost: float) -> float:
        improvement = (
            (baseline_cost - candidate_cost) / baseline_cost * 100.0
            if baseline_cost > 0
            else 0.0
        )
        return round(improvement, 2)

    @staticmethod
    def _stable_day_index(plan: TripPlan, position: int) -> int:
        day_index = plan.days[position].day_index
        return position if day_index < 0 else day_index

    @staticmethod
    def _day_position(plan: TripPlan, day_index: int) -> int | None:
        for position, day in enumerate(plan.days):
            stable_index = day.day_index if day.day_index >= 0 else position
            if stable_index == day_index:
                return position
        return None
