# app/core/llm.py
from langchain_openai import ChatOpenAI
from app.core.config import settings

def get_llm():
    """获取统一的 LLM 实例（支持 OpenAI 兼容 API）"""
    return ChatOpenAI(
        model=settings.LLM_MODEL_ID,
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        timeout=settings.LLM_TIMEOUT,
        temperature=0.0,  # 规划类任务建议低温度
    )