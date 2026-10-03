"""
The model layer: one call, parsed as JSON, retried once, spend tallied.

Descended from the job-search pipeline's request_json, with one structural
change: the client is created on first use and can be replaced. Every test in
this project swaps in a scripted stand-in through `use_backend`, so the suite
runs with no key and no network, and nothing here can reach the API by
accident during a test.
"""
import json
import os
import re

DEFAULT_MODEL = "claude-sonnet-5-5"

_backend = None


def model() -> str:
    return os.environ.get("RESUME_BUILDER_MODEL", DEFAULT_MODEL)


def use_backend(backend) -> None:
    """Replace the API client. A backend needs one method,
    `create(model, max_tokens, messages, **extra)`, returning an object shaped
    like an Anthropic Message (content blocks, stop_reason, usage)."""
    global _backend
    _backend = backend


def _client():
    global _backend
    if _backend is None:
        import anthropic
        _backend = anthropic.Anthropic().messages
    return _backend


def extract_text(resp) -> str:
    parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    if not parts:
        raise ValueError(f"No text block in response (stop_reason={resp.stop_reason})")
    return "\n".join(parts)


def parse_json_response(text: str) -> dict:
    """Pull out the outermost {...}, tolerating code fences, // comments and
    trailing commas, so one stray character does not cost a document."""
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        cleaned = cleaned[start:end + 1]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        stripped = re.sub(r"(?<!:)//[^\n]*", "", cleaned)
        stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
        return json.loads(stripped)


_SPEND = {"in": 0, "out": 0, "calls": 0}
_RATES = {"claude-sonnet-5-5": (2.00, 10.00), "claude-sonnet-5": (2.00, 10.00),
          "claude-opus-5-5": (5.00, 25.00), "claude-haiku-4-5": (1.00, 5.00)}


def spend_summary() -> str:
    if not _SPEND["calls"]:
        return ""
    in_rate, out_rate = _RATES.get(model(), (2.00, 10.00))
    cost = _SPEND["in"] / 1e6 * in_rate + _SPEND["out"] / 1e6 * out_rate
    return (f"{_SPEND['calls']} model call(s), {_SPEND['in']:,} in / "
            f"{_SPEND['out']:,} out, about ${cost:.2f}")


def request_json(messages: list, max_tokens: int, what: str,
                 schema: dict | None = None, attempts: int = 2) -> dict:
    """
    Call the model and return its JSON. One corrective pass when the answer
    does not parse; none when it was truncated, because asking again with the
    same budget truncates again.

    `schema` makes the shape a constraint the API enforces. Use it wherever the
    prompt also has a conversational job, or the model does the human half and
    drops the JSON.
    """
    last = None
    for attempt in range(attempts):
        extra = ({"output_config": {"format": {"type": "json_schema", "schema": schema}}}
                 if schema else {})
        resp = _client().create(model=model(), max_tokens=max_tokens,
                                messages=messages, **extra)
        usage = getattr(resp, "usage", None)
        _SPEND["calls"] += 1
        _SPEND["in"] += getattr(usage, "input_tokens", 0) or 0
        _SPEND["out"] += getattr(usage, "output_tokens", 0) or 0
        text = extract_text(resp).strip()
        if resp.stop_reason == "max_tokens":
            raise ValueError(f"{what}: response truncated at {max_tokens} tokens; "
                             f"raise max_tokens")
        try:
            return parse_json_response(text)
        except json.JSONDecodeError as exc:
            last = exc
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content":
                 f"That is not valid JSON ({exc}). Send the same content again "
                 f"as strictly valid JSON and nothing else."}]
    raise last


def require_credentials() -> None:
    """Fail before any work starts if the SDK has nothing to authenticate with."""
    if _backend is not None:
        return
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return
    raise SystemExit(
        "No Anthropic API key found. Set ANTHROPIC_API_KEY and run again.\n"
        "  Windows (PowerShell):  setx ANTHROPIC_API_KEY \"sk-ant-...\"  then open a new window\n"
        "  macOS / Linux:         export ANTHROPIC_API_KEY=sk-ant-...")
