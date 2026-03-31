from .base import BaseAgent
from ..prompts.agent_prompts import ATTRACTION_AGENT_PROMPT
from ..tools.amap_tools import AmapTools, parse_tool_call


class AttractionAgent(BaseAgent):
    """景点搜索专家智能体"""

    def __init__(self):
        super().__init__(ATTRACTION_AGENT_PROMPT)

    def search_attractions(self, city: str, preferences: list) -> dict:
        """搜索景点（自动调用工具）"""
        # 1. 生成搜索指令
        keywords = ",".join(preferences) if preferences else "景点"
        input_text = f"搜索{city}的{keywords}景点"
        response = self.invoke(input_text)

        # 2. 解析并调用工具
        tool_name, params = parse_tool_call(response)
        if tool_name == "amap_maps_text_search":
            return AmapTools.text_search(**params)
        return {"error": "景点搜索失败"}