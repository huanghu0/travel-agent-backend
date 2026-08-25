"""用于解决行程可执行性冲突的有界确定性优化器。"""

from __future__ import annotations

from app.constraints.evaluator import ConstraintEvaluator
from app.constraints.models import ConstraintOptimizationCandidate, TripConstraintReport
from app.schemas.trip_schema import TripPlan, TripRequest
from app.scheduling import ScheduleTimelineEvaluator


class DeterministicConstraintOptimizer:
    """优先尝试重排或跨日移动，最后才移除无法修复的景点。"""

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

        # 营业时间冲突经常会同时出现在多个日期。例如茶舍、酒吧被模型误当成
        # 普通景点后，时间轴会把它们安排在上午。通用优化器一次只移动一个景点，
        # 而编排器默认只有一次约束优化预算，因此两个冲突会在第一次优化后仍留下
        # 一个错误，最终浪费 LLM 修复次数。这里先执行一次有界的批量确定性修复：
        # 优先把冲突地点移到当日末尾；仍无法满足营业时间的地点才会被移除，随后
        # 由最低景点保障流程从其他高德候选中回填。
        opening_hours_candidate = self._repair_opening_hours_conflicts(
            request,
            plan,
            baseline_constraints,
            baseline_cost=baseline_cost,
            attractions=attractions,
            weather=weather,
        )
        if opening_hours_candidate is not None:
            return opening_hours_candidate

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

            # 第一步：尝试同一天内的确定性景点重排。
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

            # 第二步：尝试跨日移动，并优先考虑后续日期。
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
            # 如果某个过远景点导致午餐窗口无法满足，重排和跨日移动都可能失败；
            # 只有在前两种策略均不可行时，
            # 才考虑从受影响日期移除一个景点。
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

    def _repair_opening_hours_conflicts(
        self,
        request: TripRequest,
        plan: TripPlan,
        report: TripConstraintReport,
        *,
        baseline_cost: float,
        attractions: dict | None,
        weather: dict | None,
    ) -> ConstraintOptimizationCandidate | None:
        """在一次约束优化尝试中收敛所有明确的景点营业时间冲突。

        最多评估两个候选且不调用外部服务：先批量重排，再批量移除仍然冲突
        的地点。真实路线、时间轴和约束仍由编排器在接受候选前重新验证。
        """

        opening_issues = [
            item
            for item in report.issues
            if item.repairable
            and item.code == "attraction.outside_opening_hours"
            and item.source_index is not None
        ]
        if not opening_issues:
            return None

        # 保存稳定身份，避免同一天移动第一个元素后导致后续 source_index 偏移。
        targets: list[tuple[int, str, str]] = []
        for issue in opening_issues:
            day_position = self._day_position(plan, issue.day_index)
            if day_position is None:
                continue
            source_index = issue.source_index
            if source_index is None or source_index >= len(plan.days[day_position].attractions):
                continue
            attraction = plan.days[day_position].attractions[source_index]
            targets.append((issue.day_index, attraction.poi_id or "", attraction.name))
        if not targets:
            return None

        working = plan.model_copy(deep=True)
        moved_names: list[str] = []
        for day_index, poi_id, name in targets:
            day_position = self._day_position(working, day_index)
            if day_position is None:
                continue
            source_index = self._find_attraction_index(
                working, day_position, poi_id=poi_id, name=name
            )
            if source_index is None:
                continue
            day_attractions = working.days[day_position].attractions
            if source_index != len(day_attractions) - 1:
                moved = day_attractions.pop(source_index)
                day_attractions.append(moved)
                moved_names.append(moved.name)

        considered = 1
        schedule = self.schedule_evaluator.evaluate(request, working, None)
        constraints = self.evaluator.evaluate(
            request, working, schedule, attractions=attractions, weather=weather
        )
        remaining = [
            item
            for item in constraints.issues
            if item.code == "attraction.outside_opening_hours"
            and item.source_index is not None
        ]
        candidate_cost = self._combined_cost(constraints, schedule)
        if not remaining and candidate_cost + 0.01 < baseline_cost:
            first_day = targets[0][0]
            day_position = self._day_position(working, first_day) or 0
            return self._opening_hours_candidate(
                plan=working,
                source_day_index=first_day,
                moved_names=moved_names,
                removed_names=[],
                insertion_index=max(0, len(working.days[day_position].attractions) - 1),
                baseline_cost=baseline_cost,
                candidate_cost=candidate_cost,
                considered=considered,
                strategy="reorder_attractions_for_opening_hours",
            )

        if not remaining or self.max_candidates < 2:
            return None

        # 按日期和下标倒序删除，保证同一天删除多个元素时下标仍然有效。
        removals: list[tuple[int, int, str]] = []
        for issue in remaining:
            day_position = self._day_position(working, issue.day_index)
            source_index = issue.source_index
            if (
                day_position is None
                or source_index is None
                or source_index >= len(working.days[day_position].attractions)
            ):
                continue
            removals.append(
                (
                    day_position,
                    source_index,
                    working.days[day_position].attractions[source_index].name,
                )
            )
        if not removals:
            return None

        removed_names: list[str] = []
        for day_position, source_index, name in sorted(removals, reverse=True):
            working.days[day_position].attractions.pop(source_index)
            removed_names.append(name)
        removed_names.reverse()

        considered += 1
        schedule = self.schedule_evaluator.evaluate(request, working, None)
        constraints = self.evaluator.evaluate(
            request, working, schedule, attractions=attractions, weather=weather
        )
        if any(
            item.code == "attraction.outside_opening_hours"
            for item in constraints.issues
        ):
            return None
        candidate_cost = self._combined_cost(constraints, schedule)
        if candidate_cost + 0.01 >= baseline_cost:
            return None

        first_day = targets[0][0]
        day_position = self._day_position(working, first_day) or 0
        return self._opening_hours_candidate(
            plan=working,
            source_day_index=first_day,
            moved_names=moved_names,
            removed_names=removed_names,
            insertion_index=max(0, len(working.days[day_position].attractions) - 1),
            baseline_cost=baseline_cost,
            candidate_cost=candidate_cost,
            considered=considered,
            strategy="remove_attractions_outside_opening_hours",
        )

    @staticmethod
    def _find_attraction_index(
        plan: TripPlan,
        day_position: int,
        *,
        poi_id: str,
        name: str,
    ) -> int | None:
        for index, attraction in enumerate(plan.days[day_position].attractions):
            if poi_id and attraction.poi_id == poi_id:
                return index
            if attraction.name == name:
                return index
        return None

    @staticmethod
    def _opening_hours_candidate(
        *,
        plan: TripPlan,
        source_day_index: int,
        moved_names: list[str],
        removed_names: list[str],
        insertion_index: int,
        baseline_cost: float,
        candidate_cost: float,
        considered: int,
        strategy: str,
    ) -> ConstraintOptimizationCandidate:
        improvement = (
            (baseline_cost - candidate_cost) / baseline_cost * 100.0
            if baseline_cost > 0
            else 0.0
        )
        affected_names = moved_names or removed_names
        return ConstraintOptimizationCandidate(
            plan=plan,
            source_day_index=source_day_index,
            target_day_index=source_day_index,
            moved_attraction_name="、".join(affected_names),
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
