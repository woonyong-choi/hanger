from __future__ import annotations

__all__ = [
    "DocumentReference",
    "DocumentSemanticSnapshot",
    "Entity",
    "Evidence",
    "EvidenceCheck",
    "FrontmatterError",
    "LegacyMetadata",
    "RelationAssertion",
    "SemanticError",
    "SemanticMetadata",
    "document_id_for_path",
    "parse_semantics",
    "semantic_schema",
    "verify_evidence",
]

import hashlib
import json
import re
from collections.abc import Hashable, Sequence
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Self, override
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID

import yaml
from pydantic import (
    AfterValidator,
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from yaml.nodes import MappingNode, Node, ScalarNode

from hanger.errors import HangerError
from hanger.vocabulary import PREDICATES, EntityKind

_CONFIG = ConfigDict(extra="forbid", strict=True, frozen=True)
_LEGACY_FIELDS = {
    "aliases",
    "definition",
    "differentia",
    "same_as",
    "central_question",
    "broader",
    "related",
    "requires",
    "parent",
    "title",
    "slug",
}
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue], config=ConfigDict(allow_inf_nan=False))
_HTTP_ID = TypeAdapter(AnyHttpUrl)


class SemanticError(HangerError):
    code = "invalid_semantics"


class FrontmatterError(SemanticError):
    code = "invalid_frontmatter"


def _validate_id(value: str) -> str:
    if not value or any(
        character.isspace() or ord(character) < 32 or character in '<>"{}|\\^`'
        for character in value
    ):
        raise ValueError(
            "identity must be a nonempty URN, HTTP(S) IRI or doc reference"
        )
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise ValueError("identity contains invalid percent encoding")
    if value.startswith("doc:"):
        path = _validate_path(unquote(value[4:]))
        if value != "doc:" + quote(path, safe="/"):
            raise ValueError("doc reference must use canonical percent encoding")
        return value
    if value.startswith("urn:"):
        if not re.fullmatch(r"urn:[a-zA-Z0-9][a-zA-Z0-9-]*:[^\s]+", value):
            raise ValueError("invalid URN")
        if value.lower().startswith("urn:uuid:"):
            UUID(value[9:])
        return value
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("identity must be a URN, HTTP(S) IRI or doc reference")
    _HTTP_ID.validate_python(value)
    return value


type Identity = Annotated[str, AfterValidator(_validate_id)]
type Nonempty = Annotated[str, Field(min_length=1)]
type DocumentKind = Literal[
    "unknown",
    "note",
    "concept",
    "person",
    "project",
    "source",
    "claim",
    "activity",
    "index",
    "decision",
    "experiment",
]


class Entity(BaseModel):
    model_config = _CONFIG

    id: Identity
    kind: EntityKind = "unknown"
    label: Nonempty | None = None
    aliases: list[Nonempty] = Field(default_factory=list)
    definition: Nonempty | None = None
    central_question: Nonempty | None = None

    @model_validator(mode="after")
    def validate_document_identity(self) -> Self:
        if self.id.startswith("doc:") and self.kind != "document":
            raise ValueError("doc references identify documents only")
        return self


class DocumentReference(BaseModel):
    model_config = _CONFIG

    id: Identity
    path: str | None = None
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        return _validate_path(value) if value is not None else None

    @model_validator(mode="after")
    def validate_local_reference(self) -> Self:
        if (
            self.id.startswith("doc:")
            and self.path is not None
            and unquote(self.id[4:]) != self.path
        ):
            raise ValueError("document reference id and path disagree")
        return self


class Evidence(BaseModel):
    model_config = _CONFIG

    source: DocumentReference
    quote: Nonempty
    locator: Nonempty | None = None

    @field_validator("quote")
    @classmethod
    def validate_quote(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence quote must contain text")
        return value


class RelationAssertion(BaseModel):
    model_config = _CONFIG

    subject: Identity
    predicate: str = Field(json_schema_extra={"enum": list(PREDICATES)})
    object: Identity
    status: Literal["proposed", "accepted", "rejected"] = "proposed"
    origin: Literal["explicit", "inferred"] = "explicit"
    evidence: list[Evidence] = Field(default_factory=list)
    time: Nonempty | None = Field(default=None, description="Source time as recorded")
    scope: Nonempty | None = Field(default=None, description="Scope of this assertion")

    @field_validator("predicate")
    @classmethod
    def validate_predicate(cls, value: str) -> str:
        if value not in PREDICATES:
            raise ValueError(f"unsupported predicate: {value}")
        return value

    @model_validator(mode="after")
    def validate_accepted_evidence(self) -> Self:
        if self.status == "accepted" and not self.evidence:
            raise ValueError("accepted assertions require evidence")
        return self


class SemanticMetadata(BaseModel):
    model_config = _CONFIG

    version: Literal[1] = 1
    document_id: Identity | None = None
    document_kind: DocumentKind = "unknown"
    entities: list[Entity] = Field(default_factory=list)
    assertions: list[RelationAssertion] = Field(default_factory=list)

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("version must be an integer")
        return value

    @model_validator(mode="after")
    def validate_entity_ids(self) -> Self:
        ids = [entity.id for entity in self.entities]
        if self.document_id is not None:
            ids.append(self.document_id)
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate entity or document id")
        return self


class LegacyMetadata(BaseModel):
    model_config = _CONFIG

    values: dict[str, JsonValue] = Field(default_factory=dict)
    declared_fields: list[str] = Field(default_factory=list)

    @classmethod
    def from_frontmatter(cls, frontmatter: dict[str, JsonValue]) -> Self:
        values = {
            key: value for key, value in frontmatter.items() if key in _LEGACY_FIELDS
        }
        return cls(values=values, declared_fields=sorted(values))


class EvidenceCheck(BaseModel):
    model_config = _CONFIG

    assertion_index: int
    evidence_index: int
    source_id: str
    status: Literal["verified", "unverified"]


class DocumentSemanticSnapshot(BaseModel):
    model_config = _CONFIG

    path: str
    sha256: str
    text: str
    body: str
    document_id: str
    metadata: SemanticMetadata
    legacy: LegacyMetadata
    frontmatter: dict[str, JsonValue]
    declared_fields: list[str]
    evidence_checks: list[EvidenceCheck] = Field(default_factory=list)


class _FrontmatterLoader(yaml.SafeLoader):
    def construct_timestamp(self, node: Node) -> str:
        if not isinstance(node, ScalarNode):
            raise FrontmatterError("timestamp must be a scalar")
        return self.construct_scalar(node)

    @override
    def construct_mapping(self, node: Node, deep: bool = False) -> dict[Hashable, Any]:
        if not isinstance(node, MappingNode):
            raise FrontmatterError("expected a frontmatter mapping")
        self.flatten_mapping(node)
        mapping: dict[Hashable, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise FrontmatterError("frontmatter keys must be strings")
            if key in mapping:
                raise FrontmatterError(f"duplicate frontmatter key: {key}")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


_FrontmatterLoader.yaml_implicit_resolvers = {
    key: [item for item in values if item[0] != "tag:yaml.org,2002:timestamp"]
    for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_FrontmatterLoader.add_constructor(
    "tag:yaml.org,2002:timestamp", _FrontmatterLoader.construct_timestamp
)


def document_id_for_path(path: str) -> str:
    """Return a closet-local document reference without reading the filesystem.

    Raises:
        SemanticError: The path is not a normalized relative Markdown path.
    """
    try:
        return "doc:" + quote(_validate_path(path), safe="/")
    except ValueError as error:
        raise SemanticError(str(error)) from error


def parse_semantics(
    text: str, *, path: str = "document.md"
) -> DocumentSemanticSnapshot:
    """Parse a document without reading, writing or inferring semantic relations.

    Raises:
        FrontmatterError: The frontmatter is malformed or has duplicate keys.
        SemanticError: Metadata, relation types or available evidence are invalid.
    """
    fallback_id = document_id_for_path(path)
    frontmatter, body = _parse_frontmatter(text)
    raw_metadata = frontmatter.get("semantics", {})
    try:
        metadata = SemanticMetadata.model_validate(raw_metadata)
    except ValidationError as error:
        raise SemanticError(f"invalid semantics: {error}") from error
    document_id = metadata.document_id or fallback_id
    if document_id.startswith("doc:") and document_id != fallback_id:
        raise SemanticError("document id and snapshot path disagree")
    _validate_relations(metadata, document_id)
    snapshot = DocumentSemanticSnapshot(
        path=path,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        text=text,
        body=body,
        document_id=document_id,
        metadata=metadata,
        legacy=LegacyMetadata.from_frontmatter(frontmatter),
        frontmatter=frontmatter,
        declared_fields=sorted(metadata.model_fields_set),
    )
    return snapshot.model_copy(update={"evidence_checks": verify_evidence(snapshot)})


def semantic_schema() -> dict[str, Any]:
    """Export the canonical semantics namespace schema from its Pydantic model."""
    return SemanticMetadata.model_json_schema()


def verify_evidence(
    snapshot: DocumentSemanticSnapshot,
    *,
    sources: Sequence[DocumentSemanticSnapshot] = (),
) -> list[EvidenceCheck]:
    """Check exact body quotes in available snapshots; absent sources stay unverified.

    Raises:
        SemanticError: Source identities conflict, or a supplied source does not match.
    """
    documents: dict[str, DocumentSemanticSnapshot] = {}
    for source in (snapshot, *sources):
        if source.document_id in documents:
            raise SemanticError(f"duplicate evidence source id: {source.document_id}")
        documents[source.document_id] = source
    checks = []
    for assertion_index, assertion in enumerate(snapshot.metadata.assertions):
        for evidence_index, evidence in enumerate(assertion.evidence):
            available = documents.get(evidence.source.id)
            if available is not None:
                _check_quote(evidence, available)
            checks.append(
                EvidenceCheck(
                    assertion_index=assertion_index,
                    evidence_index=evidence_index,
                    source_id=evidence.source.id,
                    status="verified" if available is not None else "unverified",
                )
            )
    return checks


def _validate_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ValueError("document path must be normalized and closet-relative")
    if path.suffix.lower() != ".md" or "\\" in value or any(ord(c) < 32 for c in value):
        raise ValueError("document path must identify a Markdown file")
    return value


def _parse_frontmatter(text: str) -> tuple[dict[str, JsonValue], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != "---":
        return {}, text
    end = next(
        (index for index in range(1, len(lines)) if lines[index].rstrip() == "---"),
        None,
    )
    if end is None:
        raise FrontmatterError("frontmatter has no closing delimiter")
    header = "".join(lines[1:end])
    try:
        if header.lstrip().startswith("{"):
            try:
                value = json.loads(header, object_pairs_hook=_unique_json_object)
            except json.JSONDecodeError:
                value = _load_yaml(header)
        else:
            value = _load_yaml(header)
        if value is None and not header.strip():
            value = {}
        result = _JSON_OBJECT.validate_python(value)
    except (
        yaml.YAMLError,
        json.JSONDecodeError,
        ValidationError,
        RecursionError,
        ValueError,
    ) as error:
        raise FrontmatterError(f"invalid frontmatter: {error}") from error
    return result, "".join(lines[end + 1 :])


def _load_yaml(header: str) -> Any:
    if any(isinstance(token, yaml.AliasToken) for token in yaml.scan(header)):
        raise FrontmatterError("YAML aliases are not supported in frontmatter")
    return yaml.load(header, Loader=_FrontmatterLoader)


def _unique_json_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise FrontmatterError(f"duplicate frontmatter key: {key}")
        result[key] = value
    return result


def _validate_relations(metadata: SemanticMetadata, document_id: str) -> None:
    kinds: dict[str, EntityKind] = {document_id: "document"}
    for entity in metadata.entities:
        if entity.id in kinds:
            raise SemanticError(f"duplicate entity or document id: {entity.id}")
        kinds[entity.id] = entity.kind
    for assertion in metadata.assertions:
        predicate = PREDICATES[assertion.predicate]
        for identity, allowed in (
            (assertion.subject, predicate.domain),
            (assertion.object, predicate.range),
        ):
            kind = kinds.get(identity, "unknown")
            if kind not in allowed:
                raise SemanticError(
                    f"invalid {assertion.predicate} endpoint {identity}: {kind}"
                )
        for evidence in assertion.evidence:
            source_kind = kinds.get(evidence.source.id)
            if source_kind is not None and source_kind != "document":
                raise SemanticError("evidence source must identify a document")


def _check_quote(evidence: Evidence, source: DocumentSemanticSnapshot) -> None:
    reference = evidence.source
    if reference.path is not None and reference.path != source.path:
        raise SemanticError(f"evidence source path mismatch: {reference.id}")
    if reference.sha256 is not None and reference.sha256 != source.sha256:
        raise SemanticError(f"evidence source hash mismatch: {reference.id}")
    if evidence.quote not in source.body:
        raise SemanticError(f"evidence quote not found in source body: {reference.id}")
