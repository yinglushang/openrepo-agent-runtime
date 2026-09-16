from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langchain_core.tools import BaseTool


class ScriptedModel:
    provider = "test"
    model_name = "scripted-model"

    def __init__(self, calls: list[dict[str, object]]) -> None:
        self.calls = calls

    async def invoke(
        self,
        messages: Sequence[AnyMessage],
        tools: Sequence[BaseTool],
    ) -> AIMessage:
        del tools
        last = messages[-1]
        if isinstance(last, ToolMessage):
            return AIMessage(content=f"finished:{last.status}:{last.content}")
        return AIMessage(content="", tool_calls=self.calls)
