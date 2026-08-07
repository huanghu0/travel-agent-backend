"""Bounded deterministic cross-day schedule optimization."""

from __future__ import annotations

from app.schemas.trip_schema import TripPlan, TripRequest
from app.scheduling.models import ScheduleOptimizationCandidate, ScheduleQualityReport
from app.scheduling.timeline import ScheduleTimelineEvaluator


class DeterministicScheduleOptimizer:
    """Move at most one final attraction from an overloaded day."""

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
        overloaded = sorted(
            (day for day in report.days if day.overtime_minutes > 0),
            key=lambda day: (-day.overtime_minutes, day.day_index),
        )
        if not overloaded:
            return None

        source_report = overloaded[0]
        source_position = self._day_position(plan, source_report.day_index)
        if source_position is None or not plan.days[source_position].attractions:
            return None

        # Score baseline and candidates with the same Haversine model. Real route
        # estimates are deliberately excluded until the orchestrator verification pass.
        approximate_baseline = self.evaluator.evaluate(request, plan, None)
        baseline_cost = approximate_baseline.optimization_cost
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
        improvement = (
            (baseline_cost - candidate_cost) / baseline_cost * 100.0
            if baseline_cost > 0
            else 0.0
        )
        source_day_index = plan.days[source_position].day_index
        target_day_index = plan.days[target_position].day_index
        return ScheduleOptimizationCandidate(
            plan=candidate_plan,
            source_day_index=(source_position if source_day_index < 0 else source_day_index),
            target_day_index=(target_position if target_day_index < 0 else target_day_index),
            moved_attraction_name=moved.name,
            target_insertion_index=insertion_index,
            strategy="move_last_attraction_to_lower_cost_day",
            baseline_cost=round(baseline_cost, 2),
            candidate_cost=round(candidate_cost, 2),
            approximate_improvement_percent=round(improvement, 2),
            considered_candidates=considered,
        )

    @staticmethod
    def _day_position(plan: TripPlan, day_index: int) -> int | None:
        for position, day in enumerate(plan.days):
            stable_index = day.day_index if day.day_index >= 0 else position
            if stable_index == day_index:
                return position
        return None
