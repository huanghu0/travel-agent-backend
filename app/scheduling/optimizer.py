"""Bounded deterministic cross-day schedule optimization."""

from __future__ import annotations

from app.schemas.trip_schema import TripPlan, TripRequest
from app.scheduling.models import ScheduleOptimizationCandidate, ScheduleQualityReport
from app.scheduling.timeline import ScheduleTimelineEvaluator


class DeterministicScheduleOptimizer:
    """Move attractions first, then deterministically shed impossible workload.

    Cross-day movement preserves every attraction and therefore remains the preferred
    strategy. If every day is already overloaded and no move lowers the approximate
    schedule cost, the optimizer removes one attraction at a time from the worst day
    until the approximate timeline is feasible or the bounded candidate budget is
    exhausted. The orchestrator always re-fetches real routes before accepting either
    strategy.
    """

    def __init__(
        self,
        *,
        evaluator: ScheduleTimelineEvaluator | None = None,
        max_candidates: int = 6,
    ):
        if max_candidates < 1:
            raise ValueError("max_candidates must be at least 1")
        self.evaluator = evaluator or ScheduleTimelineEvaluator()
        self.max_candidates = max_candidates

    def optimize(
        self,
        request: TripRequest,
        plan: TripPlan,
        report: ScheduleQualityReport,
    ) -> ScheduleOptimizationCandidate | None:
        overloaded = self._overloaded_days(report)
        if not overloaded:
            return None

        # Score all local candidates with one stable Haversine model. Real route
        # estimates are deliberately fetched by the orchestrator verification pass.
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

        # When all days are overloaded, moving work merely transfers the conflict.
        # Shed the smallest deterministic set of attractions needed to fit the daily
        # windows. This is safer than asking the LLM to repeatedly rewrite the plan.
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

            # Evaluate removing each attraction from the worst day. Lowest total
            # cost wins; the original index breaks ties to keep the result stable.
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
