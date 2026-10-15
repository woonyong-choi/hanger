import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from markchain import proxy
from markchain.daemon import DaemonInfo, socket_path
from markchain.proxy import Launcher, Relay, call_tool
from markchain.search import SearchIndex


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[Path, Path, Launcher]]:
    url = os.environ.get("MARKCHAIN_TEST_SEARCH_URL")
    if not url:
        pytest.skip("requires explicit Elasticsearch test endpoint")
    root = tmp_path / "vault"
    (root / "docs").mkdir(parents=True)
    (root / "docs/a.md").write_text("# 결제\n멱등성 키")
    state = tmp_path / "s"
    state.mkdir()
    (state / "config.toml").write_text("[service]\nidle_minutes = 0.05\n")
    command = [
        sys.executable,
        "-m",
        "markchain.cli",
        "--vault",
        str(root),
        "--state",
        str(state),
        "--search-url",
        url,
        "daemon",
    ]
    launcher = Launcher(state, command)
    try:
        yield root, state, launcher
    finally:
        launcher.stop()
        index = SearchIndex(url, str(root.resolve()))
        index.client.options(ignore_status=404).indices.delete(index=index.name)
        index.close()


def _pid(state: Path) -> int:
    info = DaemonInfo.read(state)
    assert info is not None
    return info.pid


def _alive(pid: int) -> bool:
    proxy._reap()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_dead(pid: int, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(pid)


# #3: clients started concurrently share exactly one daemon.
def test_concurrent_clients_share_one_daemon(env: tuple[Path, Path, Launcher]) -> None:
    _, state, launcher = env
    with ThreadPoolExecutor(max_workers=6) as pool:
        keys = list(pool.map(lambda _: call_tool(launcher, "issue_key", {}), range(6)))
    assert len(set(keys)) == 6
    daemons = subprocess.run(
        ["pgrep", "-f", "--", f"--state {state}"], capture_output=True, text=True
    ).stdout.split()
    assert len(daemons) == 1
    assert int(daemons[0]) == _pid(state)


# #3: a killed daemon is replaced on the next call and keeps queued work.
def test_killed_daemon_restarts_and_finishes_work(
    env: tuple[Path, Path, Launcher],
) -> None:
    root, state, launcher = env
    key = call_tool(launcher, "issue_key", {})
    first = _pid(state)
    os.kill(first, signal.SIGKILL)
    _wait_dead(first)
    assert socket_path(state).exists()  # stale socket left behind
    collected = call_tool(launcher, "collect", {"key": key, "text": "메모"})
    assert collected["status"] == "accepted"
    assert _pid(state) != first
    deadline = time.monotonic() + 30
    while call_tool(launcher, "job_status", {"key": key})["status"] != "completed":
        assert time.monotonic() < deadline
        time.sleep(0.3)
    assert (root / collected["path"]).read_text() == "메모"
    again = call_tool(launcher, "collect", {"key": key, "text": "메모"})
    assert again["status"] == "duplicate"


# #3: an idle daemon exits by itself and the next request starts a new one.
def test_idle_daemon_exits_and_restarts(env: tuple[Path, Path, Launcher]) -> None:
    _, state, launcher = env
    call_tool(launcher, "issue_key", {})
    first = _pid(state)
    _wait_dead(first, 30)
    assert not socket_path(state).exists()
    call_tool(launcher, "issue_key", {})
    assert _pid(state) != first


# #3: a daemon from another version is replaced instead of reused.
def test_version_mismatch_replaces_daemon(env: tuple[Path, Path, Launcher]) -> None:
    _, state, launcher = env
    call_tool(launcher, "issue_key", {})
    first = _pid(state)
    DaemonInfo(first, "0.0.0-old").write(state)
    call_tool(launcher, "issue_key", {})
    _wait_dead(first)
    assert _pid(state) != first


# #3: the stdio relay replays the handshake and pending request after a crash,
# and replaces a daemon that stops answering.
def test_relay_survives_crash_and_hang(
    env: tuple[Path, Path, Launcher], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, state, launcher = env
    monkeypatch.setattr(proxy, "_PING_SECONDS", 1)
    monkeypatch.setattr(proxy, "_PING_TIMEOUT", 3)
    client_in, relay_in = os.pipe()
    relay_out, client_out = os.pipe()
    source = os.fdopen(client_in, "rb")
    sink = os.fdopen(client_out, "wb")
    relay = Relay(launcher, source, sink)
    thread = threading.Thread(target=relay.run, daemon=True)
    thread.start()
    writer = os.fdopen(relay_in, "wb")
    reader = os.fdopen(relay_out, "rb")

    def send(message: dict[str, object]) -> None:
        writer.write(json.dumps(message).encode() + b"\n")
        writer.flush()

    def receive(identifier: int) -> dict[str, object]:
        while True:
            reply = json.loads(reader.readline())
            if reply.get("id") == identifier:
                return reply

    send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        }
    )
    receive(1)
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    call = {"name": "issue_key", "arguments": {}}
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": call})
    assert "result" in receive(2)

    crashed = _pid(state)
    os.kill(crashed, signal.SIGKILL)
    _wait_dead(crashed)
    send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": call})
    assert "result" in receive(3)
    assert _pid(state) != crashed

    hung = _pid(state)
    os.kill(hung, signal.SIGSTOP)
    try:
        _wait_dead(hung, 30)
    finally:
        if _alive(hung):
            os.kill(hung, signal.SIGKILL)
    send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": call})
    assert "result" in receive(4)
    writer.close()
    thread.join(timeout=10)
    for stream in (source, sink, reader):
        stream.close()


# #3: a state path too long for a Unix socket falls back to a short temp path.
def test_long_state_path_uses_short_socket(tmp_path: Path) -> None:
    state = tmp_path / ("x" * 120)
    path = socket_path(state)
    assert len(str(path).encode()) < 100
    assert path == socket_path(state)
    assert socket_path(tmp_path / ("y" * 120)) != path
