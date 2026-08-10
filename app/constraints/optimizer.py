"""Bounded deterministic optimizer for execution-constraint conflicts."""

from __future__ import annotations

from app.constraints.evaluator import ConstraintEvaluator
from app.constraints.models import ConstraintOptimizationCandidate, TripConstraintReport
from app.schemas.trip_schema import TripPlan, TripRequest
from app.scheduling import ScheduleTimelineEvaluator


class DeterministicConstraintOptimizer:
    """Prefer reorder/move candidates, then shed an irreparable attraction."""

    def __init__(
        self,
        *,
        evaluator: ConstraintEvaluator | None = None,
        schedule_evaluator: ScheduleTimelineEvaluator | None = None,
        max_candidates: int = 8,
    ):
        if max_candidates < 1:
            raise ValueError("max_candidates must be at least 1")
        self.evaluator = evaluator or ConstraintEvaluator()
        self.schedule_evaluator = schedule_evaluator or ScheduleTimelineEvaluator()
        self.max_candidates = max_candidates

    def optimize(
        self,
        request: TripRequest,
        plan: TripPlan,
        report: TripConstraintReport,
        *,
        attractions: dict | None = None,
        weather: dict | None = None,
    ) -> ConstraintOptimizationCandidate | None:
        actionable = sorted(
            (item for item in report.issues if item.repairable),
            key=lambda item: (
                0 if item.severity == "error" else 1,
                -item.penalty,
                item.day_index,
                item.source_index if item.source_index is not None else 10**6,
                item.code,
            ),
        )
        if not actionable:
            return None

        baseline_schedule = self.schedule_evaluator.evaluate(request, plan, None)
        baseline_constraints = self.evaluator.evaluate(
            request,
            plan,
            baseline_schedule,
            attractions=attractions,
            weather=weather,
        )
        baseline_cost = self._combined_cost(baseline_constraints, baseline_schedule)
        candidates: list[tuple[str, int, int, int, TripPlan, str]] = []
        seen: set[str] = set()

        for issue in actionable:
            source_position = self._day_position(plan, issue.day_index)
            if source_position is None or not plan.days[source_position].attractions:
                continue
            source_index = issue.source_index
            if source_index is None or source_index >= len(plan.days[source_position].attractions):
                source_index = len(plan.days[source_position].attractions) - 1
            moved_name = plan.days[source_position].attractions[source_index].name

            # First try same-day deterministic reordering.
            for insertion_index in range(len(plan.days[source_position].attractions)):
                if insertion_index == source_index:
                    continue
                candidate = plan.model_copy(deep=True)
                moved = candidate.days[source_position].attractions.pop(source_index)
                candidate.days[source_position].attractions.insert(insertion_index, moved)
                self._append_unique(
                    candidates,
                    seen,
                    candidate,
                    "reorder_attraction_within_day",
                    source_position,
                    source_position,
                    insertion_index,
                    moved_name,
                )
                if len(candidates) >= self.max_candidates:
                    break
            if len(candidates) >= self.max_candidates:
                break

            # Then try cross-day movement, preferring later days.
            target_positions = list(range(source_position + 1, len(plan.days))) + list(
                range(0, source_position)
            )
            for target_position in target_positions:
                target_count = len(plan.days[target_position].attractions)
                for insertion_index in range(target_count + 1):
                    candidate = plan.model_copy(deep=True)
                    moved = candidate.days[source_position].attractions.pop(source_index)
                    candidate.days[target_position].attractions.insert(insertion_index, moved)
                    self._append_unique(
                        candidates,
                        seen,
                        candidate,
                        "move_attraction_between_days",
                        source_position,
                        target_position,
                        insertion_index,
                        moved_name,
                    )
                    if len(candidates) >= self.max_candidates:
                        break
                if len(candidates) >= self.max_candidates:
                    break
            if len(candidates) >= self.max_candidates:
                break

        best: tuple[float, int, int, int, TripPlan, str, str] | None = None
        considered = 0
        for strategy, source_position, target_position, insertion_index, candidate, moved_name in candidates:
            schedule = self.schedule_evaluator.evaluate(request, candidate, None)
            constraints = self.evaluator.evaluate(
                request,
                candidate,
                schedule,
                attractions=attractions,
                weather=weather,
            )
            considered += 1
            candidate_cost = self._combined_cost(constraints, schedule)
            if candidate_cost + 0.01 >= baseline_cost:
                continue
            comparison = (
                candidate_cost,
                source_position,
                target_position,
                insertion_index,
                candidate,
                strategy,
                moved_name,
            )
            if best is None or comparison[:4] < best[:4]:
                best = comparison

        removed_names: list[str] = []
        if best is None:
            # Reordering and cross-day movement can both fail when a single remote
            # attraction makes the lunch window impossible. Only then consider
            # removing one attraction from the affected day.
            removal_best: tuple[float, int, int, TripPlan, str] | None = None
            removal_considered = 0
            visited_days: set[int] = set()
            for issue in actionable:
                source_position = self._day_position(plan, issue.day_index)
                if source_position is None or source_position in visited_days:
                    continue
                visited_days.add(source_position)
                for attraction_index, attraction in enumerate(
                    plan.days[source_position].attractions
                ):
                    if removal_considered >= self.max_candidates:
                        break
                    candidate = plan.model_copy(deep=True)
                    candidate.days[source_position].attractions.pop(attraction_index)
                    schedule = self.schedule_evaluator.evaluate(request, candidate, None)
                    constraints = self.evaluator.evaluate(
                        request,
                        candidate,
                        schedule,
                        attractions=attractions,
                        weather=weather,
                    )
                    removal_considered += 1
                    candidate_cost = self._combined_cost(constraints, schedule)
                    if candidate_cost + 0.01 >= baseline_cost:
                        continue
                    comparison = (
                        candidate_cost,
                        source_position,
                        attraction_index,
                        candidate,
                        attraction.name,
                    )
                    if removal_best is None or comparison[:3] < removal_best[:3]:
                        removal_best = comparison
                if removal_considered >= self.max_candidates:
                    break
            considered += removal_considered
            if removal_best is None:
                return None
            (
                candidate_cost,
                source_position,
                insertion_index,
                candidate_plan,
                moved_name,
            ) = removal_best
            target_position = source_position
            strategy = "remove_attraction_for_constraint_feasibility"
            removed_names = [moved_name]
        else:
            (
                candidate_cost,
                source_position,
                target_position,
                insertion_index,
                candidate_plan,
                strategy,
                moved_name,
            ) = best
        improvement = (
            (baseline_cost - candidate_cost) / baseline_cost * 100.0
            if baseline_cost > 0
            else 0.0
        )
        return ConstraintOptimizationCandidate(
            plan=candidate_plan,
            source_day_index=self._stable_day_index(plan, source_position),
            target_day_index=self._stable_day_index(plan, target_position),
            moved_attraction_name=moved_name,
            target_insertion_index=insertion_index,
            removed_attraction_names=removed_names,
            strategy=strategy,
            baseline_cost=round(baseline_cost, 2),
            candidate_cost=round(candidate_cost, 2),
            approximate_improvement_percent=round(improvement, 2),
            considered_candidates=considered,
        )

    @staticmethod
    def _combined_cost(constraints, schedule) -> float:
        return round(constraints.optimization_cost + schedule.optimization_cost, 2)

    @staticmethod
    def _day_position(plan: TripPlan, day_index: int) -> int | None:
        for position, day in enumerate(plan.days):
            stable = day.day_index if day.day_index >= 0 else position
            if stable == day_index:
                return position
        return None

    @staticmethod
    def _stable_day_index(plan: TripPlan, position: int) -> int:
        value = plan.days[position].day_index
        return position if value < 0 else value

    @staticmethod
    def _append_unique(
        candidates: list,
        seen: set[str],
        plan: TripPlan,
        strategy: str,
        source_position: int,
        target_position: int,
        insertion_index: int,
        moved_name: str,
    ) -> None:
        signature = plan.model_dump_json()
        if signature in seen:
            return
        seen.add(signature)
        candidates.append(
            (
                strategy,
                source_position,
                target_position,
                insertion_index,
                plan,
                moved_name,
            )
        )
