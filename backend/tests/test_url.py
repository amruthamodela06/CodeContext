import pytest

from app.ingest import parse_github_url


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/octocat/Hello-World", ("octocat", "Hello-World")),
        ("https://github.com/octocat/Hello-World.git", ("octocat", "Hello-World")),
        ("https://github.com/octocat/Hello-World/", ("octocat", "Hello-World")),
        ("https://github.com/octocat/Hello-World.git/", ("octocat", "Hello-World")),
        ("  https://github.com/a/b  ", ("a", "b")),
        ("https://github.com/tiangolo/fastapi", ("tiangolo", "fastapi")),
        ("https://github.com/owner/repo.with.dots", ("owner", "repo.with.dots")),
        ("https://github.com/owner/repo_under_score", ("owner", "repo_under_score")),
    ],
)
def test_parse_github_url_happy(url: str, expected: tuple[str, str]) -> None:
    assert parse_github_url(url) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "git@github.com:owner/repo.git",
        "ssh://github.com/owner/repo",
        "https://gitlab.com/owner/repo",
        "https://github.com/owner",
        "https://github.com/",
        "github.com/owner/repo",
        "http://github.com/owner/repo",
        "",
        "not a url at all",
    ],
)
def test_parse_github_url_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_github_url(bad)
