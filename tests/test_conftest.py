from pathlib import Path

import pytest

from git_client import Git


def test_git_reads_no_ignore_or_attributes_file_of_the_users(
    repo: Git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # git's default core.excludesFile and core.attributesFile, which the tests' config turns off.
    (tmp_path / "xdg" / "git").mkdir(parents=True)
    (tmp_path / "xdg" / "git" / "ignore").write_text("*\n", encoding="utf-8")
    (tmp_path / "xdg" / "git" / "attributes").write_text("* diff=mine\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path))

    assert not repo.ok("check-ignore", "--quiet", "--no-index", "a")
    assert repo.run("check-attr", "diff", "--", "a") == "a: diff: unspecified"
