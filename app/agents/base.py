"""模型驱动 Agent 的公共调用封装。"""

from app.core.llm import get_llm


class BaseAgent:
    """提示词驱动型 Agent 的公共基类。"""

    def __init__(self, prompt: str, protocol: str | None = None):
        self.llm = get_llm(protocol)
        self.prompt = prompt

    def invoke(
        self,
        input_text: str,
        response_model: type | None = None,
    ) -> str:
        """通过当前配置的 LLM 协议调用模型。"""
        return self.llm.invoke(
            instructions=self.prompt,
            input_text=input_text,
            response_model=response_model,
        )
