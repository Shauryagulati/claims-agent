from __future__ import annotations

from app.llm import LLMError
from app.memory import Extraction


class FakeLLM:
    """Scripted stand-in for app.llm.LLM. Records every call."""

    def __init__(
        self,
        extractions: list[Extraction] | None = None,
        replies: list[str] | None = None,
        fail_respond: bool = False,
        smoothed: str | None = None,
        fail_smooth: bool = False,
    ) -> None:
        self.extractions = list(extractions or [])
        self.replies = list(replies or [])
        self.fail_respond = fail_respond
        self.smoothed = smoothed
        self.fail_smooth = fail_smooth
        self.calls: list[tuple[str, dict]] = []

    def extract(self, phase, history, text):
        self.calls.append(("extract", {"phase": phase, "history": history, "text": text}))
        if not self.extractions:
            raise AssertionError("FakeLLM ran out of scripted extractions")
        return self.extractions.pop(0)

    def respond(self, phase, history, text, plan_text, log_context=""):
        self.calls.append(("respond", {"phase": phase, "history": history, "text": text,
                                       "plan_text": plan_text, "log_context": log_context}))
        if self.fail_respond:
            raise LLMError("scripted failure")
        return self.replies.pop(0) if self.replies else f"[reply in {phase.value}]"

    def smooth(self, text):
        self.calls.append(("smooth", {"text": text}))
        if self.fail_smooth:
            raise LLMError("scripted smoothing failure")
        return self.smoothed if self.smoothed is not None else text
