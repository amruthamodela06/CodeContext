"""Unit tests for the citation parse/validate/render pipeline (ADR 0010).

Pure logic — no DB, no network. These pin the §9.4 citation contract: the parser
extracts shape-only tokens while skipping code, and the validator (not the
parser) is the ground truth for which ids are real.
"""

from types import SimpleNamespace

from app.citations import context as ctx_mod
from app.citations import parser, validator
from app.citations.context import CitationContext, CitedChunk
from app.citations.prompt import build_messages


def _chunk(display_id: str, chunk_id: int, path: str = "app/x.py") -> CitedChunk:
    return CitedChunk(
        display_id=display_id,
        chunk_id=chunk_id,
        file_path=path,
        start_line=10,
        end_line=20,
        language="Python",
        chunk_type="function",
        name="do_thing",
        content="def do_thing():\n    return 1\n",
        similarity=0.9,
    )


def _ctx(*display_ids: str) -> CitationContext:
    return CitationContext([_chunk(d, i + 1) for i, d in enumerate(display_ids)])


# --- Parser -------------------------------------------------------------


def test_parse_single_citation():
    cites, warnings = parser.parse("The function returns 1 [chunk:c1].")
    assert [c.display_id for c in cites] == ["c1"]
    assert warnings == []


def test_parse_multiple_citations_on_one_claim():
    cites, _ = parser.parse("It does X [chunk:c1][chunk:c2].")
    assert [c.display_id for c in cites] == ["c1", "c2"]


def test_parse_punctuation_adjacent_and_offsets():
    answer = "Foo [chunk:c1]. Bar [chunk:c2]!"
    cites, _ = parser.parse(answer)
    assert [c.display_id for c in cites] == ["c1", "c2"]
    # offsets index into the original string
    for c in cites:
        assert answer[c.start : c.end] == f"[chunk:{c.display_id}]"


def test_parse_none_sentinel():
    cites, _ = parser.parse("General knowledge [chunk:none].")
    assert [c.display_id for c in cites] == ["none"]


def test_parse_rejects_malformed_ids():
    # empty, whitespace-padded, unterminated
    cites, _ = parser.parse("a [chunk:] b [chunk: c1 ] c [chunk:c1 d")
    assert cites == []


def test_parse_skips_fenced_code_block():
    answer = "Real [chunk:c1].\n```python\n# not a cite [chunk:c2]\n```\nAfter [chunk:c3]."
    cites, warnings = parser.parse(answer)
    assert [c.display_id for c in cites] == ["c1", "c3"]
    assert warnings == []


def test_parse_skips_tilde_fence_and_variable_length():
    answer = "A [chunk:c1].\n~~~\ninside [chunk:c2]\n~~~\nB [chunk:c3]."
    cites, _ = parser.parse(answer)
    assert [c.display_id for c in cites] == ["c1", "c3"]


def test_parse_longer_fence_not_closed_by_shorter():
    # opened with 4 backticks; a 3-backtick line does NOT close it
    answer = "A [chunk:c1].\n````\n```\nstill inside [chunk:c2]\n````\nB [chunk:c3]."
    cites, _ = parser.parse(answer)
    assert [c.display_id for c in cites] == ["c1", "c3"]


def test_parse_skips_inline_code_span():
    cites, _ = parser.parse("Use `arr[chunk:c1]` here, but cite this [chunk:c2].")
    assert [c.display_id for c in cites] == ["c2"]


def test_parse_inline_code_variable_backticks():
    cites, _ = parser.parse("Code ``a `b` [chunk:c1] c`` then real [chunk:c2].")
    assert [c.display_id for c in cites] == ["c2"]


def test_parse_unterminated_fence_warns_and_suppresses():
    answer = "Before [chunk:c1].\n```\ninside [chunk:c2]\nstill inside [chunk:c3]"
    cites, warnings = parser.parse(answer)
    assert [c.display_id for c in cites] == ["c1"]
    assert "fence_unterminated" in warnings


# --- Validator ----------------------------------------------------------


def test_validate_classifies_valid_none_invalid():
    cites, _ = parser.parse("X [chunk:c1]. Y [chunk:c99]. Z [chunk:none].")
    resolved = validator.resolve(cites, _ctx("c1", "c2"), owner="o", name="r", ref="abc123")
    by_id = {r.display_id: r for r in resolved}
    assert by_id["c1"].status == "valid"
    assert by_id["c99"].status == "invalid"
    assert by_id["none"].status == "none"


def test_validate_resolves_permalink_with_sha():
    cites, _ = parser.parse("X [chunk:c1].")
    resolved = validator.resolve(
        cites, _ctx("c1"), owner="tiangolo", name="asyncer", ref="deadbeef"
    )
    r = resolved[0]
    assert r.status == "valid"
    assert r.chunk_id == 1
    assert r.permalink == "https://github.com/tiangolo/asyncer/blob/deadbeef/app/x.py#L10-L20"


def test_validate_dedupes_by_display_id():
    cites, _ = parser.parse("A [chunk:c1]. B [chunk:c1]. C [chunk:c1].")
    resolved = validator.resolve(cites, _ctx("c1"), owner="o", name="r", ref="sha")
    assert len(resolved) == 1


def test_validate_invalid_has_no_resolution():
    cites, _ = parser.parse("X [chunk:c5].")
    resolved = validator.resolve(cites, _ctx("c1"), owner="o", name="r", ref="sha")
    assert resolved[0].status == "invalid"
    assert resolved[0].permalink is None
    assert resolved[0].chunk_id is None


# --- Context + prompt ---------------------------------------------------


def test_context_from_results_assigns_sequential_ids():
    rows = [
        SimpleNamespace(
            chunk_id=11,
            file_path="a.py",
            start_line=1,
            end_line=5,
            language="Python",
            chunk_type="function",
            name="f",
            content="...",
            similarity=0.8,
        ),
        SimpleNamespace(
            chunk_id=22,
            file_path="b.py",
            start_line=2,
            end_line=9,
            language="Python",
            chunk_type="class",
            name="C",
            content="...",
            similarity=0.7,
        ),
    ]
    context = CitationContext.from_results(rows)
    assert [c.display_id for c in context.chunks] == ["c1", "c2"]
    assert context.by_token[("chunk", "c1")].chunk_id == 11
    assert context.by_token[("chunk", "c2")].chunk_id == 22


def test_render_excerpts_includes_ids_paths_and_fences():
    context = _ctx("c1", "c2")
    rendered = context.render_excerpts()
    assert "[c1] code: app/x.py:10-20 (function do_thing)" in rendered
    assert "[c2] code: app/x.py:10-20 (function do_thing)" in rendered
    assert "```python" in rendered


def test_build_messages_has_system_and_user_with_excerpts():
    context = _ctx("c1")
    messages = build_messages("tiangolo", "asyncer", "How do I run async?", context)
    assert [m.role for m in messages] == ["system", "user"]
    assert "tiangolo/asyncer" in messages[0].content
    assert "How do I run async?" in messages[1].content
    assert "[c1]" in messages[1].content
    # The system prompt must teach the [chunk:c1] format and the none sentinel.
    assert "[chunk:c1]" in messages[0].content
    assert "[chunk:none]" in messages[0].content


def test_module_exports_resolve_and_parse():
    # guard against the package __init__ drifting from the modules
    assert hasattr(ctx_mod, "CitationContext")


# --- Slice 5f: typed tokens (chunk / commit / pr / issue) ------------------


def test_parser_extracts_all_four_token_types():
    cites, _ = parser.parse(
        "Per [chunk:c1] the audit [issue:i4] led to a fix [pr:p3] in [commit:m2]."
    )
    pairs = [(c.entity_type, c.display_id) for c in cites]
    assert pairs == [("chunk", "c1"), ("issue", "i4"), ("pr", "p3"), ("commit", "m2")]


def test_parser_rejects_unknown_entity_types():
    # 'file' isn't in the allowed type set; should not parse.
    cites, _ = parser.parse("Bad [file:f1] but ok [chunk:c1].")
    assert [(c.entity_type, c.display_id) for c in cites] == [("chunk", "c1")]


def test_validator_resolves_typed_entities_to_per_type_permalinks():
    from datetime import UTC, datetime

    from app.citations.context import CitedCommit, CitedIssue, CitedPR

    ctx = CitationContext(
        chunks=[_chunk("c1", chunk_id=11, path="auth/login.py")],
        commits=[
            CitedCommit(
                display_id="m1",
                commit_id=22,
                sha="deadbeefcafe" + "0" * 28,
                author_name="Jane",
                authored_at=datetime.now(UTC),
                message="Switch to bcrypt",
            )
        ],
        prs=[
            CitedPR(
                display_id="p1",
                pr_id=33,
                number=234,
                title="Switch auth to bcrypt",
                body=None,
                state="merged",
                merged_at=datetime.now(UTC),
            )
        ],
        issues=[
            CitedIssue(
                display_id="i1",
                issue_id=44,
                number=189,
                title="Auth uses MD5",
                body=None,
                state="closed",
                closed_at=datetime.now(UTC),
            )
        ],
    )
    cites, _ = parser.parse("X [chunk:c1] Y [commit:m1] Z [pr:p1] W [issue:i1] V [pr:p99].")
    resolved = validator.resolve(cites, ctx, owner="o", name="r", ref="abc")
    by_key = {(r.entity_type, r.display_id): r for r in resolved}

    assert by_key[("chunk", "c1")].status == "valid"
    assert by_key[("chunk", "c1")].permalink == (
        "https://github.com/o/r/blob/abc/auth/login.py#L10-L20"
    )
    assert by_key[("commit", "m1")].status == "valid"
    assert by_key[("commit", "m1")].permalink == (
        "https://github.com/o/r/commit/deadbeefcafe" + "0" * 28
    )
    assert by_key[("pr", "p1")].status == "valid"
    assert by_key[("pr", "p1")].permalink == "https://github.com/o/r/pull/234"
    assert by_key[("issue", "i1")].status == "valid"
    assert by_key[("issue", "i1")].permalink == "https://github.com/o/r/issues/189"
    assert by_key[("pr", "p99")].status == "invalid"


def test_historical_why_prompt_includes_typed_token_examples():
    from app.citations import build_historical_why_messages

    ctx = _ctx("c1")
    messages = build_historical_why_messages("o", "r", "why?", ctx)
    sys_content = messages[0].content
    # Prompt must teach all four typed tokens AND the chain-tracing pattern.
    for tok in ("[chunk:cN]", "[commit:mN]", "[pr:pN]", "[issue:iN]"):
        assert tok in sys_content
    assert "chain" in sys_content.lower()


def test_system_prompt_includes_one_shot_example():
    # ADR 0010 iteration log: a worked example of the answer format. Small
    # instruction-tuned LLMs follow concrete patterns far better than abstract
    # rules — this assertion guards against a future prompt edit silently
    # dropping it.
    messages = build_messages("o", "r", "q", _ctx("c1"))
    sys = messages[0].content
    assert "Example" in sys
    # the example pairs two valid cites with a [chunk:none] sentinel
    assert "[chunk:c1]" in sys and "[chunk:c2]" in sys and "[chunk:none]" in sys
