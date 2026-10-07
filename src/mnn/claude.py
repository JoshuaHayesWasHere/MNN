"""One request to Claude that comes back as JSON of a given shape.

The sources that have Claude write for the paper (the editor, the day) share
this. It needs `ANTHROPIC_API_KEY` in the environment, and every way the
request can fail is raised as one error with a plain reason, so a source can
print something else and note why.
"""

from __future__ import annotations

import json
import os

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
EFFORTS = ("low", "medium", "high")
MAX_TOKENS = 16000
KEY_ENV = "ANTHROPIC_API_KEY"


class ClaudeError(Exception):
    """Claude could not be asked, or what it sent back cannot be used."""


def has_key() -> bool:
    return bool(os.environ.get(KEY_ENV))


def settings(config: dict) -> tuple[str, str]:
    """A section's `model` and `effort`, checked."""
    model = config.get("model", DEFAULT_MODEL)
    if not isinstance(model, str) or not model.strip():
        raise ValueError("'model' must be a model name")
    effort = config.get("effort", DEFAULT_EFFORT)
    if effort not in EFFORTS:
        raise ValueError("'effort' must be one of: " + ", ".join(EFFORTS))
    return model.strip(), effort


def ask_json(system: str, content: str, schema: dict, *, model: str, effort: str,
             timeout: float) -> dict:
    """Returns the object Claude sent, as it sent it: the schema shapes it,
    but what it holds is for the caller to check."""
    # Imported here so a Press with no such section never loads the SDK.
    import anthropic

    client = anthropic.Anthropic(timeout=timeout, max_retries=1)
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort,
                           "format": {"type": "json_schema", "schema": schema}},
            # If the model's safeguards decline the request, the API reruns
            # it on a fallback model inside the same call.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as exc:
        raise ClaudeError(f"the API key was not accepted ({exc.status_code})") from exc
    except anthropic.RateLimitError as exc:
        raise ClaudeError("rate limited") from exc
    except anthropic.APIStatusError as exc:
        raise ClaudeError(f"the API answered {exc.status_code}") from exc
    except anthropic.APIConnectionError as exc:
        raise ClaudeError("the API could not be reached in time") from exc
    except anthropic.AnthropicError as exc:
        raise ClaudeError(f"the request could not be made ({type(exc).__name__})") from exc
    if response.stop_reason == "refusal":
        raise ClaudeError("the request was declined")
    if response.stop_reason == "max_tokens":
        raise ClaudeError("the answer was cut short")
    text = next((block.text for block in response.content if block.type == "text"), "")
    try:
        answer = json.loads(text)
    except ValueError as exc:
        raise ClaudeError("the answer was not the JSON asked for") from exc
    if not isinstance(answer, dict):
        raise ClaudeError("the answer was not the JSON asked for")
    return answer
