import os
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv

load_dotenv()


# 初始化LLM（兼容OpenAI协议）
def get_llm():
    return ChatOpenAI(
        model=os.getenv("LLM_MODEL_ID"),
        api_key=os.getenv("LLM_API_KEY"),
        base_url=os.getenv("LLM_BASE_URL"),
        timeout=int(os.getenv("LLM_TIMEOUT", 60)),
        temperature=0.1
    )


# 智能体基类
class BaseAgent:
    def __init__(self, prompt: str):
        self.llm = get_llm()
        self.prompt = prompt

    def invoke(self, input_text: str) -> str:
        """调用智能体"""
        messages = [
            ("system", self.prompt),
            ("user", input_text)
        ]
        return self.llm.invoke(messages).content