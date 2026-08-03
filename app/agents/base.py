from app.core.llm import get_llm


class BaseAgent:
    """Base class for prompt-driven agents."""

    def __init__(self, prompt: str, protocol: str | None = None):
        self.llm = get_llm(protocol)
        self.prompt = prompt

    def invoke(self, input_text: str) -> str:
        """Invoke the agent through the configured LLM protocol."""
        return self.llm.invoke(
            instructions=self.prompt,
            input_text=input_text,
        )
