"""Collecting text and files into inbox/ unchanged."""

import hashlib
import time
from pathlib import Path

import pytest
from conftest import finish, offline_service

from markdown_notes_agent.documents import Documents
from markdown_notes_agent.errors import RequestError
from markdown_notes_agent.requests import issue_key
from markdown_notes_agent.rules import RULES_FILE
from markdown_notes_agent.vaults import VaultPolicy


def test_collect_respects_writable_and_rejects_bad_unicode(
    layout: tuple[Path, Path, VaultPolicy],
) -> None:
    root, state, policy = layout
    service, runtime = offline_service(root, state, policy)
    with runtime:
        with pytest.raises(RequestError) as caught:
            service.collect(issue_key(runtime._secret), "memo")
        assert caught.value.code == "write_denied"
        with pytest.raises(RequestError, match="invalid Unicode"):
            service.collect(issue_key(runtime._secret), "a\ud800b")
    assert [p.name for p in (root / "inbox").iterdir()] == [RULES_FILE]


# Collected input is named time-title-hash by one rule for every kind of input.
@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("# 결제 재시도 정책\n본문", "결제-재시도-정책"),
        ("메모 첫 줄입니다\n둘째 줄", "메모-첫-줄입니다"),
        ("https://www.example.com/a/b?c=1", "example.com-a-b-c=1"),
        ("사용자: 안녕\n어시스턴트: 반가워요", "사용자-안녕"),
        ("   \n\n", "untitled"),
        ("." * 3 + "숨김" + "가" * 60, "숨김" + "가" * 38),
    ],
)
def test_inbox_name_rule(text: str, title: str) -> None:
    from markdown_notes_agent.workflows import inbox_path

    issued = time.mktime((2026, 10, 16, 7, 31, 5, 0, 0, -1))
    path = inbox_path(text, issued)
    digest = hashlib.sha256(text.encode()).hexdigest()[:8]
    assert path == f"inbox/2026-10-16-073105-{title}-{digest}.md"
    assert not Path(path).name.startswith(".")


# A resend with the same key lands on the same file.
def test_collect_resend_is_duplicate(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    service, runtime = offline_service(root, tmp_path / "state")
    with runtime:
        key = issue_key(runtime._secret)
        first = service.collect(key, "# 링크 모음\nhttps://example.com")
        second = service.collect(key, "# 링크 모음\nhttps://example.com")
        assert first["status"] == "accepted" and second["status"] == "duplicate"
        assert first["path"] == second["path"]
        assert first["path"].startswith("inbox/") and "-링크-모음-" in first["path"]
        finish(runtime)
    assert (root / first["path"]).read_text() == "# 링크 모음\nhttps://example.com"


# Any file is collected byte for byte, named time-filename-hash.extension.
def test_collect_any_file(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    picture = tmp_path / "회의 사진 (1).PNG"
    data = bytes(range(256)) * 4000
    picture.write_bytes(data)
    service, runtime = offline_service(root, tmp_path / "state")
    with runtime:
        key = issue_key(runtime._secret)
        result = service.collect(key, file=str(picture))
        assert result["status"] == "completed"
        digest = hashlib.sha256(data).hexdigest()
        assert result["path"].endswith(f"-회의-사진-(1)-{digest[:8]}.png")
        assert (root / result["path"]).read_bytes() == data
        assert service.collect(key, file=str(picture))["status"] == "duplicate"
        assert runtime.describe(key)["status"] == "completed"
        other = tmp_path / "other.pdf"
        other.write_bytes(b"%PDF-1.7")
        with pytest.raises(RequestError, match="already used for a different request"):
            service.collect(key, file=str(other))
        with pytest.raises(RequestError, match="absolute file path"):
            service.collect(issue_key(runtime._secret), file="other.pdf")
        with pytest.raises(RequestError) as missing:
            service.collect(issue_key(runtime._secret), file=str(tmp_path / "none.bin"))
        assert missing.value.code == "not_found"
        with pytest.raises(RequestError, match="exactly one of text or file"):
            service.collect(issue_key(runtime._secret), text="a", file=str(other))
    leftovers = [p.name for p in (root / "inbox").iterdir() if p.name.startswith(".")]
    assert leftovers == []


# A file whose content changes while it is copied leaves nothing behind.
def test_collect_file_changed_while_copying(tmp_path: Path) -> None:
    from markdown_notes_agent.errors import ConflictError

    root = tmp_path / "vault"
    root.mkdir()
    source = tmp_path / "a.bin"
    source.write_bytes(b"first")
    documents = Documents(root)
    with pytest.raises(ConflictError):
        documents.store_file(
            "inbox/x-a-00000000.bin", source, hashlib.sha256(b"other").hexdigest()
        )
    assert not (root / "inbox" / "x-a-00000000.bin").exists()
    assert [p.name for p in (root / "inbox").iterdir()] == []


# Collecting a file into a folder that is not writable is refused.
def test_collect_file_respects_writable(
    layout: tuple[Path, Path, VaultPolicy],
) -> None:
    root, state, policy = layout
    source = root.parent / "a.txt"
    source.write_text("x")
    service, runtime = offline_service(root, state, policy)
    with runtime, pytest.raises(RequestError) as caught:
        service.collect(issue_key(runtime._secret), file=str(source))
    assert caught.value.code == "write_denied"
