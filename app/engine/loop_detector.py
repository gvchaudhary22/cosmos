"""
Loop Detector for COSMOS ReAct engine.

Detects infinite tool-call loops using a sliding window over the recent
tool-call history. If the same tool appears THRESHOLD or more times within
the last WINDOW calls, a loop is declared and the caller should break out
of the ReAct iteration.
"""


class LoopError(Exception):
    """Raised when a tool-call loop is detected."""


class LoopDetector:
    """
    Sliding-window loop detector for ReAct tool calls.

    Attributes:
        WINDOW    -- number of recent tool calls to inspect (default 8)
        THRESHOLD -- minimum occurrences of a single tool within the
                     window that constitutes a loop (default 3)
    """

    WINDOW = 8
    THRESHOLD = 3

    def __init__(self) -> None:
        self._history: list[str] = []

    def record(self, tool_name: str) -> None:
        """Record a tool call in the rolling history buffer."""
        self._history.append(tool_name)

    def is_loop(self) -> tuple[bool, str]:
        """
        Check whether a loop exists in the recent call history.

        Returns:
            (True, tool_name)  — if any tool appears >= THRESHOLD times
                                 within the last WINDOW calls.
            (False, "")        — no loop detected.
        """
        window = self._history[-self.WINDOW :]
        counts: dict[str, int] = {}
        for name in window:
            counts[name] = counts.get(name, 0) + 1
            if counts[name] >= self.THRESHOLD:
                return True, name
        return False, ""

    def reset(self) -> None:
        """Clear history. Call at the start of each new request."""
        self._history = []
