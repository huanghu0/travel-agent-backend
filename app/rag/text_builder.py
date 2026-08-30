"""Deterministic, privacy-bounded text for one whole guide vector and queries."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence

from app.schemas.trip_schema import TripRequest
from app.sharing.models import SharedGuideSnapshot

from .models import BuiltRetrievalText


_TAG_RE = re.compile(r"<[^>]*>")
_INVISIBLE_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_CITY_SUFFIXES = ("市", "市区")
_TRANSPORT_ALIASES = {"公交": "公共交通", "地铁": "公共交通", "驾车": "自驾", "驾驶": "自驾"}


def _clean(value: object, limit: int) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _TAG_RE.sub("", text)
    text = _INVISIBLE_RE.sub("", text)
    return " ".join(text.split())[:limit]


def _city(value: object) -> str:
    result = _clean(value, 128)
    for suffix in _CITY_SUFFIXES:
        if result.endswith(suffix) and len(result) > len(suffix):
            return result[: -len(suffix)]
    return result


def _transport(value: object) -> str:
    result = _clean(value, 64)
    return _TRANSPORT_ALIASES.get(result, result)


def _unique(values: Sequence[str], *, limit: int, sort: bool = False) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _clean(value, limit)
        if item and item not in seen:
            seen.add(item)
            cleaned.append(item)
    return sorted(cleaned) if sort else cleaned


def _join(values: Sequence[str]) -> str:
    return "、".join(values) or "无"


class EmbeddingTextBuilder:
    TEMPLATE_VERSION = "retrieval_template_v1"
    MAX_TEXT_LENGTH = 12000
    MAX_DAILY_SUMMARY_LENGTH = 500
    MAX_DAILY_ATTRACTIONS = 60

    def build_document(self, snapshot: SharedGuideSnapshot) -> BuiltRetrievalText:
        request = snapshot.request
        plan = snapshot.trip_plan
        city = _city(request.city or plan.city)
        transportation = _transport(request.transportation)
        accommodation = _clean(request.accommodation, 128)
        preferences = _unique(request.preferences, limit=64, sort=True)[:20]

        attractions: list[str] = []
        summaries: list[str] = []
        for index, day in enumerate(plan.days[:30], start=1):
            names = _unique([attraction.name for attraction in day.attractions], limit=100)[
                : self.MAX_DAILY_ATTRACTIONS
            ]
            attractions.extend(names)
            prefix = f"第{index}天："
            summary_limit = self.MAX_DAILY_SUMMARY_LENGTH - len(prefix)
            summary = _clean(day.description, summary_limit)
            if not summary:
                summary = self._fallback_summary(names, summary_limit)
            summaries.append(f"{prefix}{summary}")
        attractions = _unique(attractions, limit=100)[:60]
        suggestions = _clean(plan.overall_suggestions, 1200)
        text = self._assemble(city, plan.days and len(plan.days) or request.travel_days, transportation, accommodation, preferences, attractions, summaries, suggestions)
        return BuiltRetrievalText(
            text=text,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            city_normalized=city,
            transportation_normalized=transportation,
            template_version=self.TEMPLATE_VERSION,
        )

    def build_query(self, request: TripRequest, *, selected_attractions: Sequence[str] = ()) -> str:
        city = _city(request.city)
        transportation = _transport(request.transportation)
        accommodation = _clean(request.accommodation, 128)
        preferences = _unique(request.preferences, limit=64, sort=True)[:20]
        selected = _unique(selected_attractions, limit=100)[:60]
        lines = [
            "文档类型：旅行攻略检索请求",
            f"目的地：{city}",
            f"旅行天数：{request.travel_days}天",
            f"主要交通：{transportation}",
            f"住宿偏好：{accommodation}",
            f"旅行偏好：{_join(preferences)}",
        ]
        if selected:
            lines.append(f"用户明确选择景点：{_join(selected)}")
        extra = _clean(request.free_text_input, 1000)
        if extra:
            lines.append(f"额外要求：{extra}")
        return self._truncate("\n".join(lines))

    def _assemble(self, city: str, days: int, transportation: str, accommodation: str, preferences: list[str], attractions: list[str], summaries: list[str], suggestions: str) -> str:
        text = "\n".join([
            "文档类型：公开旅行攻略",
            f"目的地：{city}",
            f"旅行天数：{days}天",
            f"主要交通：{transportation}",
            f"住宿偏好：{accommodation}",
            f"旅行偏好：{_join(preferences)}",
            f"主要景点：{_join(attractions)}",
            "",
            "每日摘要：",
            *summaries,
            "",
            "总体建议：",
            suggestions,
        ])
        return self._truncate(text)

    @staticmethod
    def _fallback_summary(names: Sequence[str], limit: int) -> str:
        if not names:
            return "暂无景点安排。"
        selected: list[str] = []
        length = len("游览") + len("。")
        for name in names:
            separator_length = len("、") if selected else 0
            if length + separator_length + len(name) > limit:
                break
            selected.append(name)
            length += separator_length + len(name)
        return f"游览{'、'.join(selected)}。" if selected else "暂无景点安排。"

    def _truncate(self, text: str) -> str:
        if len(text) <= self.MAX_TEXT_LENGTH:
            return text
        return text[: self.MAX_TEXT_LENGTH]
