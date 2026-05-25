from pathlib import Path

import pytest

from app.ingest import detect_language


@pytest.mark.parametrize(
    "name,expected",
    [
        ("main.py", "Python"),
        ("types.pyi", "Python"),
        ("app.ts", "TypeScript"),
        ("component.tsx", "TypeScript"),
        ("script.js", "JavaScript"),
        ("module.mjs", "JavaScript"),
        ("server.go", "Go"),
        ("lib.rs", "Rust"),
        ("Main.java", "Java"),
        ("Class.kt", "Kotlin"),
        ("README.md", "Markdown"),
        ("config.toml", "TOML"),
        ("data.yaml", "YAML"),
        ("data.yml", "YAML"),
        ("page.html", "HTML"),
        ("style.css", "CSS"),
        ("query.sql", "SQL"),
        ("Dockerfile", "Dockerfile"),
        ("prod.dockerfile", "Dockerfile"),
        ("Makefile", "Makefile"),
        ("unknown.xyz", None),
        ("LICENSE", None),
        ("no_extension", None),
    ],
)
def test_detect_language(name: str, expected: str | None) -> None:
    assert detect_language(Path(name)) == expected
