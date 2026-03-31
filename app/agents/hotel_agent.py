from .base import BaseAgent
from ..prompts.agent_prompts import HOTEL_AGENT_PROMPT
from ..tools.amap_tools import AmapTools, parse_tool_call


class HotelAgent(BaseAgent):
    """酒店推荐专家智能体"""

    def __init__(self):
        super().__init__(HOTEL_AGENT_PROMPT)

    def search_hotels(self, city: str) -> dict:
        """搜索酒店（自动调用工具）"""
        input_text = f"搜索{city}的酒店"
        response = self.invoke(input_text)

        tool_name, params = parse_tool_call(response)
        if tool_name == "amap_maps_text_search":
            return AmapTools.text_search(**params)
        return {"error": "酒店搜索失败"}