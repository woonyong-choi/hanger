from __future__ import annotations

__all__ = ["encoded_size", "shape", "snippets"]

import json
from typing import Any

from markchain.config import Settings
from markchain.errors import RequestError

_SNIPPET_LINES = 2
_SNIPPET_WIDTH = 160


def encoded_size(document: dict[str, Any]) -> int:
    return len(json.dumps(document, ensure_ascii=False).encode())


# cost: time O(n*t), heap O(1), stack O(1)
# vars: n = document bytes, t = query terms
# basis: estimate
def snippets(content: str, terms: list[str]) -> list[str]:
    """Return a few lines that explain why a document matched."""
    found = []
    lowered = [t.casefold() for t in terms]
    for line in content.splitlines():
        folded = line.casefold()
        term = next((t for t in lowered if t and t in folded), None)
        if term is None:
            continue
        at = folded.index(term)
        start = max(0, at - _SNIPPET_WIDTH // 2)
        found.append(line[start : start + _SNIPPET_WIDTH].strip())
        if len(found) == _SNIPPET_LINES:
            break
    return found


def listing(document: dict[str, Any], terms: list[str]) -> dict[str, Any]:
    return {
        "path": document["path"],
        "title": document["title"],
        "bytes": len(document["content"].encode()),
        "snippets": snippets(document["content"], terms),
    }


# cost: time O(n), heap O(n), stack O(1)
# vars: n = candidate bytes
# basis: estimate
def shape(
    ranked: list[dict[str, Any]],
    required: dict[str, dict[str, Any]],
    settings: Settings,
    terms: list[str],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Split candidates into full text and a matched-line listing.

    Required inputs are always full. Ranked candidates fill the remaining
    full-text slots until the configured share of the context limit is used.
    """
    budget = int(settings.context_bytes * settings.search.context_fill)
    full = dict(required)
    used = sum(encoded_size(doc) for doc in full.values())
    if used > settings.context_bytes:
        largest = max(full.values(), key=encoded_size)
        raise RequestError(
            f"입력 문서가 너무 큽니다: {largest['path']} "
            f"(최대 {settings.search.context_kib}KiB)",
            code="input_too_large",
        )
    listed = []
    slots = settings.search.full_text
    for document in ranked:
        if document["path"] in full:
            continue
        size = encoded_size(document)
        if slots > 0 and used + size <= budget:
            full[document["path"]] = document
            used += size
            slots -= 1
        else:
            listed.append(listing(document, terms))
    return full, listed
