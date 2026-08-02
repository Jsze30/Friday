from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
MAX_RESPONSE_BYTES = 1_000_000


class ResponsesAPIError(RuntimeError):
    pass


def with_model(
    payload: dict[str, Any],
    model: str,
    reasoning_effort: str,
) -> dict[str, Any]:
    """Return a request payload configured for one model attempt."""
    configured = dict(payload)
    configured["model"] = model
    if model.casefold().startswith("gpt-5") and reasoning_effort:
        configured["reasoning"] = {"effort": reasoning_effort}
    else:
        configured.pop("reasoning", None)
    return configured


def extract_output_text(response: dict[str, Any]) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts: list[str] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
    if not parts:
        raise ResponsesAPIError("the model returned no text")
    return "\n".join(parts)


def post(
    payload: dict[str, Any],
    api_key: str,
    *,
    timeout: float,
    logger: logging.Logger,
    purpose: str,
) -> dict[str, Any]:
    request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Friday/0.3",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as error:
        logger.warning("OpenAI %s failed with HTTP %s", purpose, error.code)
        raise ResponsesAPIError(
            f"{purpose} request failed with HTTP {error.code}"
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ResponsesAPIError(f"{purpose} request could not be completed") from error
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ResponsesAPIError(f"{purpose} returned invalid JSON") from error
    if not isinstance(decoded, dict):
        raise ResponsesAPIError(f"{purpose} returned an invalid response")
    return decoded
