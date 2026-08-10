"""Deterministic normalization applied to every LLM-produced trip plan."""

from __future__ import annotations

from app.schemas.trip_schema import Attraction, TripPlan


def attraction_identity(attraction: Attraction) -> str:
    """Return a stable POI identity, falling back to a normalized name."""

    poi_id = str(attraction.poi_id or "").strip().lower()
    if poi_id:
        return f"poi:{poi_id}"
    name = "".join(str(attraction.name or "").lower().split())
    return f"name:{name}"


def remove_duplicate_attractions(plan: TripPlan) -> tuple[TripPlan, list[tuple[str, str]]]:
    """Keep the first occurrence of each attraction and remove later duplicates.

    The LLM may reintroduce duplicates during either initial generation or repair.
    Normalizing immediately keeps route queries and later deterministic evaluators from
    spending budget on a plan that is already known to be invalid.
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
