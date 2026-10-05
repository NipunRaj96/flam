"""
Single entry point for every Groq call (text, JSON and image).

- One cached client instead of a new one per call.
- Model ids are read at call time, so values in secrets/.env always apply.
- Retries transient failures; never retries a bad request or bad key.
- Strips any <think> block a reasoning model might emit.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from typing import Any, Optional

import groq

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "qwen/qwen3.8-27b"

_client: Optional[groq.AsyncGroq] = None
_THINK_RE = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL | re.IGNORECASE)
_NO_RETRY = (groq.BadRequestError, groq.AuthenticationError, groq.NotFoundError)


def has_llm() -> bool:
    return bool(os.getenv("GROQ_API_KEY"))


def text_model() -> str:
    return os.getenv("GROQ_MODEL", DEFAULT_MODEL)


def vision_model() -> str:
    # The default model reads images too; override only if you want a different one.
    return os.getenv("GROQ_VISION_MODEL") or text_model()


def _get_client() -> groq.AsyncGroq:
    global _client
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY is not set.")
    if _client is None or _client.api_key != key:
        _client = groq.AsyncGroq(api_key=key)
    return _client


def strip_reasoning(text: str) -> str:
    return _THINK_RE.sub("", text or "").strip()


async def chat(
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.3,
    max_tokens: int = 700,
    json_mode: bool = False,
    model: Optional[str] = None,
) -> str:
    kwargs: dict[str, Any] = dict(
        model=model or text_model(),
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    client = _get_client()
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = await client.chat.completions.create(**kwargs)
            return strip_reasoning(resp.choices[0].message.content or "")
        except _NO_RETRY:
            raise
        except Exception as exc:  # network, rate limit, 5xx
            last_exc = exc
            if attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def parse_json(text: str) -> Any:
    """Parse JSON from a model reply, tolerating code fences and stray prose."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"\{.*\}|\[.*\]", text, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise ValueError("No JSON found in model reply")


async def chat_json(messages: list[dict[str, Any]], **kw: Any) -> Any:
    return parse_json(await chat(messages, json_mode=True, **kw))


async def read_image_text(image_bytes: bytes, mime: str = "image/jpeg") -> str:
    """Transcribe all visible text in an image (a screenshot of a job post)."""
    b64 = base64.b64encode(image_bytes).decode()
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": (
                "This is a screenshot of a job posting or a hiring message. "
                "Transcribe ALL the text in it exactly as written, including email addresses, "
                "links, names and any instructions to applicants. Keep line breaks. "
                "Output only the transcription."
            )},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ],
    }]
    return await chat(messages, model=vision_model(), temperature=0.0, max_tokens=2000)
