"""Keyword tree: user links, model extraction, generated concepts, merging."""

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from markdown_notes_agent.documents import Documents
from markdown_notes_agent.keywords import (
    NODE,
    SPLIT_LIMIT,
    Extraction,
    KeywordBuilder,
    KeywordGraph,
    Split,
    parse_note,
)
from markdown_notes_agent.requests import digest
from markdown_notes_agent.rules import RULES_FILE
from markdown_notes_agent.search import SearchIndex
from markdown_notes_agent.sync import Synchronizer
from markdown_notes_agent.vaults import VaultPolicy


def _write(root: Path, path: str, text: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _observe(graph: KeywordGraph, root: Path, path: str, text: str) -> None:
    _write(root, path, text)
    graph.observe(path, text, digest(text.encode()))


@pytest.fixture
def graph(tmp_path: Path) -> Iterator[KeywordGraph]:
    root = tmp_path / "vault"
    root.mkdir()
    opened = KeywordGraph(root, tmp_path / "state/keywords.sqlite", VaultPolicy(root))
    yield opened
    opened.close()


def _extract(graph: KeywordGraph, path: str, **fields: Any) -> dict[str, Any]:
    ids = {name: name for name in fields.pop("ids", [])}
    values: dict[str, Any] = {
        "central_question": f"What is {path}?",
        "name": path,
        "definition": f"{path} is a kind of thing.",
        "differentia": "",
        **fields,
    }
    note = graph.note(path)
    assert note is not None
    return graph.apply(path, note["hash"], Extraction.model_validate(values), ids)


def test_front_matter_and_list_links_are_parsed() -> None:
    parsed = parse_note(
        "---\ntitle: Chairs\nslug: chairs\nparent: furniture\n---\n"
        "- [Office chair](office-chair.md)\n"
        "- see [[stool|Stools]] and [[bench]]\n"
        "Text with [a link](/wiki/table/).\n",
        "fallback",
    )
    assert parsed["title"] == "Chairs"
    assert parsed["slug"] == "chairs"
    assert parsed["links"] == [
        ("furniture", "parent", ""),
        ("office-chair.md", "list", "Office chair"),
        ("stool", "prose", "Stools"),
        ("bench", "prose", ""),
        ("/wiki/table/", "prose", "a link"),
    ]


def test_user_parent_by_slug_and_index_list(graph: KeywordGraph) -> None:
    root = graph.root
    _observe(
        graph,
        root,
        "docs/chair.md",
        "---\nslug: chair\nparent: furniture\n---\n# Chair\n",
    )
    # The parent appears after its child: the link resolves once it exists.
    _observe(
        graph,
        root,
        "docs/furniture/index.md",
        "---\nslug: furniture\n---\n# Furniture\n",
    )
    _observe(
        graph, root, "docs/rooms.md", "# Rooms\n- [[docs/furniture/index|Furniture]]\n"
    )
    assert graph.parents("docs/chair.md") == (["docs/furniture/index.md"], "user")
    assert graph.parents("docs/furniture/index.md") == (["docs/rooms.md"], "user")
    assert graph.position("docs/chair.md") == ["Rooms", "Furniture"]
    assert graph.children("docs/furniture/index.md") == ["docs/chair.md"]
    # Link text is another name of the note it points to.
    assert "Furniture" in graph.names("docs/furniture/index.md")


def test_user_parent_wins_over_the_model(graph: KeywordGraph) -> None:
    root = graph.root
    _observe(graph, root, "a.md", "# A\n")
    _observe(graph, root, "b.md", "# B\n")
    _observe(graph, root, "c.md", "---\nparent: a\n---\n# C\n")
    _extract(graph, "c.md", broader=[{"id": "b"}], ids=["b.md"])
    assert graph.parents("c.md")[1] == "user"
    assert graph.parents("c.md")[0] == ["a.md"]


def test_new_broader_name_needs_two_supporters(graph: KeywordGraph) -> None:
    root = graph.root
    for name in ("oak", "pine", "rose"):
        _observe(graph, root, f"{name}.md", f"# {name}\n")
    first = _extract(graph, "oak.md", broader=[{"new": "Tree"}])
    assert first["created"] == []
    assert graph.parents("oak.md") == ([], "")
    second = _extract(graph, "pine.md", broader=[{"new": "tree"}])
    assert second["created"] == ["tree"]
    node = NODE + "::tree"
    assert graph.parents("oak.md")[0] == [node]
    assert graph.children(node) == ["oak.md", "pine.md"]
    # One supporter leaves: the concept goes back to waiting.
    _extract(graph, "pine.md", broader=[{"new": "Conifer"}])
    assert graph.describe(node) is None
    assert graph.parents("oak.md") == ([], "")
    # A third note naming it brings it back.
    _extract(graph, "rose.md", broader=[{"new": "Tree"}])
    assert graph.parents("oak.md")[0] == [node]


def test_new_name_joins_an_existing_note(graph: KeywordGraph) -> None:
    root = graph.root
    _observe(graph, root, "furniture.md", "# Furniture\n")
    _observe(graph, root, "chair.md", "# Chair\n")
    _extract(graph, "chair.md", broader=[{"new": "furniture"}])
    assert graph.parents("chair.md") == (["furniture.md"], "model")


def test_cycles_and_other_trees_are_refused(graph: KeywordGraph) -> None:
    root = graph.root
    _observe(graph, root, "a.md", "# A\n")
    _observe(graph, root, "b.md", "# B\n")
    _write(root, "private/" + RULES_FILE, "---\ntree: private\n---\n")
    _observe(graph, root, "private/p.md", "# P\n")
    _extract(graph, "a.md", broader=[{"id": "b.md"}], ids=["b.md"])
    report = _extract(
        graph,
        "b.md",
        broader=[{"id": "a.md"}, {"id": "private/p.md"}],
        ids=["a.md", "private/p.md"],
    )
    assert graph.parents("b.md") == ([], "")
    assert set(report["ignored"]) == {"a.md", "private/p.md"}


def test_deleted_note_moves_children_up(graph: KeywordGraph) -> None:
    root = graph.root
    for name in ("top", "middle", "leaf"):
        _observe(graph, root, f"{name}.md", f"# {name}\n")
    _extract(graph, "middle.md", broader=[{"id": "top.md"}], ids=["top.md"])
    _extract(graph, "leaf.md", broader=[{"id": "middle.md"}], ids=["middle.md"])
    (root / "middle.md").unlink()
    graph.forget("middle.md")
    assert graph.parents("leaf.md")[0] == ["top.md"]
    # The child is placed again by the model.
    assert graph.next_note() == "leaf.md"


def test_same_question_is_merged_only_where_rules_allow(graph: KeywordGraph) -> None:
    root = graph.root
    merges: list[tuple[str, str, str]] = []
    graph.on_same = lambda a, b, name: merges.append((a, b, name))
    _observe(graph, root, "notes/a.md", "# Retry\n")
    _observe(graph, root, "notes/b.md", "# Retries\n")
    _extract(graph, "notes/a.md")
    report = _extract(graph, "notes/b.md", same_as="notes/a.md", ids=["notes/a.md"])
    assert report["same_as"] == "reported"
    assert merges == []
    _write(root, "notes/" + RULES_FILE, "---\nmerge: auto\n---\n")
    graph.reconcile(["notes/a.md", "notes/b.md"], lambda p: ("", ""))
    report = _extract(graph, "notes/b.md", same_as="notes/a.md", ids=["notes/a.md"])
    assert report["same_as"] == "merge_queued"
    assert merges == [("notes/b.md", "notes/a.md", "notes/b.md")]
    # Asked once per pair.
    _extract(graph, "notes/b.md", same_as="notes/a.md", ids=["notes/a.md"])
    assert len(merges) == 1


def test_keywords_off_and_reconcile(graph: KeywordGraph) -> None:
    root = graph.root
    _write(root, "inbox/" + RULES_FILE, "---\nkeywords: false\n---\n")
    _write(root, "inbox/x.md", "# X\n")
    _write(root, "y.md", "# Y\n")

    def read(path: str) -> tuple[str, str]:
        text = (root / path).read_text()
        return text, digest(text.encode())

    graph.reconcile(["inbox/x.md", "y.md"], read)
    assert graph.next_note() == "y.md"
    assert graph.status()["notes"] == 1
    graph.reconcile(["inbox/x.md"], read)
    assert graph.note("y.md") is None


def test_large_concept_is_split(graph: KeywordGraph) -> None:
    root = graph.root
    _observe(graph, root, "animal.md", "# Animal\n")
    kids = [f"k{n:02d}.md" for n in range(SPLIT_LIMIT + 1)]
    for kid in kids:
        _observe(graph, root, kid, f"# {kid}\n")
        _extract(graph, kid, broader=[{"id": "animal.md"}], ids=["animal.md"])
    assert graph.next_split() == "animal.md"
    children = graph.split_input("animal.md")
    ids = {f"c{n}": child["id"] for n, child in enumerate(children)}
    created = graph.apply_split(
        "animal.md",
        Split.model_validate(
            {
                "groups": [
                    {"name": "Bird", "definition": "", "children": ["c0", "c1", "c2"]},
                    {"name": "Lonely", "definition": "", "children": ["c3"]},
                ]
            }
        ),
        ids,
    )
    assert created == ["Bird"]
    assert graph.parents("k00.md")[0] == [NODE + "::bird"]
    assert graph.position("k00.md") == ["Animal", "Bird"]
    assert graph.parents("k03.md")[0] == ["animal.md"]
    assert graph.next_split() is None


class _Model:
    def __init__(self, answers: list[dict[str, Any]]) -> None:
        self.answers = answers
        self.prompts: list[str] = []

    def ask(
        self,
        prompt: str,
        schema: type[BaseModel],
        is_cancelled: Callable[[], bool],
        role: str = "draft",
    ) -> dict[str, Any]:
        assert role == "keywords"
        self.prompts.append(prompt)
        return self.answers.pop(0)


def test_builder_maps_short_ids_and_records_failures(graph: KeywordGraph) -> None:
    root = graph.root
    _observe(graph, root, "furniture.md", "# Furniture\nThings to sit on.\n")
    _observe(graph, root, "chair.md", "# Chair\nA seat with a back.\n")
    graph.apply(
        "furniture.md",
        graph.note("furniture.md")["hash"],  # type: ignore[index]
        Extraction(
            central_question="What is furniture?",
            name="Furniture",
            definition="Furniture is a kind of household object.",
            differentia="",
        ),
        {},
    )

    def read(path: str) -> tuple[str, str]:
        text = (root / path).read_text()
        return text, digest(text.encode())

    reindexed: list[set[str]] = []
    model = _Model(
        [
            {
                "central_question": "What is a chair?",
                "name": "Chair",
                "aliases": ["Seat"],
                "definition": "A chair is a kind of furniture with a back.",
                "differentia": "with a back",
                "broader": [{"id": "c1", "new": "", "under": ""}],
                "same_as": "",
                "related": ["c9"],
                "requires": [],
            },
            {"name": "broken"},
        ]
    )
    builder = KeywordBuilder(
        graph, model, read, lambda q, k: ["chair.md", "furniture.md"], reindexed.append
    )
    assert builder.step(lambda: False)
    assert graph.parents("chair.md") == (["furniture.md"], "model")
    assert "Seat" in graph.names("chair.md")
    assert '"id":"c1"' in model.prompts[0]
    assert "furniture.md" not in json.dumps(
        json.loads(model.prompts[0].split("\n", 1)[1])["input"]["candidates"]
    )
    assert reindexed == [{"chair.md"}]
    _observe(graph, root, "chair.md", "# Chair\nChanged.\n")
    assert builder.step(lambda: False)
    note = graph.note("chair.md")
    assert note is not None
    assert note["definition"] == "A chair is a kind of furniture with a back."
    assert graph.status()["queued"] == 1
    # A malformed answer counts as a failed attempt; the note stays queued.
    assert graph.next_note() == "chair.md"


@pytest.fixture
def indexed(tmp_path: Path) -> Iterator[tuple[Synchronizer, KeywordGraph]]:
    url = os.environ.get("MARKDOWN_NOTES_AGENT_TEST_SEARCH_URL")
    if not url:
        pytest.skip("requires explicit Elasticsearch test endpoint")
    root = tmp_path / "vault"
    root.mkdir()
    policy = VaultPolicy(root)
    index = SearchIndex(url, str(root))
    sync = Synchronizer(Documents(root), index, None, policy)
    graph = KeywordGraph(root, tmp_path / "state/keywords.sqlite", policy)
    sync.keywords = graph
    try:
        yield sync, graph
    finally:
        graph.close()
        index.delete()
        index.close()


def test_names_are_searchable_and_follow_link_text(
    indexed: tuple[Synchronizer, KeywordGraph],
) -> None:
    sync, graph = indexed
    root = sync.documents.root
    _write(root, "idem.md", "# Idempotency key\nA token sent with each request.\n")
    sync.reconcile()
    assert graph.next_note() == "idem.md"
    note = graph.note("idem.md")
    assert note is not None
    graph.apply(
        "idem.md",
        note["hash"],
        Extraction(
            central_question="How are duplicate requests caught?",
            name="Idempotency key",
            aliases=["dedupe token"],
            definition="An idempotency key is a kind of request token.",
            differentia="",
        ),
        {},
    )
    sync.reindex({"idem.md"})
    found = sync.search("dedupe token", 5)
    assert [d["path"] for d in found["documents"]] == ["idem.md"]
    # A link elsewhere gives the note another searchable name.
    _write(root, "index.md", "# Index\n- [Duplicate guard](idem.md)\n")
    sync.sync_paths(["index.md"])
    sync.index.refresh()
    assert graph.parents("idem.md") == (["index.md"], "user")
    hits = [h["path"] for h in sync.index.find("Duplicate guard", 5)]
    assert "idem.md" in hits
    (root / "index.md").unlink()
    sync.reconcile()
    assert graph.parents("idem.md") == ([], "")


def test_same_question_waits_without_looping(graph: KeywordGraph) -> None:
    root = graph.root
    _write(root, RULES_FILE, "---\nmerge: auto\n---\n")
    merges: list[tuple[str, str, str]] = []
    graph.on_same = lambda a, b, name: merges.append((a, b, name))
    _observe(graph, root, "a.md", "# A\n")
    _observe(graph, root, "b.md", "# B\n")
    report = _extract(graph, "a.md", same_as="b.md", ids=["b.md"])
    assert report["same_as"] == "waiting"
    # a is placed; b comes next instead of a again.
    assert graph.next_note() == "b.md"
    _extract(graph, "b.md")
    assert merges == [("a.md", "b.md", "a.md")]
    assert graph.next_note() is None


def test_moved_note_keeps_its_name_usable(graph: KeywordGraph) -> None:
    root = graph.root
    _observe(graph, root, "docker.md", "# Docker\n")
    _observe(graph, root, "image.md", "# Image\n")
    _extract(graph, "image.md", broader=[{"id": "docker.md"}], ids=["docker.md"])
    (root / "docker.md").unlink()
    graph.forget("docker.md")
    _observe(graph, root, "tools/docker.md", "# Docker\n")
    _extract(graph, "image.md", broader=[{"new": "Docker"}])
    assert graph.parents("image.md")[0] == ["tools/docker.md"]


def test_forget_uses_the_user_parent(graph: KeywordGraph) -> None:
    root = graph.root
    _observe(graph, root, "top.md", "# Top\n")
    _observe(graph, root, "mid.md", "---\nparent: top\n---\n# Mid\n")
    _observe(graph, root, "leaf.md", "# Leaf\n")
    _extract(graph, "leaf.md", broader=[{"id": "mid.md"}], ids=["mid.md"])
    (root / "mid.md").unlink()
    graph.forget("mid.md")
    assert graph.parents("leaf.md")[0] == ["top.md"]


def test_explicit_parent_prevents_hidden_model_edge(graph: KeywordGraph) -> None:
    root = graph.root
    _observe(graph, root, "top.md", "# Top\n")
    _observe(graph, root, "m1.md", "# M1\n")
    _observe(graph, root, "m2.md", "---\nparent: top\n---\n# M2\n")
    _extract(graph, "m2.md", broader=[{"id": "m1.md"}], ids=["m1.md"])
    report = _extract(graph, "m1.md", broader=[{"id": "m2.md"}], ids=["m2.md"])
    assert report["ignored"] == []
    assert graph.parents("m1.md") == (["m2.md"], "model")
    assert graph.parents("m2.md") == (["top.md"], "user")


def test_demoted_concept_leaves_no_ghost_supporters(graph: KeywordGraph) -> None:
    root = graph.root
    for name in ("a", "b", "c"):
        _observe(graph, root, f"{name}.md", f"# {name}\n")
    _extract(graph, "a.md", broader=[{"new": "Beta"}])
    _extract(graph, "b.md", broader=[{"new": "Beta"}])
    _extract(graph, "b.md", broader=[{"new": "Gamma"}])
    # Beta is gone; one more note proposing it does not bring it back alone.
    assert graph.describe(NODE + "::beta") is None
    report = _extract(graph, "c.md", broader=[{"new": "Gamma"}])
    assert report["created"] == ["Gamma"]
    assert graph.status()["waiting_names"] == 1


def test_user_children_do_not_trigger_splits(graph: KeywordGraph) -> None:
    root = graph.root
    listing = "".join(f"- [[k{n:02d}]]\n" for n in range(SPLIT_LIMIT + 5))
    _observe(graph, root, "index.md", "# Index\n" + listing)
    for n in range(SPLIT_LIMIT + 5):
        _observe(graph, root, f"k{n:02d}.md", f"# k{n}\n")
    _observe(graph, root, "extra.md", "# Extra\n")
    _extract(graph, "extra.md", broader=[{"id": "index.md"}], ids=["index.md"])
    assert len(graph.children("index.md")) == SPLIT_LIMIT + 6
    assert graph.next_split() is None


def test_relative_link_prefers_the_notes_folder(graph: KeywordGraph) -> None:
    root = graph.root
    _observe(graph, root, "foo.md", "# Root foo\n")
    _observe(graph, root, "sub/foo.md", "# Sub foo\n")
    _observe(graph, root, "sub/own.md", "# Own\n- [x](foo.md)\n")
    assert graph.parents("sub/foo.md")[0] == ["sub/own.md"]
    assert graph.parents("foo.md") == ([], "")


def test_model_outage_pauses_without_failing_notes(graph: KeywordGraph) -> None:
    from markdown_notes_agent.errors import ModelError

    root = graph.root
    _observe(graph, root, "a.md", "# A\n")

    class Down:
        def ask(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise ModelError("The model did not answer in time.")

    def read(path: str) -> tuple[str, str]:
        text = (root / path).read_text()
        return text, digest(text.encode())

    builder = KeywordBuilder(graph, Down(), read, lambda q, k: [], lambda p: None)
    assert builder.step(lambda: False)
    assert not builder.step(lambda: False)  # paused
    assert graph.next_note() == "a.md"
    assert graph.status()["failed_count"] == 0


def test_builder_passes_explicit_values_in_one_json_envelope(
    graph: KeywordGraph,
) -> None:
    text = '---\ntitle: Written name\naliases: []\ndifferentia: ""\n---\n# A\n'
    _observe(graph, graph.root, "a.md", text)
    model = _Model(
        [
            {
                "central_question": "What is A?",
                "name": "Guessed name",
                "aliases": ["Guessed alias"],
                "definition": "A is a concept.",
                "differentia": "Guessed distinction",
            }
        ]
    )
    builder = KeywordBuilder(
        graph,
        model,
        lambda path: (text, digest(text.encode())),
        lambda query, limit: [],
        lambda paths: None,
    )
    assert builder.step(lambda: False)
    value = json.loads(model.prompts[0].split("\n", 1)[1])
    explicit = value["input"]["explicit_values"]
    assert explicit["name"] == "Written name"
    assert explicit["aliases"] == [] and explicit["differentia"] == ""
    assert value["policy"]["field_precedence"] == ["explicit_values", "inference"]
    assert value["policy"]["inference"] == "missing_fields_only"
    assert graph.note("a.md")["name"] == "Written name"
    assert graph.note("a.md")["differentia"] == ""
    assert "Guessed alias" not in graph.names("a.md")


@pytest.mark.parametrize("rules", [b"x" * (32 * 1024 + 1), b"\xff"])
def test_invalid_rules_do_not_starve_other_notes(
    graph: KeywordGraph, rules: bytes
) -> None:
    root = graph.root
    _observe(graph, root, "a/item.md", "# A\n")
    _observe(graph, root, "b.md", "# B\n")
    (root / "a/VAULT.md").write_bytes(rules)
    model = _Model(
        [
            {
                "central_question": "What is B?",
                "name": "B",
                "definition": "B",
                "differentia": "",
            }
        ]
    )

    def read(path: str) -> tuple[str, str]:
        text = (root / path).read_text()
        return text, digest(text.encode())

    builder = KeywordBuilder(graph, model, read, lambda q, k: [], lambda p: None)
    assert builder.step(lambda: False)
    assert not model.prompts
    assert graph.next_note() == "b.md"
    assert builder.step(lambda: False)
    assert len(model.prompts) == 1
    assert graph.note("b.md")["name"] == "B"


def test_previous_prompt_extraction_is_queued_for_new_policy(
    graph: KeywordGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    from markdown_notes_agent import keywords

    current_version = keywords.PROMPT_VERSION
    _observe(graph, graph.root, "a.md", "# A\n")
    monkeypatch.setattr(keywords, "PROMPT_VERSION", 1)
    _extract(graph, "a.md")
    assert graph.next_note() is None
    monkeypatch.setattr(keywords, "PROMPT_VERSION", current_version)
    assert graph.next_note() == "a.md"
