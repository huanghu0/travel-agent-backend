"""旅行计划确定性语义校验器：不依赖 LLM，输出可修复的结构化问题。"""

from __future__ import annotations

from datetime import date, timedelta
from math import asin, cos, radians, sin, sqrt
from typing import Any

from app.constraints import TripConstraintReport
from app.providers.amap.models import RouteEstimate, RouteEstimateResult
from app.schemas.trip_schema import TripPlan, TripRequest
from app.scheduling import ScheduleQualityReport
from app.validation.models import (
    TripValidationResult,
    ValidationIssue,
    ValidationSeverity,
)


class TripPlanValidator:
    """Validate a TripPlan without asking an LLM to make decisions."""

    def validate(
        self,
        request: TripRequest,
        plan: TripPlan,
        *,
        attractions: dict[str, Any] | None = None,
        weather: dict[str, Any] | None = None,
        hotels: dict[str, Any] | None = None,
        route_estimates: dict[str, Any] | RouteEstimateResult | None = None,
        schedule_quality_report: ScheduleQualityReport | dict | None = None,
        constraint_report: TripConstraintReport | dict | None = None,
    ) -> TripValidationResult:
        issues: list[ValidationIssue] = []
        # 步骤 1：先校验用户请求本身。请求日期冲突属于不可由 LLM 修复的问题。
        request_start = self._parse_date(
            request.start_date,
            issues,
            path="request.start_date",
            code="request.invalid_start_date",
            repairable=False,
        )
        request_end = self._parse_date(
            request.end_date,
            issues,
            path="request.end_date",
            code="request.invalid_end_date",
            repairable=False,
        )

        # 步骤 2：根据起止日期计算期望日期序列，并核对 travel_days。
        expected_dates: list[date] = []
        if request_start is not None and request_end is not None:
            if request_end < request_start:
                self._error(
                    issues,
                    code="request.invalid_date_range",
                    path="request.end_date",
                    message="请求结束日期早于开始日期",
                    repair_hint="修正用户请求中的日期范围后重新执行",
                    repairable=False,
                    expected=f">= {request.start_date}",
                    actual=request.end_date,
                )
            else:
                calendar_days = (request_end - request_start).days + 1
                if calendar_days != request.travel_days:
                    self._error(
                        issues,
                        code="request.travel_days_mismatch",
                        path="request.travel_days",
                        message="请求中的旅行天数与起止日期不一致",
                        repair_hint="修正 travel_days 或起止日期后重新执行",
                        repairable=False,
                        expected=calendar_days,
                        actual=request.travel_days,
                    )
                expected_dates = [
                    request_start + timedelta(days=offset)
                    for offset in range(calendar_days)
                ]

        # 步骤 3：依次校验行程身份、每日安排、天气、预算、候选来源和路线距离。
        self._validate_plan_identity(request, plan, issues)
        self._validate_days(request, plan, expected_dates, issues)
        self._validate_weather(plan, expected_dates, issues)
        self._validate_budget(plan, issues)
        self._validate_source_consistency(plan, attractions, hotels, issues)
        self._validate_route_distances(plan, issues, route_estimates)
        self._validate_schedule_capacity(issues, schedule_quality_report)
        self._validate_execution_constraints(issues, constraint_report)

        if not plan.overall_suggestions.strip():
            self._error(
                issues,
                code="plan.empty_suggestions",
                path="overall_suggestions",
                message="总体建议不能为空",
                repair_hint="补充与天气、交通、预约和安全相关的总体建议",
                actual=plan.overall_suggestions,
            )

        # 步骤 4：汇总成统一结果，编排器据此决定完成、修复或失败。
        return TripValidationResult.from_issues(issues)

    def _validate_plan_identity(
        self,
        request: TripRequest,
        plan: TripPlan,
        issues: list[ValidationIssue],
    ) -> None:
        if self._normalize_place(plan.city) != self._normalize_place(request.city):
            self._error(
                issues,
                code="plan.city_mismatch",
                path="city",
                message="行程城市与用户请求不一致",
                repair_hint="将 city 修正为用户请求的目的地",
                expected=request.city,
                actual=plan.city,
            )
        if plan.start_date != request.start_date:
            self._error(
                issues,
                code="plan.start_date_mismatch",
                path="start_date",
                message="行程开始日期与用户请求不一致",
                repair_hint="将 start_date 修正为请求中的开始日期",
                expected=request.start_date,
                actual=plan.start_date,
            )
        if plan.end_date != request.end_date:
            self._error(
                issues,
                code="plan.end_date_mismatch",
                path="end_date",
                message="行程结束日期与用户请求不一致",
                repair_hint="将 end_date 修正为请求中的结束日期",
                expected=request.end_date,
                actual=plan.end_date,
            )

    def _validate_days(
        self,
        request: TripRequest,
        plan: TripPlan,
        expected_dates: list[date],
        issues: list[ValidationIssue],
    ) -> None:
        if len(plan.days) != request.travel_days:
            self._error(
                issues,
                code="days.count_mismatch",
                path="days",
                message="每日行程数量与旅行天数不一致",
                repair_hint="为旅行范围内的每一天生成且只生成一个 DayPlan",
                expected=request.travel_days,
                actual=len(plan.days),
            )

        seen_dates: set[str] = set()
        for position, day in enumerate(plan.days):
            path = f"days[{position}]"
            parsed_date = self._parse_date(
                day.date,
                issues,
                path=f"{path}.date",
                code="day.invalid_date",
            )
            if day.date in seen_dates:
                self._error(
                    issues,
                    code="day.duplicate_date",
                    path=f"{path}.date",
                    message="存在重复的每日行程日期",
                    repair_hint="每个日期只保留一个 DayPlan",
                    actual=day.date,
                )
            seen_dates.add(day.date)

            if position < len(expected_dates):
                expected_date = expected_dates[position].isoformat()
                if day.date != expected_date:
                    self._error(
                        issues,
                        code="day.date_sequence_mismatch",
                        path=f"{path}.date",
                        message="每日行程日期未按请求日期连续排列",
                        repair_hint="按开始日期递增生成每天的 date",
                        expected=expected_date,
                        actual=day.date,
                    )
            elif parsed_date is not None and expected_dates:
                self._error(
                    issues,
                    code="day.date_out_of_range",
                    path=f"{path}.date",
                    message="每日行程日期超出请求范围",
                    repair_hint="删除范围外日期或改为请求范围内缺失的日期",
                    actual=day.date,
                )

            if day.day_index != position:
                self._error(
                    issues,
                    code="day.index_mismatch",
                    path=f"{path}.day_index",
                    message="day_index 必须从 0 开始并与数组顺序一致",
                    repair_hint="按 days 数组顺序重新编号 day_index",
                    expected=position,
                    actual=day.day_index,
                )

            for field_name in ("description", "transportation", "accommodation"):
                value = getattr(day, field_name)
                if not value.strip():
                    self._error(
                        issues,
                        code=f"day.empty_{field_name}",
                        path=f"{path}.{field_name}",
                        message=f"每日行程的 {field_name} 不能为空",
                        repair_hint=f"补充该日的 {field_name}",
                        actual=value,
                    )

            if not day.attractions:
                self._warning(
                    issues,
                    code="day.no_attractions",
                    path=f"{path}.attractions",
                    message="当天没有安排景点",
                    repair_hint="如候选景点数据可用，建议安排 1-3 个景点",
                    actual=0,
                )
            elif len(day.attractions) > 4:
                self._warning(
                    issues,
                    code="day.too_many_attractions",
                    path=f"{path}.attractions",
                    message="当天景点数量较多，可能难以完成",
                    repair_hint="减少景点数量或拆分到其他日期",
                    expected="<= 4",
                    actual=len(day.attractions),
                )

            meal_types = {meal.type.strip().lower() for meal in day.meals}
            missing_meals = [
                name for name in ("breakfast", "lunch", "dinner")
                if name not in meal_types
            ]
            if missing_meals:
                self._warning(
                    issues,
                    code="day.meals_incomplete",
                    path=f"{path}.meals",
                    message="当天未包含完整的早、中、晚餐建议",
                    repair_hint="补充缺失餐次，或明确由用户自行安排",
                    expected=["breakfast", "lunch", "dinner"],
                    actual=sorted(meal_types),
                )

            for attraction_index, attraction in enumerate(day.attractions):
                attraction_path = f"{path}.attractions[{attraction_index}]"
                if attraction.visit_duration <= 0:
                    self._error(
                        issues,
                        code="attraction.invalid_visit_duration",
                        path=f"{attraction_path}.visit_duration",
                        message="景点游览时长必须大于 0",
                        repair_hint="填写合理的分钟数",
                        expected="> 0",
                        actual=attraction.visit_duration,
                    )
                longitude = attraction.location.longitude
                latitude = attraction.location.latitude
                if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
                    self._error(
                        issues,
                        code="attraction.invalid_location",
                        path=f"{attraction_path}.location",
                        message="景点经纬度超出合法范围",
                        repair_hint="使用候选 POI 数据中的真实经纬度",
                        expected={"longitude": "[-180, 180]", "latitude": "[-90, 90]"},
                        actual={"longitude": longitude, "latitude": latitude},
                    )

    def _validate_weather(
        self,
        plan: TripPlan,
        expected_dates: list[date],
        issues: list[ValidationIssue],
    ) -> None:
        weather_dates = [item.date for item in plan.weather_info]
        if len(weather_dates) != len(set(weather_dates)):
            self._warning(
                issues,
                code="weather.duplicate_dates",
                path="weather_info",
                message="天气信息中存在重复日期",
                repair_hint="每个日期只保留一条天气记录",
                actual=weather_dates,
            )
        if expected_dates:
            expected = {item.isoformat() for item in expected_dates}
            missing = sorted(expected - set(weather_dates))
            extra = sorted(set(weather_dates) - expected)
            if missing:
                self._warning(
                    issues,
                    code="weather.missing_dates",
                    path="weather_info",
                    message="部分旅行日期缺少天气信息",
                    repair_hint="从天气查询结果中补充缺失日期；若上游无数据则明确说明",
                    expected=sorted(expected),
                    actual=weather_dates,
                )
            if extra:
                self._warning(
                    issues,
                    code="weather.out_of_range_dates",
                    path="weather_info",
                    message="天气信息包含旅行范围外日期",
                    repair_hint="删除旅行范围外的天气记录",
                    actual=extra,
                )

    def _validate_budget(
        self,
        plan: TripPlan,
        issues: list[ValidationIssue],
    ) -> None:
        if plan.budget is None:
            self._warning(
                issues,
                code="budget.missing",
                path="budget",
                message="行程未提供预算汇总",
                repair_hint="根据门票、酒店、餐饮和交通估算预算",
            )
            return
        components = {
            "total_attractions": plan.budget.total_attractions,
            "total_hotels": plan.budget.total_hotels,
            "total_meals": plan.budget.total_meals,
            "total_transportation": plan.budget.total_transportation,
        }
        for name, value in components.items():
            if value < 0:
                self._error(
                    issues,
                    code="budget.negative_component",
                    path=f"budget.{name}",
                    message="预算分项不能为负数",
                    repair_hint="将预算修正为非负整数",
                    expected=">= 0",
                    actual=value,
                )
        expected_total = sum(components.values())
        if plan.budget.total != expected_total:
            self._warning(
                issues,
                code="budget.total_mismatch",
                path="budget.total",
                message="预算总额不等于各分项之和",
                repair_hint="重新计算 budget.total",
                expected=expected_total,
                actual=plan.budget.total,
            )

    def _validate_source_consistency(
        self,
        plan: TripPlan,
        attractions: dict[str, Any] | None,
        hotels: dict[str, Any] | None,
        issues: list[ValidationIssue],
    ) -> None:
        attraction_names = self._extract_names(attractions)
        hotel_names = self._extract_names(hotels)
        for day_index, day in enumerate(plan.days):
            for attraction_index, attraction in enumerate(day.attractions):
                if attraction_names and not self._matches_source(attraction.name, attraction_names):
                    self._error(
                        issues,
                        code="attraction.not_in_sources",
                        path=f"days[{day_index}].attractions[{attraction_index}].name",
                        message="景点未出现在已获取的候选 POI 数据中",
                        repair_hint="只使用景点搜索结果中的名称、地址和坐标",
                        expected=sorted(attraction_names),
                        actual=attraction.name,
                    )
            if day.hotel is not None and hotel_names and not self._matches_source(day.hotel.name, hotel_names):
                self._error(
                    issues,
                    code="hotel.not_in_sources",
                    path=f"days[{day_index}].hotel.name",
                    message="酒店未出现在已获取的候选酒店数据中",
                    repair_hint="只使用酒店搜索结果中的名称、地址和坐标",
                    expected=sorted(hotel_names),
                    actual=day.hotel.name,
                )

    def _validate_route_distances(
        self,
        plan: TripPlan,
        issues: list[ValidationIssue],
        route_estimates: dict[str, Any] | RouteEstimateResult | None,
    ) -> None:
        """Prefer real Amap metrics and fall back to Haversine when unavailable."""

        route_lookup = self._route_estimate_lookup(route_estimates)
        for day_position, day in enumerate(plan.days):
            for attraction_index in range(1, len(day.attractions)):
                previous = day.attractions[attraction_index - 1]
                current = day.attractions[attraction_index]
                leg_index = attraction_index - 1
                estimate = route_lookup.get((day.day_index, leg_index))
                path = f"days[{day_position}].attractions[{attraction_index}]"

                if (
                    estimate is not None
                    and estimate.available
                    and estimate.distance_meters is not None
                    and estimate.duration_seconds is not None
                ):
                    distance_km = estimate.distance_meters / 1000
                    duration_minutes = estimate.duration_seconds / 60
                    if estimate.duration_seconds > 7200:
                        self._error(
                            issues,
                            code="route.excessive_duration",
                            path=path,
                            message="\u540c\u65e5\u76f8\u90bb\u666f\u70b9\u7684\u771f\u5b9e\u4ea4\u901a\u65f6\u95f4\u8fc7\u957f",
                            repair_hint="\u8c03\u6574\u666f\u70b9\u987a\u5e8f\uff0c\u66ff\u6362\u8fc7\u8fdc\u666f\u70b9\uff0c\u6216\u5c06\u5176\u62c6\u5206\u5230\u5176\u4ed6\u65e5\u671f",
                            expected="<= 120 minutes",
                            actual={
                                "duration_minutes": round(duration_minutes),
                                "distance_km": round(distance_km, 1),
                                "mode": estimate.mode,
                                "source": "amap",
                            },
                        )
                    if distance_km > 80:
                        self._warning(
                            issues,
                            code="route.long_transfer",
                            path=path,
                            message="\u540c\u65e5\u8fde\u7eed\u666f\u70b9\u771f\u5b9e\u8def\u7ebf\u8ddd\u79bb\u8fc7\u8fdc",
                            repair_hint="\u8c03\u6574\u666f\u70b9\u987a\u5e8f\u6216\u5c06\u8fdc\u8ddd\u79bb\u666f\u70b9\u62c6\u5206\u5230\u5176\u4ed6\u65e5\u671f",
                            expected="<= 80 km",
                            actual={
                                "distance_km": round(distance_km, 1),
                                "duration_minutes": round(duration_minutes),
                                "mode": estimate.mode,
                                "source": "amap",
                            },
                        )
                    continue

                if estimate is not None and not estimate.available:
                    self._warning(
                        issues,
                        code="route.unavailable",
                        path=path,
                        message="\u9ad8\u5fb7\u672a\u8fd4\u56de\u8be5\u8def\u7ebf\u6bb5\u7684\u53ef\u7528\u65b9\u6848\uff0c\u5df2\u964d\u7ea7\u4e3a\u76f4\u7ebf\u8ddd\u79bb\u6821\u9a8c",
                        repair_hint="\u68c0\u67e5\u666f\u70b9\u5750\u6807\u548c\u4ea4\u901a\u65b9\u5f0f\uff0c\u6216\u7a0d\u540e\u91cd\u8bd5\u8def\u7ebf\u67e5\u8be2",
                        actual={
                            "error_code": estimate.error_code,
                            "error_message": estimate.error_message,
                        },
                    )

                distance_km = self._haversine_km(
                    previous.location.latitude,
                    previous.location.longitude,
                    current.location.latitude,
                    current.location.longitude,
                )
                if distance_km > 80:
                    self._warning(
                        issues,
                        code="route.long_transfer",
                        path=path,
                        message="\u540c\u65e5\u8fde\u7eed\u666f\u70b9\u8ddd\u79bb\u8fc7\u8fdc\uff0c\u8def\u7ebf\u53ef\u80fd\u4e0d\u5408\u7406",
                        repair_hint="\u8c03\u6574\u666f\u70b9\u987a\u5e8f\u6216\u5c06\u8fdc\u8ddd\u79bb\u666f\u70b9\u62c6\u5206\u5230\u5176\u4ed6\u65e5\u671f",
                        expected="<= 80 km",
                        actual={
                            "distance_km": round(distance_km, 1),
                            "source": "haversine",
                        },
                    )

    @staticmethod
    def _validate_schedule_capacity(
        issues: list[ValidationIssue],
        schedule_quality_report: ScheduleQualityReport | dict | None,
    ) -> None:
        """Expose unresolved daily overtime to the existing repair loop."""

        if schedule_quality_report is None:
            return
        try:
            report = (
                schedule_quality_report
                if isinstance(schedule_quality_report, ScheduleQualityReport)
                else ScheduleQualityReport.model_validate(schedule_quality_report)
            )
        except Exception:
            return
        for day in report.days:
            if day.overtime_minutes <= 0:
                continue
            TripPlanValidator._error(
                issues,
                code="schedule.daily_overtime",
                path=f"days[{day.day_index}].attractions",
                message=(
                    f"第 {day.day_index + 1} 天的日程超出可用时间 {day.overtime_minutes} 分钟"
                ),
                repair_hint=(
                    "减少当日景点、缩短游览时间，或将部分景点移动到其他日期"
                ),
                expected={"overtime_minutes": 0},
                actual={
                    "overtime_minutes": day.overtime_minutes,
                    "total_required_minutes": day.total_required_minutes,
                    "available_minutes": day.available_minutes,
                },
            )

    @staticmethod
    def _validate_execution_constraints(
        issues: list[ValidationIssue],
        constraint_report: TripConstraintReport | dict | None,
    ) -> None:
        """Expose unresolved feasibility conflicts to the existing repair loop."""

        if constraint_report is None:
            return
        try:
            report = (
                constraint_report
                if isinstance(constraint_report, TripConstraintReport)
                else TripConstraintReport.model_validate(constraint_report)
            )
        except Exception:
            return
        for item in report.issues:
            issue = ValidationIssue(
                code=item.code,
                severity=(
                    ValidationSeverity.ERROR
                    if item.severity == "error"
                    else ValidationSeverity.WARNING
                ),
                path=item.path,
                message=item.message,
                repair_hint=item.repair_hint,
                repairable=item.repairable,
                expected=item.expected,
                actual=item.actual,
            )
            issues.append(issue)

    @staticmethod
    def _route_estimate_lookup(
        route_estimates: dict[str, Any] | RouteEstimateResult | None,
    ) -> dict[tuple[int, int], RouteEstimate]:
        if route_estimates is None:
            return {}
        try:
            result = (
                route_estimates
                if isinstance(route_estimates, RouteEstimateResult)
                else RouteEstimateResult.model_validate(route_estimates)
            )
        except Exception:
            return {}
        return {(item.day_index, item.leg_index): item for item in result.routes}

    @staticmethod
    def _parse_date(
        value: str,
        issues: list[ValidationIssue],
        *,
        path: str,
        code: str,
        repairable: bool = True,
    ) -> date | None:
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError):
            TripPlanValidator._error(
                issues,
                code=code,
                path=path,
                message="日期必须使用 YYYY-MM-DD 格式",
                repair_hint=(
                    "修正用户请求日期后重新执行"
                    if not repairable
                    else "根据用户请求改为合法的 YYYY-MM-DD 日期"
                ),
                repairable=repairable,
                expected="YYYY-MM-DD",
                actual=value,
            )
            return None

    @staticmethod
    def _extract_names(payload: Any) -> set[str]:
        names: set[str] = set()
        if isinstance(payload, dict):
            value = payload.get("name")
            if isinstance(value, str) and value.strip():
                names.add(value.strip())
            for child in payload.values():
                names.update(TripPlanValidator._extract_names(child))
        elif isinstance(payload, list):
            for child in payload:
                names.update(TripPlanValidator._extract_names(child))
        return names

    @classmethod
    def _matches_source(cls, value: str, candidates: set[str]) -> bool:
        normalized = cls._normalize_place(value)
        return any(
            normalized == cls._normalize_place(candidate)
            or normalized in cls._normalize_place(candidate)
            or cls._normalize_place(candidate) in normalized
            for candidate in candidates
        )

    @staticmethod
    def _normalize_place(value: str) -> str:
        normalized = "".join(value.lower().split())
        for suffix in ("特别行政区", "维吾尔自治区", "壮族自治区", "回族自治区", "自治区", "省", "市"):
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)]
                break
        return normalized

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        radius = 6371.0
        d_lat = radians(lat2 - lat1)
        d_lon = radians(lon2 - lon1)
        a = (
            sin(d_lat / 2) ** 2
            + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
        )
        return 2 * radius * asin(sqrt(a))

    @staticmethod
    def _error(
        issues: list[ValidationIssue],
        *,
        code: str,
        path: str,
        message: str,
        repair_hint: str,
        repairable: bool = True,
        expected: Any = None,
        actual: Any = None,
    ) -> None:
        issues.append(
            ValidationIssue(
                code=code,
                severity=ValidationSeverity.ERROR,
                path=path,
                message=message,
                repair_hint=repair_hint,
                repairable=repairable,
                expected=expected,
                actual=actual,
            )
        )

    @staticmethod
    def _warning(
        issues: list[ValidationIssue],
        *,
        code: str,
        path: str,
        message: str,
        repair_hint: str,
        expected: Any = None,
        actual: Any = None,
    ) -> None:
        issues.append(
            ValidationIssue(
                code=code,
                severity=ValidationSeverity.WARNING,
                path=path,
                message=message,
                repair_hint=repair_hint,
                expected=expected,
                actual=actual,
            )
        )
