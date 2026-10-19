from __future__ import annotations

__all__ = ["EntityKind", "PREDICATES", "Predicate", "predicate_catalog"]

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

type EntityKind = Literal[
    "unknown",
    "concept",
    "person",
    "agent",
    "activity",
    "document",
    "project",
    "source",
    "claim",
]

_ENTITIES: tuple[EntityKind, ...] = (
    "concept",
    "person",
    "agent",
    "document",
    "project",
    "source",
    "claim",
)
_AGENTS: tuple[EntityKind, ...] = ("person", "agent")


class Predicate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    uri: str
    domain: tuple[EntityKind, ...]
    range: tuple[EntityKind, ...]
    is_extension: bool = False


PREDICATES: Mapping[str, Predicate] = MappingProxyType(
    {
        "skos:broader": Predicate(
            uri="http://www.w3.org/2004/02/skos/core#broader",
            domain=("concept",),
            range=("concept",),
        ),
        "skos:related": Predicate(
            uri="http://www.w3.org/2004/02/skos/core#related",
            domain=("concept",),
            range=("concept",),
        ),
        "prov:used": Predicate(
            uri="http://www.w3.org/ns/prov#used",
            domain=("activity",),
            range=_ENTITIES,
        ),
        "prov:wasGeneratedBy": Predicate(
            uri="http://www.w3.org/ns/prov#wasGeneratedBy",
            domain=_ENTITIES,
            range=("activity",),
        ),
        "prov:wasAttributedTo": Predicate(
            uri="http://www.w3.org/ns/prov#wasAttributedTo",
            domain=_ENTITIES,
            range=_AGENTS,
        ),
        "prov:wasAssociatedWith": Predicate(
            uri="http://www.w3.org/ns/prov#wasAssociatedWith",
            domain=("activity",),
            range=_AGENTS,
        ),
        "prov:wasDerivedFrom": Predicate(
            uri="http://www.w3.org/ns/prov#wasDerivedFrom",
            domain=_ENTITIES,
            range=_ENTITIES,
        ),
        "prov:wasRevisionOf": Predicate(
            uri="http://www.w3.org/ns/prov#wasRevisionOf",
            domain=_ENTITIES,
            range=_ENTITIES,
        ),
        "dcterms:subject": Predicate(
            uri="http://purl.org/dc/terms/subject",
            domain=("document",),
            range=("concept", "person"),
        ),
        "hanger:supportedBy": Predicate(
            uri="urn:hanger:relation:supportedBy",
            domain=("claim",),
            range=("source", "document"),
            is_extension=True,
        ),
        "hanger:refutedBy": Predicate(
            uri="urn:hanger:relation:refutedBy",
            domain=("claim",),
            range=("source", "document"),
            is_extension=True,
        ),
        "hanger:requires": Predicate(
            uri="urn:hanger:relation:requires",
            domain=("concept",),
            range=("concept",),
            is_extension=True,
        ),
        "hanger:supersedes": Predicate(
            uri="urn:hanger:relation:supersedes",
            domain=("claim", "document"),
            range=("claim", "document"),
            is_extension=True,
        ),
    }
)


def predicate_catalog() -> dict[str, dict[str, Any]]:
    """Return the fixed vocabulary and the supported Hanger type profile."""
    return {
        name: predicate.model_dump(mode="json")
        for name, predicate in PREDICATES.items()
    }
