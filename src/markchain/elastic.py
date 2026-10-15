"""Install, run and supervise the Elasticsearch process Markchain depends on."""

from __future__ import annotations

__all__ = ["ES_VERSION", "INSTALL_STEPS", "ElasticManager"]

import hashlib
import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from filelock import FileLock

from markchain.config import Settings
from markchain.errors import MarkchainError, SearchUnavailableError
from markchain.progress import Progress, Step

logger = logging.getLogger(__name__)

ES_VERSION = "9.5.1"
INSTALL_STEPS = 5
_BASE = "https://artifacts.elastic.co/downloads"
_ARCHIVES = {
    ("darwin", "arm64"): (
        "darwin-aarch64",
        "a227418c40f768b112327fb663946006087feedadd2641be84aa6485cdde3917"
        "0cdbdb0089978d0c83804b203405b7e3d21191630a68651418ef6d4b25f23cb7",
    ),
    ("darwin", "x86_64"): (
        "darwin-x86_64",
        "111b9a4de0e82b56dc7adf1deffcb386b56d30ea6aa87a16f9560daf346c639e"
        "92e58a22d4d20dd47d7f22523d54a7bc1d7354caac27bb12961b5db1e31ad180",
    ),
    ("linux", "x86_64"): (
        "linux-x86_64",
        "c86ee0b98c1b6a2da46292522b0470dc21e075cc5140fe6038784fcbc55b74b5"
        "7c5d8ec8c4389451b684088891fce9162873ed0087578f384386d62c91f0cb2b",
    ),
    ("linux", "aarch64"): (
        "linux-aarch64",
        "3c8cc1e330e5ea1a76fe22dd617730bd352c921fe780a4ac1117afc2e13463f4"
        "79ffbdcf16f31cdeefe87513d85cd68303292e540d0c56a78cd883723fbc4923",
    ),
}
_NORI_SHA512 = (
    "ee8024c88114730c3198e13807266e4c076b9446eaf4dd3fff27646632988800"
    "e6267fa5777972d001ee2cde7cf0842852f6ba74846d2a925af512bab2caeda6"
)
_HEALTH_SECONDS = 10
_HEALTH_FAILURES = 3
_START_SECONDS = 180
_RESTART_BACKOFF = (5, 30, 120, 300)


def _archive() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "arm64": "arm64", "aarch64": "aarch64"}.get(
        machine, machine
    )
    if system == "darwin" and machine == "aarch64":
        machine = "arm64"
    if system == "linux" and machine == "arm64":
        machine = "aarch64"
    try:
        return _ARCHIVES[(system, machine)]
    except KeyError as error:
        raise MarkchainError(
            f"지원하지 않는 플랫폼입니다: {system}-{machine}", code="platform"
        ) from error


class ElasticManager:
    """Own one Elasticsearch process: install, start, watch and restart it."""

    def __init__(
        self,
        install_root: Path,
        state: Path,
        settings: Settings,
        *,
        downloader: Callable[..., None] | None = None,
    ) -> None:
        self.install_root = install_root
        self.home = install_root / f"elasticsearch-{ES_VERSION}"
        self.state = state / "elasticsearch"
        self.settings = settings.elasticsearch
        self.url = f"http://127.0.0.1:{self.settings.port}"
        # The cluster name ties a running server to this state directory.
        self.cluster = (
            "markchain-"
            + hashlib.sha256(str(state.resolve()).encode()).hexdigest()[:12]
        )
        self._download = downloader or _download
        self._status = "stopped"
        self._detail = ""
        self._since = time.monotonic()
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None

    # -- state -----------------------------------------------------------------

    def ready(self) -> bool:
        return self._status == "ready"

    def message(self) -> str:
        text = {
            "stopped": "검색 서버가 꺼져 있습니다",
            "installing": "검색 서버 설치 중",
            "starting": "검색 서버 시작 중",
            "ready": "검색 서버 정상",
            "recovering": "검색 서버 복구 중",
            "failed": "검색 서버 복구 실패, 다시 시도 중",
        }[self._status]
        if self._status in {"installing", "starting", "recovering"}:
            text += f" ({int(time.monotonic() - self._since)}초 경과)"
        return text + (f": {self._detail}" if self._detail else "")

    def require(self) -> None:
        if not self.ready():
            raise SearchUnavailableError(self.message())

    def _set(self, status: str, detail: str = "") -> None:
        if status != self._status:
            self._since = time.monotonic()
        if (status, detail) != (self._status, self._detail):
            log = logger.warning if status in {"recovering", "failed"} else logger.info
            log("search engine %s%s", status, f": {detail}" if detail else "")
        self._status, self._detail = status, detail

    # -- install ---------------------------------------------------------------

    # cost: time O(a), heap O(1), stack O(1), io O(a)
    # vars: a = archive bytes (about 700 MB once per version)
    # basis: estimate
    def installed(self) -> bool:
        return (self.home / ".markchain-installed").exists()

    def install(
        self,
        progress: Progress | None = None,
        *,
        reinstall: bool = False,
        title: str | None = "Markchain search engine setup",
        first: int = 1,
        total: int = INSTALL_STEPS,
    ) -> Path:
        """Download, verify and unpack the pinned server once per version.

        A reinstall builds the new copy beside the old one and swaps it in only
        after every step succeeded, so a failed download keeps the old server.
        """
        marker = self.home / ".markchain-installed"
        if marker.exists() and not reinstall:
            return self.home
        self.install_root.mkdir(parents=True, exist_ok=True)
        with FileLock(self.install_root / "install.lock"):
            if marker.exists() and not reinstall:
                return self.home
            suffix, checksum = _archive()
            if progress is not None and title:
                progress.title(
                    title,
                    f"Elasticsearch {ES_VERSION} ({suffix}) + Nori Korean analyzer",
                )
            work = self.install_root / f".install-{os.getpid()}"
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir()
            try:
                self._install_into(work, suffix, checksum, progress, first - 1, total)
            except (OSError, subprocess.CalledProcessError, tarfile.TarError) as error:
                raise MarkchainError(
                    f"Search engine install failed: {error}", code="search_install"
                ) from error
            finally:
                shutil.rmtree(work, ignore_errors=True)
        logger.info("Elasticsearch %s installed at %s", ES_VERSION, self.home)
        return self.home

    # cost: time O(a), heap O(1), stack O(1), io O(a)
    # vars: a = archive bytes
    # basis: estimate
    def _install_into(
        self,
        work: Path,
        suffix: str,
        checksum: str,
        progress: Progress | None,
        offset: int,
        steps: int,
    ) -> None:
        def step(
            index: int, label: str, status: str, unit: str = "bytes"
        ) -> Step | None:
            self._set("installing", status)
            if progress is None:
                return None
            return progress.step(offset + index, steps, label, unit)

        archive = work / "elasticsearch.tar.gz"
        nori = work / "analysis-nori.zip"
        downloads = (
            (
                "Download Elasticsearch",
                f"{_BASE}/elasticsearch/elasticsearch-{ES_VERSION}-{suffix}.tar.gz",
                archive,
                checksum,
            ),
            (
                "Download Korean analyzer",
                f"{_BASE}/elasticsearch-plugins/analysis-nori/"
                f"analysis-nori-{ES_VERSION}.zip",
                nori,
                _NORI_SHA512,
            ),
        )
        for index, (label, url, target, _) in enumerate(downloads, start=1):
            current = step(index, label, f"downloading {target.name}")
            self._download(url, target, current.update if current else None)
            if current:
                current.done()
        current = step(3, "Verify SHA-512 checksums", "verifying checksums")
        total = sum(target.stat().st_size for _, _, target, _ in downloads)
        hashed = 0
        for _, url, target, expected in downloads:
            digest = hashlib.sha512()
            with target.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    hashed += len(chunk)
                    if current:
                        current.update(hashed, total)
            if digest.hexdigest() != expected:
                if current:
                    current.fail(f"mismatch: {target.name}")
                raise OSError(f"checksum mismatch: {url}")
        if current:
            current.done("2 files verified")
        current = step(4, "Extract", "extracting", "files")
        with tarfile.open(archive) as bundle:
            members = bundle.getmembers()
            for number, member in enumerate(members, start=1):
                bundle.extract(member, work, filter="tar")
                if current:
                    current.update(number, len(members))
        archive.unlink()
        extracted = work / f"elasticsearch-{ES_VERSION}"
        if current:
            current.done(f"{len(members):,} files")
        current = step(5, "Install Korean analyzer", "installing Nori analyzer")
        subprocess.run(
            [
                str(extracted / "bin" / "elasticsearch-plugin"),
                "install",
                "--batch",
                nori.resolve().as_uri(),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        (extracted / ".markchain-installed").write_text(ES_VERSION)
        shutil.rmtree(self.home, ignore_errors=True)
        extracted.rename(self.home)
        if current:
            current.done("analysis-nori")

    # -- process ---------------------------------------------------------------

    def _probe(self) -> dict[str, object] | None:
        """Return the server identity on our port, or None when nothing answers."""
        try:
            with urllib.request.urlopen(self.url, timeout=5) as response:
                data = json.loads(response.read())
                return data if isinstance(data, dict) else {}
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def _healthy(self) -> bool:
        try:
            with urllib.request.urlopen(
                f"{self.url}/_cluster/health?wait_for_status=yellow&timeout=4s",
                timeout=5,
            ) as response:
                status = json.loads(response.read()).get("status")
                return status in {"yellow", "green"}
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def _launch(self) -> None:
        data = self.state / "data"
        logs = self.state / "logs"
        data.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "markchain.guard",
            str(os.getpid()),
            str(self.home / "bin" / "elasticsearch"),
            "-p",
            str(self.state / "es.pid"),
            f"-Ecluster.name={self.cluster}",
            "-Ediscovery.type=single-node",
            "-Expack.security.enabled=false",
            "-Expack.ml.enabled=false",
            "-Enetwork.host=127.0.0.1",
            f"-Ehttp.port={self.settings.port}",
            f"-Epath.data={data}",
            f"-Epath.logs={logs}",
            # Percentage watermarks block a nearly full laptop disk (90% of a
            # large drive is still plenty); use absolute free-space limits.
            "-Ecluster.routing.allocation.disk.watermark.low=3gb",
            "-Ecluster.routing.allocation.disk.watermark.high=2gb",
            "-Ecluster.routing.allocation.disk.watermark.flood_stage=1gb",
        ]
        heap = self.settings.heap_mb
        env = {**os.environ, "ES_JAVA_OPTS": f"-Xms{heap}m -Xmx{heap}m"}
        with (self.state / "console.log").open("ab") as console:
            self._process = subprocess.Popen(
                command, stdout=console, stderr=subprocess.STDOUT, env=env
            )

    def _kill_recorded(self) -> None:
        """Stop a server this state started earlier, found by its pid file."""
        pid_file = self.state / "es.pid"
        try:
            pid = int(pid_file.read_text().strip())
        except (OSError, ValueError):
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        for _ in range(60):
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return

    # cost: time O(s), heap O(1), stack O(1), io O(1)
    # vars: s = server start time
    # basis: estimate
    def _start_once(self) -> None:
        with self._lock:
            identity = self._probe()
            if identity is not None:
                if identity.get("cluster_name") != self.cluster:
                    raise MarkchainError(
                        f"{self.settings.port} 포트 사용 중 (Markchain 검색 서버 아님)",
                        code="port_in_use",
                    )
                if self._healthy():
                    logger.info("이전 검색 서버를 이어서 사용합니다")
                    self._set("ready")
                    return
                self._kill_recorded()
            self._launch()
        deadline = time.monotonic() + _START_SECONDS
        while time.monotonic() < deadline and not self._stop.is_set():
            if self._process is not None and self._process.poll() is not None:
                raise MarkchainError(
                    f"검색 서버가 시작 중 종료됨 (코드 {self._process.returncode})",
                    code="search_start",
                )
            if self._healthy():
                self._set("ready")
                return
            time.sleep(1)
        raise MarkchainError("검색 서버 시작 시간 초과", code="search_start")

    def _terminate(self) -> None:
        with self._lock:
            process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(60)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        self._kill_recorded()

    # -- supervision -----------------------------------------------------------

    def start(self) -> None:
        """Install if needed and supervise the server in a background thread."""
        if not self.settings.managed:
            self._set("ready")
            return
        self._stop.clear()
        self._monitor = threading.Thread(
            target=self._supervise, name="markchain-search", daemon=True
        )
        self._monitor.start()

    def _supervise(self) -> None:
        failures = 0
        restarts = 0
        while not self._stop.is_set():
            try:
                if self._status != "ready":
                    self.install()
                    self._set("starting" if restarts == 0 else "recovering")
                    self._start_once()
                    failures = 0
                    restarts = 0
                exited = self._process is not None and self._process.poll() is not None
                if exited or not self._healthy():
                    failures += 1
                    if exited or failures >= _HEALTH_FAILURES:
                        self._set("recovering", "응답 없음, 다시 시작")
                        self._terminate()
                        failures = 0
                        continue
                else:
                    failures = 0
                self._stop.wait(_HEALTH_SECONDS)
            except MarkchainError as error:
                delay = _RESTART_BACKOFF[min(restarts, len(_RESTART_BACKOFF) - 1)]
                restarts += 1
                self._set("failed", f"{error} ({delay}초 후 재시도)")
                self._terminate()
                self._stop.wait(delay)
            except Exception as error:
                logger.exception("검색 서버 감시 오류")
                self._set("failed", str(error))
                self._stop.wait(_RESTART_BACKOFF[0])

    def stop(self) -> None:
        self._stop.set()
        if self._monitor is not None:
            self._monitor.join(timeout=10)
        if self.settings.managed:
            self._terminate()
        self._set("stopped")


# cost: time O(a), heap O(1), stack O(1), io O(a)
# vars: a = downloaded bytes
# basis: estimate
def _download(
    url: str, target: Path, report: Callable[[int, int | None], None] | None = None
) -> None:
    with urllib.request.urlopen(url, timeout=60) as response, target.open("wb") as out:
        length = response.headers.get("Content-Length")
        total = int(length) if length and length.isdigit() else None
        done = 0
        while chunk := response.read(1024 * 1024):
            out.write(chunk)
            done += len(chunk)
            if report is not None:
                report(done, total)
