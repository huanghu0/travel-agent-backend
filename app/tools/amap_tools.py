import os
import requests
from dotenv import load_dotenv
from typing import Dict, Any

load_dotenv()
AMAP_KEY = os.getenv("AMAP_API_KEY")


class AmapTools:
    """高德地图API工具类"""

    @staticmethod
    def text_search(keywords: str, city: str) -> Dict[str, Any]:
        """文本搜索（景点/酒店）"""
        url = "https://restapi.amap.com/v3/place/text"
        params = {
            "key": AMAP_KEY,
            "keywords": keywords,
            "city": city,
            "output": "json",
            "page_size": 10
        }
        response = requests.get(url, params=params)
        return response.json()

    @staticmethod
    def get_weather(city: str) -> Dict[str, Any]:
        """查询城市天气"""
        url = "https://restapi.amap.com/v3/weather/weatherInfo"
        params = {
            "key": AMAP_KEY,
            "city": city,
            "output": "json",
            "extensions": "all"  # 获取预报天气
        }
        response = requests.get(url, params=params)
        return response.json()


# 工具调用解析函数（解析智能体返回的工具指令）
def parse_tool_call(response: str):
    """解析智能体返回的工具调用指令"""
    import re
    pattern = r"\[TOOL_CALL:(.*?):(.*?)\]"
    match = re.search(pattern, response)
    if match:
        tool_name = match.group(1)
        params_str = match.group(2)
        params = {}
        for param in params_str.split(","):
            k, v = param.split("=")
            params[k.strip()] = v.strip()
        return tool_name, params
    return None, None