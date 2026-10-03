"""A scripted stand-in for the API, so no test needs a key or a network."""
import json
from types import SimpleNamespace


class FakeBackend:
    """Returns the queued replies in order and records every request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def create(self, model, max_tokens, messages, **extra):
        self.requests.append({"messages": messages, **extra})
        reply = self.replies.pop(0)
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5))
