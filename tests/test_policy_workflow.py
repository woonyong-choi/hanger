import copy
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from hanger.closets import ClosetPolicy
from hanger.config import Settings
from hanger.documents import Documents
from hanger.errors import ConflictError, RequestError
from hanger.model import CliModel, Intent, Planner
from hanger.requests import Change, Proposal, issue_key
from hanger.runtime import Runtime
from hanger.search import SearchIndex
from hanger.sync import Synchronizer
from hanger.trace import Trace
from hanger.workflows import run_workflow


def payload(prompt: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(prompt.split("\n", 1)[1]))


def decision(path: str = "a/note.md", new: str = "new") -> dict[str, Any]:
    return {
        "action": "supplement",
        "reason": "requested edit",
        "sources": [path],
        "uncertainties": [],
        "evidence": [{"path": path, "quote": "old"}],
        "changes": [
            {
                "path": path,
                "operation": "replace",
                "replacements": [{"old": "old", "new": new}],
            }
        ],
    }


class Script(CliModel):
    def __init__(self, choices: list[dict[str, Any]]) -> None:
        super().__init__()
        self.choices = choices
        self.drafts: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []
        self.checks = [{"approved": True, "issues": []}]
        self.after_draft: Callable[[], object] = lambda: None
        self.after_review: Callable[[], object] = lambda: None

    def generate(self, prompt: str, is_cancelled: Callable[[], bool]) -> dict[str, Any]:
        self.drafts.append(payload(prompt))
        self.after_draft()
        choice = self.choices.pop(0) if len(self.choices) > 1 else self.choices[0]
        return {"decision": copy.deepcopy(choice)}

    def review(self, prompt: str, is_cancelled: Callable[[], bool]) -> dict[str, Any]:
        self.reviews.append(payload(prompt))
        self.after_review()
        check = self.checks.pop(0) if len(self.checks) > 1 else self.checks[0]
        return {"review": check}


@pytest.fixture
def sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Synchronizer:
    for folder in ("a", "b"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "note.md").write_text(
            "# Note\nold\ncondition: version 2 only"
        )
        (tmp_path / folder / "HANGER.md").write_text(f"Rules for {folder}")
    index = SearchIndex("http://127.0.0.1:9", str(tmp_path))
    result = Synchronizer(
        Documents(tmp_path), index, Settings(), ClosetPolicy(tmp_path)
    )
    monkeypatch.setattr(result, "search", lambda *a, **k: {"documents": []})
    return result


def execute(sync: Synchronizer, model: Script, **extra: Any) -> dict[str, Any]:
    document = sync.read("a/note.md")
    context = {
        "request": {
            "kind": "edit",
            "profile": "wiki",
            "instruction": "replace old with new",
            "allow_delete": False,
            "folders": [],
        },
        "documents": {"a/note.md": document},
        "signatures": {"a/note.md": document["signature"]},
        **extra,
    }
    graph = StateGraph(dict)
    graph.add_node(
        "write",
        lambda state: run_workflow(context, model, sync, Trace(), lambda: False),
    )
    graph.add_edge(START, "write")
    graph.add_edge("write", END)
    return graph.compile(checkpointer=InMemorySaver()).invoke(
        {}, {"configurable": {"thread_id": "policy-test"}}, durability="sync"
    )


def test_new_target_loads_own_policy_before_redraft_and_review(
    sync: Synchronizer,
) -> None:
    create = {
        **decision(),
        "action": "new",
        "changes": [{"path": "b/new.md", "operation": "create", "content": "new"}],
    }
    model = Script([create])
    result = execute(sync, model)
    assert len(model.drafts) == 2
    policy = model.drafts[-1]["policy"]
    assert policy["source_rules"]["a/note.md"] == ["a/HANGER.md"]
    assert policy["target_rules"]["b/new.md"] == ["b/HANGER.md"]
    assert model.reviews[0]["policy"] == policy
    assert result["policy_dependencies"]["b/HANGER.md"]


def test_known_target_policy_is_bound_without_extra_model_call(
    sync: Synchronizer,
) -> None:
    (sync.documents.root / "HANGER.md").write_text("Shared rules")
    create = {
        **decision(),
        "action": "new",
        "changes": [{"path": "a/new.md", "operation": "create", "content": "new"}],
    }
    model = Script([create])
    execute(sync, model)
    assert len(model.drafts) == 1
    policy = model.reviews[-1]["policy"]
    assert policy["target_rules"]["a/new.md"] == ["HANGER.md", "a/HANGER.md"]
    assert list(policy["rule_catalog"]).count("HANGER.md") == 1


@pytest.mark.parametrize("discovery", ["read_more", "similar"])
def test_extra_sources_rebuild_scoped_rules_permissions_and_keywords(
    sync: Synchronizer, monkeypatch: pytest.MonkeyPatch, discovery: str
) -> None:
    (sync.documents.root / "b/HANGER.md").write_text(
        "---\nwrite: false\n---\nDo not rewrite"
    )
    if discovery == "read_more":
        first = {
            **decision(),
            "action": "defer",
            "changes": [],
            "read_more": ["b/note.md"],
        }
    else:
        first = {
            **decision(),
            "action": "new",
            "changes": [
                {"path": "a/new.md", "operation": "create", "content": "# new"}
            ],
        }
        monkeypatch.setattr(
            sync, "search", lambda *a, **k: {"documents": [{"path": "b/note.md"}]}
        )
    monkeypatch.setattr(
        sync, "keywords", SimpleNamespace(summary=lambda path: {"name": path})
    )
    second = {**decision(), "action": "keep", "changes": []}
    model = Script([first, second])
    execute(sync, model, reads_left=1, listed=[{"path": "b/note.md"}])
    last = model.drafts[-1]
    assert "b/note.md" in last["input"]["documents"]
    assert last["input"]["read_only"] == ["b/note.md"]
    assert last["input"]["keywords"]["b/note.md"] == {"name": "b/note.md"}
    assert last["policy"]["source_rules"]["b/note.md"] == ["b/HANGER.md"]


@pytest.mark.parametrize("stage", ["draft", "review"])
@pytest.mark.parametrize(
    "mutation", ["change", "create", "delete", "ignore", "applied_ignore"]
)
def test_policy_change_during_model_call_rejects_result(
    sync: Synchronizer, stage: str, mutation: str
) -> None:
    model = Script([decision()])
    root = sync.documents.root
    actions = {
        "change": lambda: (root / "a/HANGER.md").write_text("different"),
        "create": lambda: (root / "HANGER.md").write_text("new root policy"),
        "delete": lambda: (root / "a/HANGER.md").unlink(),
        "ignore": lambda: (root / ".hangerignore").write_text("a/"),
        "applied_ignore": lambda: sync.policy.update("a/"),
    }
    setattr(model, f"after_{stage}", actions[mutation])
    with pytest.raises(ConflictError):
        execute(sync, model)
    assert "old" in (root / "a/note.md").read_text()


def test_same_content_has_no_proposal_or_review(sync: Synchronizer) -> None:
    model = Script([decision(new="old")])
    with pytest.raises(RequestError, match="unchanged"):
        execute(sync, model)
    assert model.reviews == []
    assert "old" in (sync.documents.root / "a/note.md").read_text()


@pytest.mark.parametrize("defect", ["delete", "evidence", "read_only"])
def test_runtime_constraints_reject_unauthorized_model_changes(
    sync: Synchronizer, defect: str
) -> None:
    choice = decision()
    if defect == "delete":
        choice["changes"] = [{"path": "a/note.md", "operation": "delete"}]
    elif defect == "evidence":
        choice["evidence"][0]["quote"] = "imaginary source"
    else:
        (sync.documents.root / "a/HANGER.md").write_text("---\nwrite: false\n---")
    with pytest.raises(RequestError):
        execute(sync, Script([choice]))


def test_rule_budget_rejects_instead_of_dropping_root(sync: Synchronizer) -> None:
    (sync.documents.root / "HANGER.md").write_text("x" * (32 * 1024))
    with pytest.raises(RequestError) as error:
        execute(sync, Script([decision()]))
    assert error.value.code == "rules_limit"


@pytest.mark.parametrize("restart", [False, True])
def test_policy_change_after_queueing_blocks_apply(
    tmp_path: Path, *, restart: bool
) -> None:
    root = tmp_path / "closet"
    root.mkdir()
    (root / "note.md").write_text("old")
    state = tmp_path / "state"
    runtime = Runtime(root, state)
    key = issue_key(runtime._secret)
    runtime.submit(
        key,
        Proposal(
            changes=(
                Change(
                    path="note.md",
                    before=runtime.documents.read_hash("note.md"),
                    after="new",
                ),
            )
        ),
    )
    if restart:
        runtime.close()
        runtime = Runtime(root, state)
    try:
        (root / "HANGER.md").write_text("Keep exact text")
        result = runtime.run_next()
        assert result is not None and result["status"] == "conflict"
        assert (root / "note.md").read_text() == "old"
    finally:
        runtime.close()


def test_reviewed_policy_snapshot_survives_restart(
    sync: Synchronizer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hanger.model import Intent, Planner

    model = Script([decision()])
    monkeypatch.setattr(sync, "terms", lambda query: [])
    planner = Planner(sync, model)
    state = tmp_path.parent / (tmp_path.name + "-state")

    class InterruptedRuntime(Runtime):
        def _apply(self, state: Any) -> dict[str, Any]:
            raise InterruptedError("stop before apply")

    runtime = InterruptedRuntime(sync.documents.root, state, planner)
    key = issue_key(runtime._secret)
    runtime.submit_intent(
        key,
        Intent(
            kind="edit",
            query="note",
            instruction="edit old",
            paths=("a/note.md",),
            profile="wiki",
        ),
    )

    with pytest.raises(InterruptedError):
        runtime.run_next()
    runtime.close()
    (sync.documents.root / "a/HANGER.md").write_text("Changed after review")
    with Runtime(sync.documents.root, state, planner) as resumed:
        result = resumed.run_next()
    assert result is not None and result["status"] == "conflict"
    assert len(model.drafts) == len(model.reviews) == 1
    assert "old" in (sync.documents.root / "a/note.md").read_text()


def test_nearer_write_setting_overrides_root_without_global_stop(
    sync: Synchronizer,
) -> None:
    (sync.documents.root / "HANGER.md").write_text("---\nwrite: false\n---\nRoot")
    (sync.documents.root / "a/HANGER.md").write_text("---\nwrite: true\n---\nChild")
    assert sync.policy.writable("a/note.md")
    assert not sync.policy.writable("b/note.md")


def test_aggregate_rule_budget_rejects_disjoint_folder_overflow(
    sync: Synchronizer,
) -> None:
    for folder in ("a", "b"):
        (sync.documents.root / folder / "HANGER.md").write_text("x" * (17 * 1024))
    with pytest.raises(RequestError) as error:
        sync.policy.context(["a/note.md", "b/note.md"])
    assert error.value.code == "rules_limit"


def test_legacy_queued_proposal_without_policy_snapshot_conflicts(
    tmp_path: Path,
) -> None:
    from hanger.requests import validate_key

    root = tmp_path / "closet"
    root.mkdir()
    (root / "note.md").write_text("old")
    with Runtime(root, tmp_path / "state") as runtime:
        key = issue_key(runtime._secret)
        proposal = Proposal(
            changes=(
                Change(
                    path="note.md",
                    before=runtime.documents.read_hash("note.md"),
                    after="new",
                ),
            )
        )
        runtime._enqueue(
            key,
            {"proposal": proposal.model_dump(mode="json")},
            proposal.fingerprint,
            validate_key(key, runtime._secret),
        )
        result = runtime.run_next()
    assert result is not None and result["status"] == "conflict"
    assert "lacks policy dependencies" in result["error"]
    assert (root / "note.md").read_text() == "old"


def test_policy_change_after_interrupted_review_blocks_resumed_draft(
    sync: Synchronizer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hanger.model import Intent, Planner

    model = Script([decision()])
    monkeypatch.setattr(sync, "terms", lambda query: [])
    planner = Planner(sync, model)
    state = tmp_path.parent / (tmp_path.name + "-state")

    def interrupted() -> None:
        raise InterruptedError("review interrupted")

    model.after_review = interrupted
    with Runtime(sync.documents.root, state, planner) as runtime:
        runtime.submit_intent(
            issue_key(runtime._secret),
            Intent(
                kind="edit",
                query="note",
                instruction="edit old",
                paths=("a/note.md",),
                profile="wiki",
            ),
        )
        with pytest.raises(InterruptedError):
            runtime.run_next()
    model.after_review = lambda: None
    (sync.documents.root / "a/HANGER.md").write_text("Changed during review")
    with Runtime(sync.documents.root, state, planner) as resumed:
        result = resumed.run_next()
    assert result is not None and result["status"] == "conflict"
    assert len(model.drafts) == 1
    assert "old" in (sync.documents.root / "a/note.md").read_text()


def test_policy_change_during_context_assembly_does_not_replace_dependency(
    sync: Synchronizer, monkeypatch: pytest.MonkeyPatch
) -> None:
    rule = sync.documents.root / "HANGER.md"
    rule.write_text("Original root")
    read_bytes = Path.read_bytes
    changed = False

    def mutate_after_read(path: Path) -> bytes:
        nonlocal changed
        result = read_bytes(path)
        if path == rule and not changed:
            changed = True
            rule.write_text("Different root")
        return result

    monkeypatch.setattr(Path, "read_bytes", mutate_after_read)
    with pytest.raises(ConflictError, match="collecting policy"):
        sync.policy.context(["a/note.md"])


@pytest.mark.parametrize("action", ["keep", "defer"])
def test_noop_review_reads_all_sources_without_citations(
    sync: Synchronizer, action: str
) -> None:
    choice = {
        **decision(),
        "action": action,
        "sources": [],
        "evidence": [],
        "changes": [],
    }
    model = Script([choice])
    documents = {path: sync.read(path) for path in ("a/note.md", "b/note.md")}
    before = {path: (sync.documents.root / path).read_bytes() for path in documents}

    result = execute(sync, model, documents=documents)

    assert result["proposal"] == {}
    assert result["decision"]["action"] == action
    assert len(model.drafts) == len(model.reviews) == 1
    review = model.reviews[0]["input"]
    assert review["decision"]["action"] == action
    assert review["changes"] == []
    assert review["documents"] == documents
    assert before == {
        path: (sync.documents.root / path).read_bytes() for path in documents
    }


@pytest.mark.parametrize("action", ["keep", "defer"])
@pytest.mark.parametrize("recover", [True, False])
def test_noop_rejection_uses_bounded_runtime_redraft(
    sync: Synchronizer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    *,
    recover: bool,
) -> None:
    instruction = "Record that the improvement is an untested hypothesis."
    noop = {
        **decision(),
        "action": action,
        "sources": [],
        "evidence": [],
        "changes": [],
    }
    corrected = decision(new="old\nThe improvement is an untested hypothesis.")
    corrected["sources"].append("request")
    corrected["evidence"].append({"path": "request", "quote": instruction})
    model = Script([noop, corrected] if recover else [noop])
    rejected = {
        "approved": False,
        "issues": ["Requested untested hypothesis is missing"],
    }
    model.checks = (
        [rejected, {"approved": True, "issues": []}] if recover else [rejected]
    )
    monkeypatch.setattr(sync, "terms", lambda query: [])
    path = sync.documents.root / "a/note.md"
    before = path.read_bytes()
    with Runtime(
        sync.documents.root,
        tmp_path.parent / (tmp_path.name + "-state"),
        Planner(sync, model),
    ) as runtime:
        runtime.submit_intent(
            issue_key(runtime._secret),
            Intent(
                kind="edit",
                query="note",
                instruction=instruction,
                paths=("a/note.md",),
                profile="wiki",
            ),
        )
        result = runtime.run_next()
    assert result is not None
    assert result["status"] == ("completed" if recover else "failed")
    assert len(model.drafts) == len(model.reviews) == (2 if recover else 3)
    assert model.drafts[1]["input"]["feedback"] == rejected["issues"]
    if recover:
        assert "untested hypothesis" in path.read_text()
        assert "condition: version 2 only" in path.read_text()
    else:
        assert result["error_code"] == "review_rejected"
        assert path.read_bytes() == before


@pytest.mark.parametrize("mutation", ["source", "policy"])
def test_noop_review_conflicts_on_source_or_policy_change(
    sync: Synchronizer, mutation: str
) -> None:
    model = Script(
        [{**decision(), "action": "keep", "sources": [], "evidence": [], "changes": []}]
    )
    path = sync.documents.root / (
        "a/note.md" if mutation == "source" else "a/HANGER.md"
    )
    model.after_review = lambda: path.write_text("User changed this")
    with pytest.raises(ConflictError):
        execute(sync, model)
    assert len(model.reviews) == 1
    assert path.read_text() == "User changed this"
