from __future__ import annotations

__all__ = ["encoded_size", "shape", "snippets"]

import json
from collections.abc import Callable, Iterable
from typing import Any

from hanger.config import Settings
from hanger.errors import HangerError, RequestError

_SNIPPET_LINES = 2
_SNIPPET_WIDTH = 160


def encoded_size(document: dict[str, Any]) -> int:
    return len(json.dumps(document, ensure_ascii=False).encode())


# cost: time O(n*t), heap O(1), stack O(1)
# vars: n = document bytes, t = query terms
# basis: estimate
def snippets(content: str | Iterable[str], terms: list[str]) -> list[str]:
    """Return a few lines that explain why a document matched.

    Accepts text or an open file; reading stops once enough lines are found.
    """
    found: list[str] = []
    lowered = [t.casefold() for t in terms]
    lines = content.splitlines() if isinstance(content, str) else content
    for raw in lines:
        line = raw.rstrip("\n")
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
    load: Callable[[str], dict[str, Any]] | None = None,
    peek: Callable[[str, list[str]], list[str]] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Split candidates into full text and a matched-line listing.

    Required inputs are always full. Ranked candidates fill the remaining
    full-text slots until the configured share of the context limit is used.
    Candidates without content are read with ``load`` only when they are
    returned in full; listed ones only get matched lines through ``peek``.
    """
    budget = int(settings.context_bytes * settings.search.context_fill)
    full = dict(required)
    used = sum(encoded_size(doc) for doc in full.values())
    if used > settings.context_bytes:
        largest = max(full.values(), key=encoded_size)
        raise RequestError(
            f"입력 문서가 모델 입력 한도({settings.search.context_kib}KiB)를 넘습니다: "
            f"{largest['path']}",
            code="input_too_large",
        )
    listed = []
    slots = settings.search.full_text
    for document in ranked:
        path = document["path"]
        if path in full:
            continue
        if "content" not in document:
            # Size on disk is a lower bound of the encoded size.
            if slots > 0 and used + document["bytes"] <= budget and load:
                try:
                    document = load(path)
                except (OSError, HangerError):
                    continue  # changed or removed since the search; skip it
            else:
                listed.append(_peeked(document, terms, peek))
                continue
        size = encoded_size(document)
        if slots > 0 and used + size <= budget:
            full[path] = document
            used += size
            slots -= 1
        else:
            listed.append(listing(document, terms))
    return full, listed


def _peeked(
    document: dict[str, Any],
    terms: list[str],
    peek: Callable[[str, list[str]], list[str]] | None,
) -> dict[str, Any]:
    found: list[str] = []
    if peek is not None:
        try:
            found = peek(document["path"], terms)
        except (OSError, HangerError):
            found = []
    return {
        "path": document["path"],
        "title": document["title"],
        "bytes": document["bytes"],
        "snippets": found,
    }
