"""确定性评估行程在真实场景中的可执行性约束。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any

from app.constraints.models import (
    ConstraintIssue,
    DayConstraintReport,
    TripConstraintReport,
)
from app.schemas.trip_schema import Attraction, TripPlan, TripRequest
from app.scheduling import ScheduleQualityReport


_CLOCK_RANGE = re.compile(
    r"(?P<start_h>[0-2]?\d):(?P<start_m>[0-5]\d)\s*[-~\uFF5E\u2014\u2013\u81F3]\s*"
    r"(?P<end_h>[0-2]?\d):(?P<end_m>[0-5]\d)"
)
_ADVERSE_WEATHER = (
    "暴雨",
    "大雨",
    "雷暴",
    "雷阵雨",
    "台风",
    "暴雪",
    "大雪",
    "冰雹",
)
_OUTDOOR_MARKERS = (
    "公园",
    "山",
    "湖",
    "海",
    "滩",
    "草原",
    "湿地",
    "峡谷",
    "森林",
    "动物园",
    "植物园",
    "风景名胜区",
)


def _normalized(value: object) -> str:
    return "".join(str(value or "").strip().lower().split())


def _clock_minutes(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":", 1))
    return hour * 60 + minute


def _timeline_clock(value: str) -> int:
    parts = value.split(":", 1)
    return int(parts[0]) * 60 + int(parts[1])


def constraint_plan_fingerprint(request: TripRequest, plan: TripPlan) -> str:
    """对所有影响可执行性规则的请求和行程字段生成稳定指纹。"""

    payload = {
        "request": {
            "transportation": request.transportation,
            "preferences": request.preferences,
            "free_text_input": request.free_text_input or "",
        },
        "plan": plan.model_dump(mode="json"),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ConstraintEvaluator:
    """评估营业时间、用餐窗口、用户偏好以及天气风险。"""

    def __init__(
        self,
        *,
        lunch_window_start: str = "11:30",
        lunch_window_end: str = "14:00",
        daily_attraction_soft_limit: int = 5,
    ):
        self.lunch_start = _clock_minutes(lunch_window_start)
        self.lunch_end = _clock_minutes(lunch_window_end)
        if self.lunch_end <= self.lunch_start:
            raise ValueError("lunch_window_end must be later than lunch_window_start")
        if daily_attraction_soft_limit < 1:
            raise ValueError("daily_attraction_soft_limit must be at least 1")
        self.daily_attraction_soft_limit = daily_attraction_soft_limit

    def evaluate(
        self,
        request: TripRequest,
        plan: TripPlan,
        schedule_report: ScheduleQualityReport | dict,
        *,
        attractions: dict[str, Any] | None = None,
        weather: dict[str, Any] | None = None,
    ) -> TripConstraintReport:
        schedule = (
            schedule_report
            if isinstance(schedule_report, ScheduleQualityReport)
            else ScheduleQualityReport.model_validate(schedule_report)
        )
        source_by_id, source_by_name = self._source_indexes(attractions)
        weather_by_date = self._weather_by_date(weather, plan)
        schedule_by_day = {day.day_index: day for day in schedule.days}
        all_issues: list[ConstraintIssue] = []
        day_reports: list[DayConstraintReport] = []

        for day_position, day in enumerate(plan.days):
            day_index = day.day_index if day.day_index >= 0 else day_position
            day_schedule = schedule_by_day.get(day_index)
            issues: list[ConstraintIssue] = []
            if day_schedule is not None:
                self._check_lunch_window(day_index, day_schedule.timeline, issues)
                self._check_attraction_hours(
                    day_index,
                    day.date,
                    day.attractions,
                    day_schedule.timeline,
                    source_by_id,
                    source_by_name,
                    issues,
                )
                self._check_time_preferences(
                    request,
                    day_index,
                    day.attractions,
                    day_schedule.timeline,
                    issues,
                )
            self._check_daily_load(day_index, day.attractions, issues)
            self._check_weather(
                day_index,
                day.date,
                day.attractions,
                source_by_id,
                source_by_name,
                weather_by_date,
                issues,
            )
            errors = sum(item.severity == "error" for item in issues)
            warnings = sum(item.severity == "warning" for item in issues)
            cost = round(sum(item.penalty for item in issues), 2)
            day_reports.append(
                DayConstraintReport(
                    day_index=day_index,
                    date=day.date,
                    error_count=errors,
                    warning_count=warnings,
                    optimization_cost=cost,
                    feasible=errors == 0,
                    issues=issues,
                )
            )
            all_issues.extend(issues)

        error_count = sum(item.severity == "error" for item in all_issues)
        warning_count = sum(item.severity == "warning" for item in all_issues)
        repairable_count = sum(item.repairable for item in all_issues)
        cost = round(sum(item.penalty for item in all_issues), 2)
        score = max(0.0, min(100.0, 100.0 - error_count * 25.0 - warning_count * 5.0))
        return TripConstraintReport(
            plan_fingerprint=constraint_plan_fingerprint(request, plan),
            error_count=error_count,
            warning_count=warning_count,
            repairable_issue_count=repairable_count,
            optimization_cost=cost,
            quality_score=round(score, 2),
            feasible=error_count == 0,
            optimization_recommended=any(item.repairable for item in all_issues),
            days=day_reports,
            issues=all_issues,
        )

    def _check_lunch_window(
        self,
        day_index: int,
        timeline: Iterable[Any],
        issues: list[ConstraintIssue],
    ) -> None:
        for item in timeline:
            if item.item_type != "meal":
                continue
            start = _timeline_clock(item.start_time)
            end = _timeline_clock(item.end_time)
            if start >= self.lunch_start and end <= self.lunch_end:
                continue
            issues.append(
                ConstraintIssue(
                    code="meal.outside_time_window",
                    severity="error",
                    path=f"days[{day_index}].meals",
                    message=(
                        f"Lunch is scheduled at {item.start_time}-{item.end_time}, "
                        "outside the configured meal window"
                    ),
                    repair_hint="Reorder attractions so lunch falls inside the meal window",
                    day_index=day_index,
                    penalty=1200.0,
                    expected={
                        "start": self._format_clock(self.lunch_start),
                        "end": self._format_clock(self.lunch_end),
                    },
                    actual={"start": item.start_time, "end": item.end_time},
                )
            )

    def _check_attraction_hours(
        self,
        day_index: int,
        day_date: str,
        attractions: list[Attraction],
        timeline: Iterable[Any],
        source_by_id: dict[str, dict[str, Any]],
        source_by_name: dict[str, dict[str, Any]],
        issues: list[ConstraintIssue],
    ) -> None:
        timeline_by_source = {
            item.source_index: item
            for item in timeline
            if item.item_type == "attraction" and item.source_index is not None
        }
        for source_index, attraction in enumerate(attractions):
            source = self._source_for(attraction, source_by_id, source_by_name)
            if self._is_closed_on_date(source, day_date):
                issues.append(
                    ConstraintIssue(
                        code="attraction.closed_on_date",
                        severity="error",
                        path=f"days[{day_index}].attractions[{source_index}]",
                        message=f"{attraction.name} is marked closed on {day_date}",
                        repair_hint="Move this attraction to an open date",
                        day_index=day_index,
                        source_index=source_index,
                        attraction_name=attraction.name,
                        penalty=1800.0,
                        expected="open date",
                        actual=day_date,
                    )
                )
                continue
            hours = self._opening_ranges(source)
            item = timeline_by_source.get(source_index)
            if not hours or item is None:
                continue
            start = _timeline_clock(item.start_time)
            end = _timeline_clock(item.end_time)
            if any(start >= opening and end <= closing for opening, closing in hours):
                continue
            issues.append(
                ConstraintIssue(
                    code="attraction.outside_opening_hours",
                    severity="error",
                    path=f"days[{day_index}].attractions[{source_index}]",
                    message=(
                        f"{attraction.name} is scheduled at {item.start_time}-{item.end_time} "
                        "outside its known opening hours"
                    ),
                    repair_hint="Reorder the day or move the attraction to another date",
                    day_index=day_index,
                    source_index=source_index,
                    attraction_name=attraction.name,
                    penalty=1600.0,
                    expected=[
                        f"{self._format_clock(opening)}-{self._format_clock(closing)}"
                        for opening, closing in hours
                    ],
                    actual=f"{item.start_time}-{item.end_time}",
                )
            )

    def _check_time_preferences(
        self,
        request: TripRequest,
        day_index: int,
        attractions: list[Attraction],
        timeline: Iterable[Any],
        issues: list[ConstraintIssue],
    ) -> None:
        text = request.free_text_input or ""
        if not text:
            return
        timeline_by_source = {
            item.source_index: item
            for item in timeline
            if item.item_type == "attraction" and item.source_index is not None
        }
        for source_index, attraction in enumerate(attractions):
            period = self._requested_period(text, attraction.name)
            item = timeline_by_source.get(source_index)
            if period is None or item is None:
                continue
            start = _timeline_clock(item.start_time)
            actual = "morning" if start < 12 * 60 else "afternoon"
            if actual == period:
                continue
            issues.append(
                ConstraintIssue(
                    code="preference.wrong_time_period",
                    severity="error",
                    path=f"days[{day_index}].attractions[{source_index}]",
                    message=(
                        f"{attraction.name} is scheduled in the {actual}, "
                        f"but the request requires {period}"
                    ),
                    repair_hint="Reorder attractions to satisfy the requested time period",
                    day_index=day_index,
                    source_index=source_index,
                    attraction_name=attraction.name,
                    penalty=1400.0,
                    expected=period,
                    actual=actual,
                )
            )

    def _check_daily_load(
        self,
        day_index: int,
        attractions: list[Attraction],
        issues: list[ConstraintIssue],
    ) -> None:
        count = len(attractions)
        if count <= self.daily_attraction_soft_limit:
            return
        issues.append(
            ConstraintIssue(
                code="schedule.too_many_attractions",
                severity="warning",
                path=f"days[{day_index}].attractions",
                message=(
                    f"The day contains {count} attractions, above the configured soft limit "
                    f"of {self.daily_attraction_soft_limit}"
                ),
                repair_hint="Move one attraction to a less busy day",
                day_index=day_index,
                source_index=count - 1,
                attraction_name=attractions[-1].name,
                penalty=250.0,
                expected=self.daily_attraction_soft_limit,
                actual=count,
            )
        )

    def _check_weather(
        self,
        day_index: int,
        day_date: str,
        attractions: list[Attraction],
        source_by_id: dict[str, dict[str, Any]],
        source_by_name: dict[str, dict[str, Any]],
        weather_by_date: dict[str, str],
        issues: list[ConstraintIssue],
    ) -> None:
        condition = weather_by_date.get(day_date, "")
        if not condition or not any(marker in condition for marker in _ADVERSE_WEATHER):
            return
        for source_index, attraction in enumerate(attractions):
            source = self._source_for(attraction, source_by_id, source_by_name)
            category = str(source.get("category", attraction.category or ""))
            descriptor = f"{attraction.name} {category}"
            if not any(marker in descriptor for marker in _OUTDOOR_MARKERS):
                continue
            issues.append(
                ConstraintIssue(
                    code="weather.outdoor_risk",
                    severity="warning",
                    path=f"days[{day_index}].attractions[{source_index}]",
                    message=f"{attraction.name} is an outdoor activity during {condition}",
                    repair_hint="Move the outdoor attraction to a day with safer weather",
                    day_index=day_index,
                    source_index=source_index,
                    attraction_name=attraction.name,
                    penalty=400.0,
                    expected="non-adverse weather",
                    actual=condition,
                )
            )

    @staticmethod
    def _source_candidates(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        candidates = payload.get("candidates", payload.get("pois", []))
        return [item for item in candidates if isinstance(item, dict)] if isinstance(candidates, list) else []

    @classmethod
    def _source_indexes(
        cls,
        payload: dict[str, Any] | None,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        by_id: dict[str, dict[str, Any]] = {}
        by_name: dict[str, dict[str, Any]] = {}
        for item in cls._source_candidates(payload):
            poi_id = _normalized(item.get("poi_id", item.get("id")))
            name = _normalized(item.get("name"))
            if poi_id:
                by_id.setdefault(poi_id, item)
            if name:
                by_name.setdefault(name, item)
        return by_id, by_name

    @staticmethod
    def _source_for(
        attraction: Attraction,
        by_id: dict[str, dict[str, Any]],
        by_name: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        if attraction.poi_id:
            source = by_id.get(_normalized(attraction.poi_id))
            if source is not None:
                return source
        return by_name.get(_normalized(attraction.name), {})

    @staticmethod
    def _opening_ranges(source: dict[str, Any]) -> list[tuple[int, int]]:
        value = source.get(
            "opening_hours",
            source.get("opentime", source.get("business_hours", "")),
        )
        if isinstance(value, dict):
            value = value.get("today", value.get("value", ""))
        text = str(value or "")
        if not text or "\u6682\u65e0" in text or "24\u5c0f\u65f6" in text:
            return []
        ranges: list[tuple[int, int]] = []
        for match in _CLOCK_RANGE.finditer(text):
            start = int(match.group("start_h")) * 60 + int(match.group("start_m"))
            end = int(match.group("end_h")) * 60 + int(match.group("end_m"))
            if 0 <= start < end <= 24 * 60:
                ranges.append((start, end))
        return ranges

    @staticmethod
    def _is_closed_on_date(source: dict[str, Any], day_date: str) -> bool:
        closed_dates = source.get("closed_dates", [])
        return isinstance(closed_dates, list) and day_date in {
            str(value) for value in closed_dates
        }

    @staticmethod
    def _requested_period(text: str, attraction_name: str) -> str | None:
        escaped = re.escape(attraction_name)
        patterns = (
            rf"(\u4e0a\u5348|\u65e9\u4e0a).{{0,10}}{escaped}",
            rf"{escaped}.{{0,10}}(\u4e0a\u5348|\u65e9\u4e0a)",
            rf"(\u4e0b\u5348).{{0,10}}{escaped}",
            rf"{escaped}.{{0,10}}(\u4e0b\u5348)",
        )
        for index, pattern in enumerate(patterns):
            if re.search(pattern, text):
                return "morning" if index < 2 else "afternoon"
        return None

    @staticmethod
    def _weather_by_date(
        payload: dict[str, Any] | None,
        plan: TripPlan,
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        if isinstance(payload, dict):
            values = payload.get("forecasts", payload.get("weather", []))
            if isinstance(values, list):
                for item in values:
                    if not isinstance(item, dict):
                        continue
                    day_date = str(item.get("date", ""))
                    condition = str(
                        item.get("day_weather", item.get("dayweather", "")) or ""
                    )
                    if day_date:
                        result[day_date] = condition
        for item in plan.weather_info:
            result.setdefault(item.date, str(item.day_weather or ""))
        return result

    @staticmethod
    def _format_clock(total_minutes: int) -> str:
        hours, minutes = divmod(total_minutes, 60)
        return f"{hours:02d}:{minutes:02d}"
