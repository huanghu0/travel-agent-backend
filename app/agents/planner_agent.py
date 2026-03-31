import json
from .base import BaseAgent
from ..prompts.agent_prompts import PLANNER_AGENT_PROMPT
from ..schemas.trip_schema import TripRequest


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
            # 暴力清洗，确保能转JSON
            response = response.strip()
            if "```json" in response:
                response = response.split("```json")[1].split("```")[0].strip()
            if "```" in response:
                response = response.replace("```", "").strip()

            return json.loads(response)
        except Exception as e:
            return {
                "error": "行程解析失败",
                "reason": str(e),
                "raw": response[:500]
            }