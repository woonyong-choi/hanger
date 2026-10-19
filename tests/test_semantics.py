from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from markdown_notes_agent.errors import MarkdownNotesAgentError
from markdown_notes_agent.semantics import (
    FrontmatterError,
    SemanticError,
    SemanticMetadata,
    document_id_for_path,
    parse_semantics,
    semantic_schema,
    verify_evidence,
)
from markdown_notes_agent.vocabulary import PREDICATES, predicate_catalog


@pytest.fixture
def concept_metadata() -> dict[str, Any]:
    return {
        "version": 1,
        "document_id": "urn:example:document:wal",
        "document_kind": "note",
        "entities": [
            {"id": "urn:example:concept:wal", "kind": "concept", "label": "WAL"},
            {"id": "urn:example:concept:recovery", "kind": "concept"},
        ],
        "assertions": [
            {
                "subject": "urn:example:concept:wal",
                "predicate": "skos:broader",
                "object": "urn:example:concept:recovery",
                "status": "accepted",
                "evidence": [
                    {
                        "source": {"id": "urn:example:document:wal"},
                        "quote": "WAL is a recovery technique.",
                        "locator": "# Definition",
                    }
                ],
            },
        ],
    }


def _document(
    metadata: dict[str, Any], body: str = "WAL is a recovery technique.\n"
) -> str:
    return "---\n" + json.dumps({"semantics": metadata}) + "\n---\n" + body


def test_concept_snapshot_separates_document_from_subject_and_preserves_source(
    concept_metadata: dict[str, Any],
) -> None:
    text = _document(concept_metadata)
    snapshot = parse_semantics(text, path="notes/wal.md")
    assert snapshot.text == text
    assert snapshot.body == "WAL is a recovery technique.\n"
    assert snapshot.sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert snapshot.document_id == "urn:example:document:wal"
    assert snapshot.metadata.entities[0].id != snapshot.document_id
    assert snapshot.evidence_checks[0].status == "verified"
    assert snapshot.metadata.assertions[0].origin == "explicit"
    assert "assertions" in snapshot.declared_fields
    assert json.loads(snapshot.model_dump_json()) == snapshot.model_dump(mode="json")
    assert (
        SemanticMetadata.model_validate(snapshot.metadata.model_dump())
        == snapshot.metadata
    )


def test_missing_metadata_stays_unknown_and_empty() -> None:
    snapshot = parse_semantics("# Someone\nA person wrote this.")
    assert snapshot.metadata.document_kind == "unknown"
    assert snapshot.metadata.entities == []
    assert snapshot.metadata.assertions == []
    assert snapshot.declared_fields == []
    assert snapshot.document_id == "doc:document.md"


def test_empty_declared_assertions_remain_distinguishable_from_missing() -> None:
    explicit = parse_semantics(_document({"assertions": []}))
    missing = parse_semantics(_document({}))
    assert explicit.metadata.assertions == missing.metadata.assertions == []
    assert explicit.declared_fields == ["assertions"]
    assert missing.declared_fields == []


def test_legacy_yaml_keeps_multiline_lists_unknown_fields_and_original_text() -> None:
    text = """---
title: 'WAL: 복구'
slug: wal
aliases:
  - 로그
  - 'write-ahead log'
definition: |
  첫 줄
  둘째 줄
central_question: 복구는 어떻게 동작하나
broader: [recovery]
related: []
requires: [storage]
parent: database
published: 2026-10-19
custom:
  retained: true
---
# WAL
본문
"""
    snapshot = parse_semantics(text)
    assert snapshot.text == text
    assert snapshot.legacy.values["definition"] == "첫 줄\n둘째 줄\n"
    assert snapshot.legacy.values["aliases"] == ["로그", "write-ahead log"]
    assert snapshot.legacy.values["related"] == []
    assert "related" in snapshot.legacy.declared_fields
    assert snapshot.frontmatter["custom"] == {"retained": True}
    assert snapshot.frontmatter["published"] == "2026-10-19"
    assert snapshot.metadata.entities == snapshot.metadata.assertions == []


def test_canonical_namespace_does_not_absorb_or_overwrite_legacy_fields() -> None:
    snapshot = parse_semantics("""---
title: Existing title
related: [legacy-name]
semantics:
  document_kind: person
  entities: []
  assertions: []
---
text
""")
    assert snapshot.legacy.values["related"] == ["legacy-name"]
    assert snapshot.metadata.document_kind == "person"
    assert snapshot.metadata.assertions == []


def test_same_person_name_does_not_merge_distinct_identifiers() -> None:
    snapshot = parse_semantics(
        _document(
            {
                "document_kind": "person",
                "entities": [
                    {"id": "urn:example:person:1", "kind": "person", "label": "민수"},
                    {"id": "urn:example:person:2", "kind": "person", "label": "민수"},
                ],
                "assertions": [
                    {
                        "subject": "doc:document.md",
                        "predicate": "dcterms:subject",
                        "object": "urn:example:person:1",
                    }
                ],
            }
        )
    )
    assert len(snapshot.metadata.entities) == 2
    assert snapshot.metadata.assertions[0].object == "urn:example:person:1"
    assert snapshot.metadata.assertions[0].status == "proposed"


@pytest.mark.parametrize(
    ("predicate", "subject_kind", "object_kind"),
    [
        ("prov:used", "activity", "source"),
        ("prov:wasGeneratedBy", "claim", "activity"),
        ("prov:wasAttributedTo", "document", "person"),
        ("prov:wasAssociatedWith", "activity", "agent"),
        ("prov:wasDerivedFrom", "claim", "source"),
        ("prov:wasRevisionOf", "document", "document"),
        ("dcterms:subject", "document", "concept"),
        ("skos:related", "concept", "concept"),
        ("markdown-notes-agent:requires", "concept", "concept"),
        ("markdown-notes-agent:supportedBy", "claim", "source"),
        ("markdown-notes-agent:refutedBy", "claim", "document"),
        ("markdown-notes-agent:supersedes", "claim", "claim"),
    ],
)
def test_supported_relation_has_valid_domain_and_range(
    predicate: str,
    subject_kind: str,
    object_kind: str,
) -> None:
    snapshot = parse_semantics(
        _document(
            {
                "entities": [
                    {"id": "urn:example:subject", "kind": subject_kind},
                    {"id": "urn:example:object", "kind": object_kind},
                ],
                "assertions": [
                    {
                        "subject": "urn:example:subject",
                        "predicate": predicate,
                        "object": "urn:example:object",
                        "origin": "inferred",
                        "time": "2026-10-19",
                        "scope": "local experiment",
                    }
                ],
            }
        )
    )
    assertion = snapshot.metadata.assertions[0]
    assert assertion.predicate == predicate
    assert assertion.origin == "inferred"
    assert assertion.scope == "local experiment"


@pytest.mark.parametrize("kind", ["person", "activity", "unknown"])
def test_skos_does_not_coerce_non_concepts(kind: str) -> None:
    with pytest.raises(SemanticError, match="invalid skos:broader endpoint"):
        parse_semantics(
            _document(
                {
                    "entities": [{"id": "urn:example:x", "kind": kind}],
                    "assertions": [
                        {
                            "subject": "urn:example:x",
                            "predicate": "skos:broader",
                            "object": "urn:example:x",
                        }
                    ],
                }
            )
        )


def test_wrong_provenance_direction_is_rejected() -> None:
    with pytest.raises(SemanticError, match="invalid prov:used endpoint"):
        parse_semantics(
            _document(
                {
                    "entities": [
                        {"id": "urn:example:source", "kind": "source"},
                        {"id": "urn:example:activity", "kind": "activity"},
                    ],
                    "assertions": [
                        {
                            "subject": "urn:example:source",
                            "predicate": "prov:used",
                            "object": "urn:example:activity",
                        }
                    ],
                }
            )
        )


def test_undeclared_relation_target_is_not_inferred(
    concept_metadata: dict[str, Any],
) -> None:
    concept_metadata["entities"].pop()
    with pytest.raises(SemanticError, match="endpoint.*unknown"):
        parse_semantics(_document(concept_metadata))


@pytest.mark.parametrize(
    "body", ["", "Only metadata has the quote.", "WAL is something else."]
)
def test_local_evidence_must_appear_in_body(
    concept_metadata: dict[str, Any],
    body: str,
) -> None:
    with pytest.raises(SemanticError, match="quote not found in source body"):
        parse_semantics(_document(concept_metadata, body))


def test_external_evidence_stays_unverified_until_snapshot_is_supplied(
    concept_metadata: dict[str, Any],
) -> None:
    evidence = concept_metadata["assertions"][0]["evidence"][0]
    evidence["source"] = {"id": "doc:source.md"}
    snapshot = parse_semantics(_document(concept_metadata, "No local quote.\n"))
    assert snapshot.evidence_checks[0].status == "unverified"
    source = parse_semantics("WAL is a recovery technique.\n", path="source.md")
    assert verify_evidence(snapshot, sources=[source])[0].status == "verified"
    assert snapshot.evidence_checks[0].status == "unverified"
    wrong = parse_semantics("Something else.", path="source.md")
    with pytest.raises(SemanticError, match="quote not found"):
        verify_evidence(snapshot, sources=[wrong])


@pytest.mark.parametrize(
    ("field", "value"), [("path", "different.md"), ("sha256", "0" * 64)]
)
def test_evidence_source_revision_mismatch_is_rejected(
    concept_metadata: dict[str, Any],
    field: str,
    value: str,
) -> None:
    concept_metadata["assertions"][0]["evidence"][0]["source"][field] = value
    with pytest.raises(SemanticError, match="evidence source.*mismatch"):
        parse_semantics(_document(concept_metadata))


def test_accepted_assertions_require_evidence(concept_metadata: dict[str, Any]) -> None:
    concept_metadata["assertions"][0]["evidence"] = []
    with pytest.raises(SemanticError, match="accepted assertions require evidence"):
        parse_semantics(_document(concept_metadata))


@pytest.mark.parametrize(
    "metadata",
    [
        {"document_kind": "typo"},
        {"version": 2},
        {"version": True},
        {"version": "1"},
        {"unknown": "not allowed"},
        {"@context": "https://example.com/context"},
        {"entities": [{"id": "urn:example:x", "kind": "typo"}]},
        {"entities": [{"id": "person.md", "kind": "person"}]},
        {"entities": [{"id": "doc:person.md", "kind": "person"}]},
        {"entities": [{"id": "urn:uuid:not-a-uuid", "kind": "person"}]},
        {"entities": [{"id": "urn:example:x", "extra": "invalid"}]},
        {"entities": [{"id": "urn:example:x"}, {"id": "urn:example:x"}]},
        {"document_id": "urn:example:x", "entities": [{"id": "urn:example:x"}]},
        {"entities": [{"id": "doc:document.md", "kind": "document"}]},
        {
            "assertions": [
                {
                    "subject": "urn:example:x",
                    "predicate": "skos:reltaed",
                    "object": "urn:example:y",
                }
            ]
        },
    ],
)
def test_invalid_explicit_contract_raises_markdown_notes_agent_error(
    metadata: dict[str, Any],
) -> None:
    with pytest.raises(MarkdownNotesAgentError) as caught:
        parse_semantics(_document(metadata))
    assert caught.value.code == "invalid_semantics"


@pytest.mark.parametrize(
    "header",
    [
        "semantics: [unterminated",
        "semantics: null",
        "- a root list",
        "title: first\ntitle: second",
        '{"title": "first", "title": "second"}',
        "semantics: {document_kind: note, document_kind: person}",
        'semantics: !!python/object/apply:os.system ["echo unsafe"]',
        "custom: .nan",
        '{"custom": NaN}',
        "custom: &cycle [*cycle]",
        "? [invalid, key]\n: value",
    ],
)
def test_invalid_frontmatter_fails_without_fallback(header: str) -> None:
    with pytest.raises(SemanticError):
        parse_semantics("---\n" + header + "\n---\nbody")


def test_unclosed_frontmatter_is_not_treated_as_body() -> None:
    with pytest.raises(FrontmatterError, match="closing delimiter"):
        parse_semantics("---\ntitle: incomplete\n")


@pytest.mark.parametrize(
    "path", ["../note.md", "/note.md", "notes//note.md", "./note.md", "note.txt"]
)
def test_invalid_document_paths_are_rejected(path: str) -> None:
    with pytest.raises(SemanticError):
        parse_semantics("body", path=path)


def test_document_ids_escape_paths_without_claiming_global_identity() -> None:
    assert (
        document_id_for_path("notes/한 글.md") == "doc:notes/%ED%95%9C%20%EA%B8%80.md"
    )
    assert (
        parse_semantics(
            _document({"document_id": "https://example.org/notes/1"})
        ).document_id
        == "https://example.org/notes/1"
    )


def test_schema_and_catalog_derive_from_runtime_contract() -> None:
    schema = semantic_schema()
    assert schema == SemanticMetadata.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["RelationAssertion"]["properties"]["predicate"][
        "enum"
    ] == list(PREDICATES)
    catalog = predicate_catalog()
    assert (
        catalog["skos:broader"]["uri"] == "http://www.w3.org/2004/02/skos/core#broader"
    )
    assert catalog["prov:used"]["domain"] == ["activity"]
    assert catalog["markdown-notes-agent:supportedBy"]["is_extension"] is True
    assert catalog["dcterms:subject"]["is_extension"] is False


def test_yaml_timestamps_stay_json_strings_without_changing_quoted_text() -> None:
    snapshot = parse_semantics("""---
published: 2026-10-19T12:30:00+09:00
quoted: '2026-10-19T12:30:00+09:00'
typed: !!timestamp 2026-10-19
---
body
""")
    assert snapshot.frontmatter == {
        "published": "2026-10-19T12:30:00+09:00",
        "quoted": "2026-10-19T12:30:00+09:00",
        "typed": "2026-10-19",
    }
    assert json.loads(snapshot.model_dump_json())["frontmatter"] == snapshot.frontmatter


@pytest.mark.parametrize(
    "header",
    [
        "null",
        "custom: &value [one, two]\nother: *value",
        "custom: !!binary aGVsbG8=",
    ],
)
def test_non_json_root_values_and_yaml_aliases_are_rejected(header: str) -> None:
    with pytest.raises(FrontmatterError):
        parse_semantics("---\n" + header + "\n---\n")


def test_documentation_example_parses_and_catalog_uris_match() -> None:
    documentation = (Path(__file__).parents[1] / "docs" / "data.md").read_text()
    example = documentation.split("```text\n", 1)[1].split("```", 1)[0]
    snapshot = parse_semantics(example, path="notes/wal.md")
    assert snapshot.evidence_checks[0].status == "verified"
    assert snapshot.metadata.assertions[0].status == "accepted"
    for name, predicate in predicate_catalog().items():
        assert f"| `{name}` | `{predicate['uri']}` |" in documentation


@pytest.mark.parametrize(
    "identity",
    [
        "https://example.org:invalid/person",
        "urn:example:<person>",
        "urn:example:\x00",
    ],
)
def test_malformed_absolute_identifiers_are_rejected(identity: str) -> None:
    with pytest.raises(SemanticError):
        parse_semantics(_document({"entities": [{"id": identity, "kind": "person"}]}))


def test_indented_delimiter_remains_inside_yaml_scalar() -> None:
    snapshot = parse_semantics("""---
definition: |
  first
  ---
  second
semantics:
  document_kind: person
---
body
""")
    assert snapshot.legacy.values["definition"] == "first\n---\nsecond\n"
    assert snapshot.metadata.document_kind == "person"
    assert snapshot.body == "body\n"


def test_safe_yaml_flow_mapping_is_supported() -> None:
    snapshot = parse_semantics("---\n{title: WAL, semantics: {version: 1}}\n---\nbody")
    assert snapshot.frontmatter["title"] == "WAL"
    assert snapshot.metadata.version == 1


@pytest.mark.parametrize("header", ['{"custom": %s}', "custom: %s"])
def test_unrepresentable_integer_uses_frontmatter_error(header: str) -> None:
    with pytest.raises(FrontmatterError):
        parse_semantics("---\n" + (header % ("1" * 5000)) + "\n---\nbody")
