import io
from pathlib import Path

import pytest

from markchain.cli import main
from markchain.config import Settings, load_settings
from markchain.documents import Documents
from markchain.search import SearchIndex
from markchain.sync import Synchronizer
from markchain.wizard import SetupAbortedError, run_setup


def _vault(root: Path) -> Path:
    for folder in ("docs/a", "inbox", "career", ".obsidian"):
        (root / folder).mkdir(parents=True)
    (root / "docs/a/x.md").write_text("# x")
    (root / "inbox/y.md").write_text("# y")
    (root / "career/z.md").write_text("# z")
    (root / "top.md").write_text("# top")
    return root


# setup: a missing vault is asked three times, then setup stops unchanged.
def test_three_invalid_vaults_abort(tmp_path: Path) -> None:
    config = tmp_path / "state" / "config.toml"
    answers = io.StringIO("/nope\n\n/also/nope\n")
    out = io.StringIO()
    with pytest.raises(SetupAbortedError, match="3 attempts"):
        run_setup(config, Settings(), source=answers, out=out)
    assert out.getvalue().count("?    Vault root folder") == 3
    assert "2 attempts left" in out.getvalue()
    assert not config.exists()


# setup: folders are listed with note counts and toggled before saving.
def test_folder_selection_is_saved_and_applied(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "vault")
    config = tmp_path / "state" / "config.toml"
    answers = io.StringIO(f"/missing\n{vault}\n1\n\n\n")
    out = io.StringIO()
    settings = run_setup(config, Settings(), source=answers, out=out)
    text = out.getvalue()
    assert "career" in text and ".obsidian" not in text
    assert "markchain config --edit" in text
    assert settings.vault.root == str(vault.resolve())
    assert settings.vault.managed == ("docs", "inbox")
    assert load_settings(config) == settings
    sync = Synchronizer(
        Documents(vault), SearchIndex("http://127.0.0.1:9", "x"), settings
    )
    assert sorted(sync.paths()) == ["docs/a/x.md", "inbox/y.md"]
    assert not settings.writable("career/new.md")
    assert settings.writable("docs/a/new.md")


# setup: an existing choice is the default and Enter keeps it.
def test_rerun_keeps_previous_answers(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "vault")
    config = tmp_path / "state" / "config.toml"
    first = run_setup(
        config, Settings(), source=io.StringIO(f"{vault}\n3\n\n\n"), out=io.StringIO()
    )
    out = io.StringIO()
    second = run_setup(config, first, source=io.StringIO("\n\n\n"), out=out)
    assert second.vault.managed == first.vault.managed == ("career", "docs")
    assert f"({vault.resolve()})" in out.getvalue()


# cli: commands without a configured vault point to setup.
def test_cli_requires_setup_or_vault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--state", str(tmp_path / "s"), "status", "x"]) == 1
    assert "markchain setup" in capsys.readouterr().err
    assert main(["--state", str(tmp_path / "s"), "config"]) == 0
    assert "Run: markchain setup" in capsys.readouterr().out


# setup: declining the confirmation saves nothing.
def test_declined_confirmation_saves_nothing(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "vault")
    config = tmp_path / "state" / "config.toml"
    with pytest.raises(SetupAbortedError, match="cancelled"):
        run_setup(
            config, Settings(), source=io.StringIO(f"{vault}\n\nn\n"), out=io.StringIO()
        )
    assert not config.exists()


# setup: choosing every folder stores an empty list so new folders are included.
def test_all_folders_means_future_folders_too(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "vault")
    config = tmp_path / "state" / "config.toml"
    settings = run_setup(
        config, Settings(), source=io.StringIO(f"{vault}\n\n\n"), out=io.StringIO()
    )
    assert settings.vault.managed == ()
