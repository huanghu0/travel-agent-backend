# app/core/config.py
import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # LLM 配置
    LLM_MODEL_ID: str = os.getenv("LLM_MODEL_ID", "gpt-4o")
    LLM_API_KEY: str = os.getenv("LLM_API_KEY")
    LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    LLM_TIMEOUT: int = int(os.getenv("LLM_TIMEOUT", "60"))

    # 高德地图
    AMAP_API_KEY: str = os.getenv("AMAP_API_KEY")

    # Unsplash
    UNSPLASH_ACCESS_KEY: str = os.getenv("UNSPLASH_ACCESS_KEY")
    UNSPLASH_SECRET_KEY: str = os.getenv("UNSPLASH_SECRET_KEY")


settings = Settings()