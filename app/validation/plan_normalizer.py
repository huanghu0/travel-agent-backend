"""对每份 LLM 行程结果执行的确定性标准化。"""

from __future__ import annotations

from app.schemas.trip_schema import Attraction, TripPlan


def attraction_identity(attraction: Attraction) -> str:
    """优先使用 POI ID 生成稳定标识，缺失时回退到标准化名称。"""

    poi_id = str(attraction.poi_id or "").strip().lower()
    if poi_id:
        return f"poi:{poi_id}"
    name = "".join(str(attraction.name or "").lower().split())
    return f"name:{name}"


def remove_duplicate_attractions(plan: TripPlan) -> tuple[TripPlan, list[tuple[str, str]]]:
    """保留每个景点的首次出现，并移除后续重复项。

    LLM 在首次生成或修复时都可能重新引入重复景点。尽早标准化可以避免
    路线查询和后续确定性评估器在已知无效的行程上继续消耗预算。
    """

    normalized = plan.model_copy(deep=True)
    seen: set[str] = set()
    removed: list[tuple[str, str]] = []
    for day_position, day in enumerate(normalized.days):
        unique = []
        for attraction_position, attraction in enumerate(day.attractions):
            identity = attraction_identity(attraction)
            if identity in seen:
                removed.append(
                    (
                        attraction.name,
                        f"days[{day_position}].attractions[{attraction_position}]",
                    )
                )
                continue
            seen.add(identity)
            unique.append(attraction)
        day.attractions = unique
    return normalized, removed
