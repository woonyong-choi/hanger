import json
import os
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from markdown_notes_agent.documents import Documents
from markdown_notes_agent.keywords import Extraction, KeywordBuilder, KeywordGraph
from markdown_notes_agent.search import SearchIndex
from markdown_notes_agent.semantics import parse_semantics
from markdown_notes_agent.sync import Synchronizer
from markdown_notes_agent.vaults import VaultPolicy


class _Index:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}

    def ensure(self) -> None:
        return None

    def refresh(self) -> None:
        return None

    def analyze(self, query: str) -> list[str]:
        return query.split()

    def metadata(self, paths: list[str]) -> dict[str, dict[str, Any]]:
        return {p: self.documents[p] for p in paths if p in self.documents}

    def entries(self) -> Iterator[dict[str, Any]]:
        yield from list(self.documents.values())

    def write(self, entries: Iterable[dict[str, Any]]) -> None:
        for entry in entries:
            if "hash" in entry:
                self.documents[entry["path"]] = entry
            else:
                self.documents.pop(entry["path"], None)

    def find(
        self, query: str, limit: int, *, folders: tuple[str, ...] = ()
    ) -> list[dict[str, Any]]:
        found = []
        for document in self.documents.values():
            if folders and not any(
                document["path"].startswith(f + "/") for f in folders
            ):
                continue
            matches = [
                field
                for field in ("content", "names")
                if query.casefold() in document.get(field, "").casefold()
            ]
            if matches:
                found.append({**document, "matched_by": matches})
        return found[:limit]


@pytest.fixture
def sync(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Synchronizer]:
    root = tmp_path / "vault"
    root.mkdir()
    index: SearchIndex
    if getattr(request, "param", "memory") == "elasticsearch":
        url = os.environ.get("MARKDOWN_NOTES_AGENT_TEST_SEARCH_URL")
        if not url:
            pytest.skip("requires explicit Elasticsearch test endpoint")
        index = SearchIndex(url, str(root))
    else:
        index = cast(SearchIndex, _Index())
    graph = KeywordGraph(root, tmp_path / "state/keywords.sqlite", VaultPolicy(root))
    synchronizer = Synchronizer(Documents(root), index, policy=graph.policy)
    synchronizer.keywords = graph
    try:
        yield synchronizer
    finally:
        graph.close()
        if isinstance(index, SearchIndex):
            try:
                index.delete()
            finally:
                index.close()


def _write(sync: Synchronizer, path: str, metadata: dict[str, Any], body: str) -> None:
    target = sync.documents.root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\n" + json.dumps(metadata, ensure_ascii=False) + "\n---\n" + body
    )


def _entity(name: str, kind: str) -> dict[str, str]:
    return {"id": "urn:test:" + name, "kind": kind}


def _assertion(
    subject: str, predicate: str, obj: str, source: str, quote: str
) -> dict[str, Any]:
    return {
        "subject": "urn:test:" + subject,
        "predicate": predicate,
        "object": "urn:test:" + obj,
        "status": "accepted",
        "evidence": [{"source": {"id": "doc:" + source}, "quote": quote}],
    }


def _chain(sync: Synchronizer, *, folder: str = "public/") -> None:
    _write(
        sync,
        folder + "person.md",
        {
            "semantics": {
                "document_kind": "person",
                "entities": [
                    {
                        **_entity("person", "person"),
                        "label": "Alex",
                        "aliases": ["needle"],
                    }
                ],
            }
        },
        "# Person\nA person record.",
    )
    quote = "The experiment involved the person."
    _write(
        sync,
        folder + "experiment.md",
        {
            "semantics": {
                "document_kind": "experiment",
                "entities": [
                    _entity("person", "person"),
                    _entity("experiment", "activity"),
                ],
                "assertions": [
                    _assertion(
                        "experiment",
                        "prov:wasAssociatedWith",
                        "person",
                        folder + "experiment.md",
                        quote,
                    )
                ],
            }
        },
        "# Experiment\n" + quote,
    )
    quote = "The judgment came from the experiment."
    _write(
        sync,
        folder + "decision.md",
        {
            "semantics": {
                "document_kind": "decision",
                "entities": [
                    _entity("judgment", "claim"),
                    _entity("experiment", "activity"),
                ],
                "assertions": [
                    _assertion(
                        "judgment",
                        "prov:wasGeneratedBy",
                        "experiment",
                        folder + "evidence.md",
                        quote,
                    )
                ],
            }
        },
        "# Decision\nA judgment.",
    )
    _write(sync, folder + "evidence.md", {}, "# Evidence\n" + quote)
    sync.reconcile()


def test_search_expands_two_hops_to_current_evidence(sync: Synchronizer) -> None:
    _chain(sync)
    result = sync.search("needle")
    assert [d["path"] for d in result["documents"]] == ["public/person.md"]
    related = {d["path"]: d for d in result["relations"]["documents"]}
    assert related.keys() == {
        "public/experiment.md",
        "public/decision.md",
        "public/evidence.md",
    }
    assert (
        related["public/evidence.md"]["content"]
        == (sync.documents.root / "public/evidence.md").read_text()
    )
    trail = related["public/evidence.md"]["relation_path"]
    assert [edge["predicate"] for edge in trail] == [
        "prov:wasAssociatedWith",
        "prov:wasGeneratedBy",
    ]
    assert all(len(edge["owner_sha256"]) == 64 for edge in trail)
    assert related["public/evidence.md"]["matched_by"] == ["relation"]
    assert result["relations"]["limits"]["hops"] == 2


@pytest.mark.parametrize("change", ["edit", "delete", "rename", "ignore"])
def test_search_revalidates_external_evidence_before_expansion(
    sync: Synchronizer, change: str
) -> None:
    _chain(sync)
    path = sync.documents.root / "public/evidence.md"
    if change == "edit":
        path.write_text("# Changed\nThe old quote is gone.")
    elif change == "delete":
        path.unlink()
    elif change == "rename":
        path.rename(path.with_name("renamed.md"))
    else:
        sync.policy.update("public/evidence.md\n")
    result = sync.search("needle")
    assert [d["path"] for d in result["relations"]["documents"]] == [
        "public/experiment.md"
    ]
    assert result["relations"]["omitted"]


@pytest.mark.parametrize("state", ["proposed", "rejected", "inferred", "external"])
def test_search_never_promotes_unverified_or_inferred_relations(
    sync: Synchronizer, state: str
) -> None:
    _chain(sync)
    target = sync.documents.root / "public/experiment.md"
    content = target.read_text()
    if state == "inferred":
        content = content.replace(
            '"status": "accepted"', '"status": "accepted", "origin": "inferred"'
        )
    elif state == "external":
        content = content.replace(
            '"doc:public/experiment.md"', '"https://example.test/source"'
        )
    else:
        content = content.replace('"status": "accepted"', f'"status": "{state}"')
    target.write_text(content)
    sync.sync_paths(["public/experiment.md"])
    result = sync.search("needle")
    assert result["relations"]["documents"] == []
    assert result["relations"]["omitted"]


@pytest.mark.parametrize("restriction", ["folder", "ignore"])
def test_search_hidden_intermediate_never_exposes_path_label_or_quote(
    sync: Synchronizer, restriction: str
) -> None:
    _chain(sync)
    source = sync.documents.root / "public/experiment.md"
    text = source.read_text().replace("public/experiment.md", "private/secret.md")
    source.unlink()
    target = sync.documents.root / "private/secret.md"
    target.parent.mkdir()
    target.write_text(
        text.replace("The experiment involved the person.", "SECRET QUOTE")
    )
    sync.reconcile()
    if restriction == "ignore":
        sync.policy.update("private/\n")
    result = sync.search(
        "needle", folders=("public",) if restriction == "folder" else ()
    )
    assert result["relations"]["documents"] == []
    serialized = json.dumps(result)
    assert "private" not in serialized
    assert "SECRET" not in serialized


def test_search_same_name_does_not_join_person_identities(sync: Synchronizer) -> None:
    _chain(sync)
    assert sync.keywords is not None
    assert sync.keywords.find("Person") is None
    _write(
        sync,
        "public/other.md",
        {
            "semantics": {
                "document_kind": "person",
                "entities": [
                    {
                        **_entity("other-person", "person"),
                        "label": "Alex",
                        "aliases": ["unique-query"],
                    }
                ],
            }
        },
        "# Person\nOther identity.",
    )
    sync.sync_paths(["public/other.md"])
    assert sync.search("unique-query")["relations"]["documents"] == []
    graph = sync.keywords
    assert graph is not None
    assert graph.next_note() == "public/evidence.md"
    assert graph.find("Person") is None


def test_search_conflicting_identity_is_not_arbitrarily_selected(
    sync: Synchronizer,
) -> None:
    _chain(sync)
    _write(
        sync,
        "public/conflict.md",
        {
            "semantics": {
                "document_kind": "person",
                "entities": [
                    {**_entity("person", "person"), "label": "Different Alex"}
                ],
            }
        },
        "# Conflict\n",
    )
    sync.sync_paths(["public/conflict.md"])
    result = sync.search("needle")
    assert result["relations"]["documents"] == []
    assert "identity_conflict" in result["relations"]["omitted"]


def test_search_alias_change_and_deleted_projection_rebuild(sync: Synchronizer) -> None:
    _chain(sync)
    target = sync.documents.root / "public/person.md"
    target.write_text(target.read_text().replace("needle", "changed-alias"))
    assert sync.search("needle")["documents"] == []
    assert sync.search("changed-alias")["documents"][0]["path"] == "public/person.md"
    graph = sync.keywords
    assert graph is not None
    for path in sync.paths():
        graph.relations.forget(path)
    sync.reconcile()
    assert len(sync.search("changed-alias")["relations"]["documents"]) == 3


@pytest.mark.parametrize("empty", [[], None])
def test_keyword_explicit_empty_fields_survive_inference(
    sync: Synchronizer, empty: list[str] | None
) -> None:
    _write(
        sync,
        "a.md",
        {
            "aliases": empty,
            "definition": "Written definition",
            "central_question": "Written question",
            "parent": empty,
            "related": empty,
            "requires": empty,
        },
        "# A\n",
    )
    _write(sync, "b.md", {}, "# B\n")
    sync.reconcile()
    graph = sync.keywords
    assert graph is not None
    note = graph.note("a.md")
    assert note is not None
    graph.apply(
        "a.md",
        note["hash"],
        Extraction(
            name="Guessed",
            aliases=["Wrong alias"],
            central_question="Wrong question",
            definition="Wrong definition",
            differentia="",
            broader=[{"id": "b"}],
            related=["b"],
            requires=["b"],
        ),
        {"b": "b.md"},
    )
    assert "Wrong alias" not in graph.names("a.md")
    assert graph.note("a.md")["definition"] == "Written definition"
    assert graph.note("a.md")["question"] == "Written question"
    assert graph.parents("a.md") == ([], "user")
    assert graph.describe("a.md")["related"] == []
    assert graph.describe("a.md")["requires"] == []


def test_keyword_complete_explicit_note_needs_no_model(sync: Synchronizer) -> None:
    _write(
        sync,
        "a.md",
        {
            "title": "A",
            "aliases": [],
            "definition": "Written definition",
            "central_question": "Question",
            "differentia": "Written distinction",
            "broader": [],
            "related": [],
            "requires": [],
        },
        "# A\n",
    )
    sync.reconcile()
    graph = sync.keywords
    assert graph is not None
    builder = KeywordBuilder(
        graph, cast(Any, None), sync.text, lambda query, limit: [], sync.reindex
    )
    assert not builder.step(lambda: False)
    assert graph.note("a.md")["definition"] == "Written definition"
    assert graph.note("a.md")["differentia"] == "Written distinction"


def test_keyword_ambiguous_broader_name_is_not_chosen(sync: Synchronizer) -> None:
    for name in ("a", "b"):
        _write(sync, name + ".md", {}, "# Same\n")
    _write(sync, "c.md", {}, "# C\n")
    sync.reconcile()
    graph = sync.keywords
    assert graph is not None
    note = graph.note("c.md")
    graph.apply(
        "c.md",
        note["hash"],
        Extraction(
            name="C",
            central_question="Q",
            definition="D",
            differentia="",
            broader=[{"new": "Same"}],
        ),
        {},
    )
    assert graph.parents("c.md") == ([], "")
    assert graph.find("Same") is None


@pytest.mark.parametrize("change", ["edit", "delete", "rename"])
def test_search_changed_edge_owner_is_not_used(sync: Synchronizer, change: str) -> None:
    _chain(sync)
    owner = sync.documents.root / "public/experiment.md"
    if change == "edit":
        owner.write_text(owner.read_text() + "\nChanged current source.")
    elif change == "delete":
        owner.unlink()
    else:
        owner.rename(owner.with_name("renamed.md"))
    assert sync.search("needle")["relations"]["documents"] == []


def test_search_reindexed_quote_mismatch_remains_unverified(sync: Synchronizer) -> None:
    _chain(sync)
    source = sync.documents.root / "public/evidence.md"
    source.write_text("# Evidence\nDifferent evidence.")
    sync.sync_paths(["public/evidence.md"])
    result = sync.search("needle")["relations"]
    assert [d["path"] for d in result["documents"]] == ["public/experiment.md"]
    assert "unverified_evidence" in result["omitted"]


def test_search_byte_budget_keeps_large_evidence_unverified(sync: Synchronizer) -> None:
    _chain(sync)
    source = sync.documents.root / "public/evidence.md"
    source.write_text(source.read_text() + "\n" + "a" * (512 * 1024))
    sync.sync_paths(["public/evidence.md"])
    result = sync.search("needle")["relations"]
    assert result["read_bytes"] <= 512 * 1024
    assert result["output_bytes"] <= 512 * 1024
    assert "byte_budget" in result["omitted"]
    assert [d["path"] for d in result["documents"]] == ["public/experiment.md"]


def test_search_third_hop_is_not_followed(sync: Synchronizer) -> None:
    _chain(sync)
    quote = "A later judgment revises the first."
    _write(
        sync,
        "public/later.md",
        {
            "semantics": {
                "document_kind": "claim",
                "entities": [_entity("later", "claim"), _entity("judgment", "claim")],
                "assertions": [
                    _assertion(
                        "later",
                        "prov:wasRevisionOf",
                        "judgment",
                        "public/later.md",
                        quote,
                    )
                ],
            }
        },
        "# Later\n" + quote,
    )
    _write(
        sync,
        "public/final.md",
        {
            "semantics": {
                "document_kind": "claim",
                "entities": [_entity("later", "claim")],
            }
        },
        "# Final\nOnly reachable through the third assertion.",
    )
    sync.sync_paths(["public/later.md", "public/final.md"])
    result = sync.search("needle")["relations"]
    assert {d["path"] for d in result["documents"]} == {
        "public/experiment.md",
        "public/decision.md",
        "public/evidence.md",
    }
    assert all(
        edge["predicate"] != "prov:wasRevisionOf"
        for document in result["documents"]
        for edge in document["relation_path"]
    )


@pytest.mark.parametrize("sync", ["memory", "elasticsearch"], indirect=True)
def test_search_cli_and_mcp_include_current_relation_evidence(
    sync: Synchronizer, tmp_path: Path
) -> None:
    import asyncio

    from conftest import service_for

    from markdown_notes_agent.cli import _call_local
    from markdown_notes_agent.runtime import Runtime

    _chain(sync)
    with Runtime(sync.documents.root, tmp_path / "runtime") as runtime:
        service = service_for(runtime, sync)
        service.hub.get(None).keywords = sync.keywords
        cli = _call_local(
            service, "search_documents", {"query": "needle", "folders": ["public"]}
        )
        mcp = asyncio.run(service.search_documents("needle", folders=("public",)))
    assert cli == mcp
    assert cli["documents"][0]["matched_by"]
    keywords = cli["documents"][0]["keywords"]
    assert keywords.keys() == {
        "name",
        "position",
        "definition",
        "source",
        "verification",
    }
    assert keywords["name"] == "Person"
    assert keywords["source"] == "legacy_keyword_tree"
    assert keywords["verification"] == "unverified_model_or_links"
    relation = next(
        d for d in cli["relations"]["documents"] if d["path"] == "public/evidence.md"
    )
    assert relation["seed"] == "public/person.md"
    assert relation["evidence"][-1]["quote"] == "The judgment came from the experiment."
    assert relation["evidence"][-1]["source"]["sha256"] == relation["hash"]
    assert "semantic_support_of_quotes" in cli["relations"]["unverified_scope"]


def test_search_returns_document_defining_entity_endpoint(sync: Synchronizer) -> None:
    quote = "Cache is a narrower storage concept."
    _write(
        sync,
        "cache.md",
        {
            "semantics": {
                "document_kind": "concept",
                "entities": [
                    _entity("cache", "concept"),
                    _entity("storage", "concept"),
                ],
                "assertions": [
                    _assertion("cache", "skos:broader", "storage", "cache.md", quote)
                ],
            }
        },
        "# Needle\n" + quote,
    )
    _write(
        sync,
        "storage.md",
        {
            "semantics": {
                "document_kind": "concept",
                "entities": [{**_entity("storage", "concept"), "label": "Storage"}],
            }
        },
        "# Storage\nStored data persists for later retrieval.",
    )
    sync.reconcile()
    result = sync.search("Needle")["relations"]["documents"]
    assert [document["path"] for document in result] == ["storage.md"]
    assert result[0]["relation_path"][0]["predicate"] == "skos:broader"


def test_keyword_legacy_relation_objects_preserve_explicit_ids(
    sync: Synchronizer,
) -> None:
    _write(sync, "storage.md", {"slug": "storage-id"}, "# Storage\n")
    _write(sync, "cache.md", {"broader": [{"id": "storage-id"}]}, "# Cache\n")
    sync.reconcile()
    assert sync.keywords is not None
    assert sync.keywords.parents("cache.md") == (["storage.md"], "user")


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("invalid", [[{"name": "unknown"}], True])
def test_keyword_invalid_legacy_relation_is_reported(
    sync: Synchronizer, invalid: Any, *, existing: bool
) -> None:
    if existing:
        _write(sync, "invalid.md", {}, "# Previously valid\n")
        sync.reconcile()
    _write(sync, "invalid.md", {"broader": invalid}, "# Invalid\n")
    _write(sync, "valid.md", {}, "# Valid\n")
    result = sync.reconcile()
    assert any(item["path"] == "invalid.md" for item in result["diagnostics"])
    assert sync.keywords is not None
    assert sync.keywords.note("invalid.md") is None
    assert sync.keywords.note("valid.md") is not None


@pytest.mark.parametrize(
    "description",
    [{}, {"kind": "unknown"}, {"label": None}, {"aliases": []}, {"definition": None}],
)
def test_search_does_not_return_empty_endpoint_redeclaration(
    sync: Synchronizer, description: dict[str, Any]
) -> None:
    _chain(sync)
    assertion = _assertion(
        "experiment", "prov:wasGeneratedBy", "experiment", "unused.md", "Missing"
    )
    assertion["evidence"][0]["source"]["id"] = "https://example.test/unverified"
    _write(
        sync,
        "public/unverified.md",
        {
            "semantics": {
                "document_kind": "experiment",
                "entities": [{**_entity("experiment", "activity"), **description}],
                "assertions": [assertion],
            }
        },
        "# Unverified\nUnrelated source text.",
    )
    sync.sync_paths(["public/unverified.md"])
    result = sync.search("needle")
    paths = {document["path"] for document in result["relations"]["documents"]}
    assert "public/unverified.md" not in paths
    assert {"public/experiment.md", "public/evidence.md"} <= paths


@pytest.mark.parametrize("sync", ["memory", "elasticsearch"], indirect=True)
@pytest.mark.parametrize("connected", [False, True])
def test_search_json_empty_aliases_override_legacy_names(
    sync: Synchronizer, *, connected: bool
) -> None:
    if not connected:
        sync.keywords = None
    _write(
        sync,
        "a.md",
        {
            "aliases": ["obsoletealias"],
            "semantics": {
                "document_kind": "concept",
                "entities": [{**_entity("a", "concept"), "aliases": []}],
            },
        },
        "# Current\n",
    )
    sync.reconcile()
    indexed = sync.index.metadata(["a.md"])["a.md"]
    assert "obsoletealias" not in indexed["names"]
    result = sync.search("obsoletealias")
    assert [document["path"] for document in result["documents"]] == ["a.md"]
    assert "names" not in result["documents"][0]["matched_by"]


def test_keyword_json_empty_assertions_override_legacy_parent(
    sync: Synchronizer,
) -> None:
    _write(sync, "b.md", {}, "# B\n")
    _write(
        sync,
        "a.md",
        {"parent": "b.md", "semantics": {"assertions": []}},
        "# A\n",
    )
    sync.reconcile()
    assert sync.keywords is not None
    assert sync.keywords.parents("a.md") == ([], "user")


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({}, "Guessed distinction"),
        ({"differentia": "Written distinction"}, "Written distinction"),
        ({"differentia": ""}, ""),
        ({"differentia": None}, ""),
    ],
)
def test_keyword_differentia_only_missing_values_are_inferred(
    sync: Synchronizer, metadata: dict[str, Any], expected: str
) -> None:
    _write(sync, "a.md", metadata, "# A\n")
    _write(sync, "b.md", {}, "# B\n")
    sync.reconcile()
    graph = sync.keywords
    assert graph is not None
    note = graph.note("a.md")
    assert note is not None
    assert note["differentia"] == (metadata.get("differentia") or "")
    graph.apply(
        "a.md",
        note["hash"],
        Extraction(
            name="A",
            aliases=[],
            central_question="Question",
            definition="Definition",
            differentia="Guessed distinction",
            broader=[{"id": "b"}],
        ),
        {"b": "b.md"},
    )
    assert graph.note("a.md")["differentia"] == expected
    assert graph.split_input("b.md")[0]["differentia"] == expected


@pytest.mark.parametrize("declared", ["a.md", "b.md"])
@pytest.mark.parametrize("value", ["manual-candidate", None, ""])
def test_keyword_explicit_same_as_blocks_model_merge_in_both_directions(
    sync: Synchronizer, declared: str, value: str | None
) -> None:
    rules = sync.documents.root / "notes/VAULT.md"
    rules.parent.mkdir()
    rules.write_text("---\nmerge: auto\n---\n")
    for name in ("a.md", "b.md"):
        _write(
            sync,
            "notes/" + name,
            {"same_as": value} if name == declared else {},
            "# " + name + "\n",
        )
    sync.reconcile()
    graph = sync.keywords
    assert graph is not None
    merges: list[tuple[str, str, str]] = []
    graph.on_same = lambda a, b, name: merges.append((a, b, name))
    path = "notes/" + declared
    snapshot = parse_semantics((sync.documents.root / path).read_text(), path=path)
    assert snapshot.legacy.values["same_as"] == value
    assert "same_as" in snapshot.legacy.declared_fields
    assert graph.explicit(path)["same_as"] == (value or "")
    for name in ("a.md", "b.md"):
        path = "notes/" + name
        note = graph.note(path)
        assert note is not None and note["merge"]
        report = graph.apply(
            path,
            note["hash"],
            Extraction(
                name=name,
                central_question="Question",
                definition="Definition",
                differentia="Distinction",
                same_as="a" if name == "b.md" else "",
            ),
            {"a": "notes/a.md"},
        )
    assert report["same_as"] in {None, "ignored"}
    assert graph.describe("notes/b.md")["same_question"] == []
    assert merges == []
