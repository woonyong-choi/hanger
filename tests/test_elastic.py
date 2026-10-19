import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from markdown_notes_agent.config import Settings
from markdown_notes_agent.elastic import ES_VERSION, ElasticManager
from markdown_notes_agent.errors import MarkdownNotesAgentError

PORT = 19230


@pytest.fixture
def install_root(tmp_path: Path) -> Path:
    """Point the manager at an already extracted Elasticsearch with Nori."""
    home = os.environ.get("MARKDOWN_NOTES_AGENT_TEST_ES_HOME")
    if not home:
        pytest.skip(
            "requires MARKDOWN_NOTES_AGENT_TEST_ES_HOME with an extracted server"
        )
    root = tmp_path / "install"
    root.mkdir()
    (root / f"elasticsearch-{ES_VERSION}").symlink_to(home)
    return root


def _settings() -> Settings:
    return Settings.model_validate({"elasticsearch": {"port": PORT}})


def _wait(predicate: object, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        time.sleep(0.5)
    raise AssertionError("condition not reached")


def _es_pid(state: Path) -> int:
    return int((state / "elasticsearch" / "es.pid").read_text())


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def manager(install_root: Path, tmp_path: Path) -> Iterator[ElasticManager]:
    engine = ElasticManager(install_root, tmp_path / "state", _settings())
    try:
        yield engine
    finally:
        engine.stop()


# a killed search process is detected and restarted by the supervisor.
def test_supervisor_restarts_killed_server(
    manager: ElasticManager, tmp_path: Path
) -> None:
    manager.start()
    _wait(manager.ready, 180)
    first = _es_pid(tmp_path / "state")
    os.kill(first, signal.SIGKILL)
    _wait(lambda: not manager.ready(), 30)
    assert "Restarting" in manager.message() or "Starting" in manager.message()
    _wait(manager.ready, 180)
    assert _es_pid(tmp_path / "state") != first


# a server left by a previous manager is reused instead of duplicated.
def test_new_manager_adopts_healthy_server(install_root: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    first = ElasticManager(install_root, state, _settings())
    first.start()
    try:
        _wait(first.ready, 180)
        pid = _es_pid(state)
        second = ElasticManager(install_root, state, _settings())
        second.start()
        _wait(second.ready, 30)
        assert _es_pid(state) == pid
        second.stop()
        _wait(lambda: not _alive(pid), 60)
    finally:
        first.stop()


# when the owning process dies without cleanup the server is stopped too.
def test_server_stops_when_owner_is_killed(install_root: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    script = (
        "import sys,time\n"
        "from pathlib import Path\n"
        "from markdown_notes_agent.config import Settings\n"
        "from markdown_notes_agent.elastic import ElasticManager\n"
        f"m=ElasticManager(Path({str(install_root)!r}),Path({str(state)!r}),"
        f"Settings.model_validate({{'elasticsearch':{{'port':{PORT}}}}}))\n"
        "m.start()\n"
        "while not m.ready(): time.sleep(0.5)\n"
        "print('ready', flush=True)\n"
        "time.sleep(600)\n"
    )
    owner = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE)
    try:
        assert owner.stdout is not None
        assert owner.stdout.readline().strip() == b"ready"
        pid = _es_pid(state)
        owner.kill()
        owner.wait()
        _wait(lambda: not _alive(pid), 90)
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait()
        if owner.stdout is not None:
            owner.stdout.close()


class _Foreign(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        body = json.dumps({"cluster_name": "someone-else"}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


# a port held by another program is reported, never taken over.
def test_foreign_server_on_port_is_reported(manager: ElasticManager) -> None:
    server = http.server.HTTPServer(("127.0.0.1", PORT), _Foreign)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        manager.start()
        _wait(lambda: "Another program" in manager.message(), 60)
        assert not manager.ready()
    finally:
        manager.stop()
        server.shutdown()
        server.server_close()


# a corrupted download is rejected before anything is installed.
def test_install_rejects_checksum_mismatch(tmp_path: Path) -> None:
    def fake(url: str, target: Path, report: object = None) -> None:
        target.write_bytes(b"not elasticsearch")

    engine = ElasticManager(
        tmp_path / "install", tmp_path / "state", Settings(), downloader=fake
    )
    with pytest.raises(MarkdownNotesAgentError, match="checksum mismatch"):
        engine.install()
    assert not (tmp_path / "install" / f"elasticsearch-{ES_VERSION}").exists()


# redirected setup output stays short: one line per 10 percent.
def test_progress_without_terminal_prints_deciles() -> None:
    import io

    from markdown_notes_agent.progress import Progress

    stream = io.StringIO()
    progress = Progress(stream)
    step = progress.step(1, 2, "Download Elasticsearch")
    for done in range(0, 1001, 5):
        step.update(done, 1000)
    step.done()
    lines = stream.getvalue().splitlines()
    assert len(lines) == 12
    assert lines[-1].startswith("  done [1/2] Download Elasticsearch")
    assert "1000 B" in lines[-1]
