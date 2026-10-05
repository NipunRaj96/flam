"""
Deterministic clean-up of model output before it goes into a form or an email.

The prompt does most of the work; this catches what slips through: markdown,
quotes around the answer, em dashes, and a few stock AI words.
"""
from __future__ import annotations

import re
from typing import Optional

# word -> plain replacement (matched case-insensitively, whole word)
_SWAPS = {
    "utilize": "use", "utilise": "use", "utilizes": "uses", "utilises": "uses",
    "utilized": "used", "utilised": "used", "utilizing": "using", "utilising": "using",
    "leverage": "use", "leverages": "uses", "leveraged": "used", "leveraging": "using",
    "spearheaded": "led", "spearhead": "lead", "spearheading": "leading",
    "cutting-edge": "modern", "state-of-the-art": "modern",
    "seamlessly": "smoothly", "seamless": "smooth",
    "robust": "solid", "delve into": "look into", "delved into": "looked into",
    "endeavor": "effort", "endeavour": "effort",
    "passionate about": "interested in",
}
_SWAP_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in sorted(_SWAPS, key=len, reverse=True)) + r")\b", re.IGNORECASE)

_PREFIX_RE = re.compile(r"^\s*(?:here(?:'s| is)[^:\n]*:|answer:|response:)\s*", re.IGNORECASE)


def _swap(m: re.Match) -> str:
    rep = _SWAPS[m.group(1).lower()]
    return rep.capitalize() if m.group(1)[0].isupper() else rep


def clean_answer(text: str, field_type: str = "paragraph", max_chars: Optional[int] = None) -> str:
    t = re.sub(r"<think>.*?(?:</think>|$)", "", text or "", flags=re.DOTALL | re.IGNORECASE).strip()
    t = _PREFIX_RE.sub("", t)
    t = re.sub(r"^```\w*\n?|\n?```$", "", t).strip()

    # markdown -> plain text
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: m.group(2) if m.group(1) == m.group(2) else f"{m.group(1)} ({m.group(2)})", t)
    t = re.sub(r"(\*\*|__)(.+?)\1", r"\2", t)
    t = re.sub(r"(?<!\w)[*_](\S[^*_\n]*?)[*_](?!\w)", r"\1", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\s*[*•]\s+", "- ", t, flags=re.MULTILINE)

    # dashes: em dash reads as machine-written
    t = re.sub(r"\s*—\s*", ", ", t)
    t = re.sub(r"(?<=\d)\s*–\s*(?=\d)", "-", t)
    t = re.sub(r"\s*–\s*", ", ", t)

    t = _SWAP_RE.sub(_swap, t)

    # outer quotes
    if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'“”":
        t = t[1:-1].strip()
    t = t.strip("“”").strip()

    if field_type == "short_text":
        t = re.sub(r"\s*\n+\s*", " ", t)
    else:
        t = re.sub(r"[ \t]+\n", "\n", t)
        t = re.sub(r"\n{3,}", "\n\n", t)

    if max_chars and len(t) > max_chars:
        t = _trim(t, max_chars)
    return t.strip()


def _trim(t: str, limit: int) -> str:
    cut = t[:limit]
    end = max(cut.rfind(". "), cut.rfind(".\n"), cut.rfind("? "), cut.rfind("! "))
    if cut.endswith((".", "?", "!")):
        return cut
    if end > limit * 0.5:
        return cut[: end + 1]
    cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(",;:- ") + "."


_CHAR_RE = re.compile(r"(\d[\d,]{1,5})\s*(?:characters?|chars?)\b", re.IGNORECASE)
_WORD_RE = re.compile(r"(\d[\d,]{1,4})\s*words?\b", re.IGNORECASE)


def parse_limit(question: str) -> tuple[Optional[int], Optional[int]]:
    """Return (max_chars, max_words) if the question states a length limit."""
    c = _CHAR_RE.search(question or "")
    w = _WORD_RE.search(question or "")
    return (
        int(c.group(1).replace(",", "")) if c else None,
        int(w.group(1).replace(",", "")) if w else None,
    )


def limit_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return _trim(" ".join(words[:max_words]), len(" ".join(words[:max_words])))
