"""餐饮营业时间解析和确定性餐次时间推导。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal


OpeningStatus = Literal["open", "closed", "unknown"]

_CLOCK_RANGE = re.compile(
    r"(?P<start_h>[0-2]?\d):(?P<start_m>[0-5]\d)\s*"
    r"[-~～—–至]\s*"
    r"(?P<end_h>[0-2]?\d):(?P<end_m>[0-5]\d)"
)
_UNKNOWN_MARKERS = ("暂无", "未知", "待确认", "未提供")


@dataclass(frozen=True)
class MealServiceInterval:
    """一个餐次预计占用的连续时间区间。"""

    meal_type: str
    start_minute: int
    end_minute: int

    @property
    def start_time(self) -> str:
        return format_clock(self.start_minute)

    @property
    def end_time(self) -> str:
        return format_clock(self.end_minute)


def parse_clock(value: str) -> int:
    """解析 HH:MM；允许时间轴使用 24 点之后的超时小时。"""

    parts = str(value or "").strip().split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid clock value: {value!r}")
    hour, minute = (int(part) for part in parts)
    if hour < 0 or not 0 <= minute <= 59:
        raise ValueError(f"Invalid clock value: {value!r}")
    return hour * 60 + minute


def format_clock(total_minutes: int) -> str:
    """格式化分钟数，并保留跨日后的 24 点以上小时。"""

    hours, minutes = divmod(max(0, total_minutes), 60)
    return f"{hours:02d}:{minutes:02d}"


def parse_opening_ranges(opening_hours: str) -> list[tuple[int, int]]:
    """把高德常见营业时间文本转换为分钟区间。

    跨午夜区间会把结束时间扩展到下一自然日，例如 18:00-02:00
    转换为 (1080, 1560)。无法可靠解析时返回空列表，表示 unknown。
    """

    text = str(opening_hours or "").strip()
    if not text or any(marker in text for marker in _UNKNOWN_MARKERS):
        return []
    compact = "".join(text.lower().split())
    if any(marker in compact for marker in ("24小时", "全天", "24h")):
        return [(0, 24 * 60)]

    ranges: list[tuple[int, int]] = []
    for match in _CLOCK_RANGE.finditer(text):
        start_h = int(match.group("start_h"))
        start_m = int(match.group("start_m"))
        end_h = int(match.group("end_h"))
        end_m = int(match.group("end_m"))
        if start_h > 24 or end_h > 24:
            continue
        if (start_h == 24 and start_m != 0) or (end_h == 24 and end_m != 0):
            continue
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        if start == end:
            # 00:00-00:00 在供应商数据中通常表示全天营业。
            if start == 0:
                ranges.append((0, 24 * 60))
            continue
        if end < start:
            end += 24 * 60
        ranges.append((start, end))
    return ranges


def opening_status_for_interval(
    opening_hours: str,
    start_minute: int,
    end_minute: int,
) -> OpeningStatus:
    """判断完整用餐区间是否落在任一已知营业区间内。"""

    ranges = parse_opening_ranges(opening_hours)
    if not ranges:
        return "unknown"
    if end_minute <= start_minute:
        return "closed"

    # 同时比较当天区间和整体平移一天后的区间，兼容跨午夜营业文本。
    for opening, closing in ranges:
        for offset in (0, 24 * 60):
            if start_minute >= opening + offset and end_minute <= closing + offset:
                return "open"
    return "closed"


def meal_service_intervals(schedule_day: Any | None) -> dict[str, MealServiceInterval]:
    """从已有时间轴推导早餐、午餐、晚餐预计时间，不改变日程容量评分。"""

    breakfast = MealServiceInterval("breakfast", 8 * 60, 8 * 60 + 45)
    lunch_start = 12 * 60
    lunch_end = 13 * 60
    last_end = 18 * 60

    timeline = getattr(schedule_day, "timeline", None)
    if timeline is None and isinstance(schedule_day, dict):
        timeline = schedule_day.get("timeline")
    if isinstance(timeline, list):
        for item in timeline:
            item_type = getattr(item, "item_type", None)
            start_time = getattr(item, "start_time", None)
            end_time = getattr(item, "end_time", None)
            if isinstance(item, dict):
                item_type = item.get("item_type")
                start_time = item.get("start_time")
                end_time = item.get("end_time")
            try:
                start = parse_clock(str(start_time))
                end = parse_clock(str(end_time))
            except (TypeError, ValueError):
                continue
            last_end = max(last_end, end)
            if item_type == "meal":
                lunch_start, lunch_end = start, end

    dinner_start = max(18 * 60, last_end)
    return {
        "breakfast": breakfast,
        "lunch": MealServiceInterval("lunch", lunch_start, lunch_end),
        "dinner": MealServiceInterval("dinner", dinner_start, dinner_start + 60),
    }


def format_opening_ranges(opening_hours: str) -> list[str]:
    """输出适合约束报告展示的营业区间。"""

    return [
        f"{format_clock(start)}-{format_clock(end)}"
        for start, end in parse_opening_ranges(opening_hours)
    ]
