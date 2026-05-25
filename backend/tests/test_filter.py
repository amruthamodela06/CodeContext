from pathlib import Path

import pytest

from app.ingest import MAX_FILE_SIZE, walk_repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A fake repo tree with one of each filter category."""
    # Kept: source files
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')")
    (tmp_path / "README.md").write_text("# hello")

    # Skipped: dependency dir
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("x")

    # Skipped: .git internals
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main")

    # Skipped: lockfile by name
    (tmp_path / "package-lock.json").write_text("{}")

    # Skipped: known binary extension
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n")

    # Skipped: too large
    (tmp_path / "big.txt").write_bytes(b"x" * (MAX_FILE_SIZE + 1))

    return tmp_path


def test_walk_includes_source_files(repo: Path) -> None:
    paths = {f.path for f in walk_repo(repo)}
    assert "src/main.py" in paths
    assert "README.md" in paths


def test_walk_skips_dependency_dir(repo: Path) -> None:
    paths = {f.path for f in walk_repo(repo)}
    assert not any(p.startswith("node_modules/") for p in paths)


def test_walk_skips_git_dir(repo: Path) -> None:
    paths = {f.path for f in walk_repo(repo)}
    assert not any(p.startswith(".git/") for p in paths)


def test_walk_skips_lockfile(repo: Path) -> None:
    paths = {f.path for f in walk_repo(repo)}
    assert "package-lock.json" not in paths


def test_walk_skips_binary(repo: Path) -> None:
    paths = {f.path for f in walk_repo(repo)}
    assert "logo.png" not in paths


def test_walk_skips_oversized(repo: Path) -> None:
    paths = {f.path for f in walk_repo(repo)}
    assert "big.txt" not in paths


def test_walk_records_size_and_language(repo: Path) -> None:
    files = {f.path: f for f in walk_repo(repo)}
    main = files["src/main.py"]
    assert main.language == "Python"
    assert main.size_bytes == len("print('hi')")


def test_walk_paths_are_posix_style(repo: Path) -> None:
    """Walk must normalize to forward slashes even on Windows."""
    nested = repo / "src" / "sub"
    nested.mkdir()
    (nested / "deep.py").write_text("x")
    paths = {f.path for f in walk_repo(repo)}
    assert "src/sub/deep.py" in paths
