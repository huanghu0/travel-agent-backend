from .base import BaseAgent
from ..prompts.agent_prompts import WEATHER_AGENT_PROMPT
from ..tools.amap_tools import AmapTools, parse_tool_call


class WeatherAgent(BaseAgent):
    """天气查询专家智能体"""

    def __init__(self):
        super().__init__(WEATHER_AGENT_PROMPT)

    def get_city_weather(self, city: str) -> dict:
        """查询天气（自动调用工具）"""
        input_text = f"查询{city}天气"
        response = self.invoke(input_text)

        tool_name, params = parse_tool_call(response)
        if tool_name == "amap_maps_weather":
            return AmapTools.get_weather(**params)
        return {"error": "天气查询失败"}