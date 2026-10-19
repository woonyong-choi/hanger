from __future__ import annotations

__all__ = ["KeywordMetadata", "keyword_metadata"]

from dataclasses import dataclass
from typing import Any

from markdown_notes_agent.semantics import DocumentSemanticSnapshot, SemanticError

_FIELDS = {
    "name",
    "aliases",
    "definition",
    "central_question",
    "broader",
    "related",
    "requires",
}


@dataclass(frozen=True)
class KeywordMetadata:
    values: dict[str, Any]
    concept: bool

    @property
    def complete(self) -> bool:
        return self.values.keys() >= _FIELDS


def keyword_metadata(snapshot: DocumentSemanticSnapshot) -> KeywordMetadata:
    values: dict[str, Any] = dict(snapshot.legacy.values)
    if "title" in values:
        values["name"] = values["title"] if isinstance(values["title"], str) else ""
    for key in ("aliases", "broader", "related", "requires", "parent"):
        if key in values:
            values[key] = _references(values[key], key)
    for key in ("definition", "central_question", "differentia", "same_as"):
        if key in values and not isinstance(values[key], str):
            values[key] = ""
    metadata = snapshot.metadata
    concept = metadata.document_kind in {"unknown", "concept"}
    concept = concept and all(
        e.kind in {"concept", "document", "unknown"} for e in metadata.entities
    )
    concepts = [entity for entity in metadata.entities if entity.kind == "concept"]
    if metadata.document_kind == "concept" and len(concepts) == 1:
        entity = concepts[0]
        for key in entity.model_fields_set & {
            "label",
            "aliases",
            "definition",
            "central_question",
        }:
            values["name" if key == "label" else key] = getattr(entity, key) or (
                [] if key == "aliases" else ""
            )
    if "assertions" in snapshot.declared_fields:
        values["same_as"] = ""
        for key in ("broader", "related", "requires", "parent"):
            values[key] = []
    return KeywordMetadata(values, concept)


def _references(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise SemanticError(f"invalid legacy {field}: expected a string or list")
    references = []
    for item in value:
        if isinstance(item, str):
            references.append(item)
        elif (
            field != "aliases"
            and isinstance(item, dict)
            and item.keys() == {"id"}
            and isinstance(item["id"], str)
            and item["id"].strip()
        ):
            references.append(item["id"])
        else:
            raise SemanticError(f"invalid legacy {field} entry")
    return references
