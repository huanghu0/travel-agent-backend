import json
from .base import BaseAgent
from ..prompts.agent_prompts import PLANNER_AGENT_PROMPT
from pydantic import ValidationError

from ..schemas.trip_schema import TripPlan, TripRequest


def _extract_json_object(response: str) -> dict:
    """Extract the first complete JSON object from a model response."""
    cleaned = response.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[len("```json"):].strip()
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:].strip()

    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].strip()

    object_start = cleaned.find("{")
    if object_start < 0:
        raise ValueError("Model response does not contain a JSON object")

    value, _ = json.JSONDecoder().raw_decode(cleaned[object_start:])
    if not isinstance(value, dict):
        raise ValueError("Trip plan JSON must be an object")
    return value


class PlannerAgent(BaseAgent):
    def __init__(self):
        super().__init__(PLANNER_AGENT_PROMPT)

    def generate_plan(self, request: TripRequest, attractions: dict, weather: dict, hotels: dict) -> dict:
        input_info = f"""
        你是专业旅行规划师，根据以下信息生成旅行计划。
        必须严格返回JSON，不要返回任何多余文字、解释、markdown。
        返回格式必须是标准JSON，不能有任何注释。

        用户请求：
        {request.model_dump_json()}

        景点信息：
        {json.dumps(attractions, ensure_ascii=False)}

        天气信息：
        {json.dumps(weather, ensure_ascii=False)}

        酒店信息：
        {json.dumps(hotels, ensure_ascii=False)}

        按照你收到的格式返回。
        """

        response = self.invoke(input_info)

        try:
            parsed = _extract_json_object(response)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(
                "Trip plan JSON parsing failed. The model output may be truncated "
                f"or malformed: {exc}"
            ) from exc

        try:
            return TripPlan.model_validate(parsed).model_dump()
        except ValidationError as exc:
            raise ValueError(
                f"Trip plan structure validation failed: {exc}"
            ) from exc
