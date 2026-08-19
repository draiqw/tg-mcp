"""What the MCP server puts on the wire, measured rather than assumed.

Everything here is about size. The agent reading these answers pays for every
token of them, and the three things checked below were each found by measuring:
an answer that travelled twice, an answer indented for a reader that does not
exist, and an argument schema written for a form generator. None of them changed
what the tools do, and all of them were invisible until somebody counted.

These are regression tests in the literal sense: each of these came back for
free the moment the code around it was touched, because nothing failed when it
did.
"""

from __future__ import annotations

import json

import pytest

from tgagent import mcp_server


@pytest.fixture
def tools():
    return mcp_server.mcp._tool_manager._tools


def test_no_tool_advertises_an_output_schema(tools):
    """An output schema here means the answer is sent a second time.

    Every tool returns an opaque JSON string. FastMCP reads the `-> str`
    annotation, decides the result is structured, and sends it both as text and
    as `{"result": ...}` — the same bytes twice, plus a schema per tool in the
    listing. Measured on this account it was an exact doubling.
    """
    guilty = sorted(name for name, t in tools.items() if t.output_schema)
    assert not guilty, f"these tools send their answer twice: {guilty}"


def test_every_tool_goes_through_the_local_decorator(tools):
    """`@tool` is what turns the second copy off; `@mcp.tool` does not.

    The two spellings look identical at the call site, which is exactly why this
    is checked here and not left to review.
    """
    src = (mcp_server.config.ROOT / "tgagent" / "mcp_server.py").read_text()
    assert "@mcp.tool(" not in src, "a tool is registered past the wrapper"
    assert src.count("@tool(") == len(tools)


def test_answers_carry_no_indentation():
    """Pretty-printing is for a person, and no person reads this.

    A list of forty messages spends about a quarter of itself on leading spaces
    and newlines. `tg call` and the CLI format their own copy for a human.
    """
    out = mcp_server.j({"a": [{"x": 1}, {"y": "два"}]})
    assert "\n" not in out and ", " not in out
    assert out == '{"a":[{"x":1},{"y":"два"}]}'
    assert json.loads(out) == {"a": [{"x": 1}, {"y": "два"}]}


def test_argument_schemas_carry_no_titles(tools):
    """"Chat" next to a property called `chat`, on every argument of every tool."""
    def titles(node) -> int:
        if isinstance(node, dict):
            return ("title" in node) + sum(titles(v) for v in node.values())
        if isinstance(node, list):
            return sum(titles(v) for v in node)
        return 0

    assert sum(titles(t.parameters) for t in tools.values()) == 0


def test_an_optional_argument_is_one_type_not_a_union(tools):
    """`anyOf: [X, null]` is how Python spells optional, at four times the width.

    What the reader needs is the type and whether it may be left out; the second
    half is in `required`, where being absent already says so.

    The exception is an argument where null is a value rather than an absence —
    `tg_dialogs(archived=None)` means "both lists", which is not the same request
    as leaving it out. Those keep the union, because there the null branch is the
    documentation.
    """
    unions = {
        f"{name}.{arg}"
        for name, t in tools.items()
        for arg, spec in t.parameters.get("properties", {}).items()
        if any(b.get("type") == "null" for b in spec.get("anyOf", []))
    }
    assert unions == {"tg_dialogs.archived"}, f"unions that should be flat: {unions}"


def test_an_explicit_null_still_validates():
    """Flattening the schema must not narrow what the tool accepts.

    Validation runs against the function's own model, not against the advertised
    schema, and a client that sends `null` for an argument it does not want is
    doing nothing wrong.
    """
    tool = mcp_server.mcp._tool_manager._tools["tg_history"]
    args = tool.fn_metadata.arg_model.model_validate(
        {"chat": "me", "limit": 2, "before_id": None, "search": None}
    )
    assert args.model_dump()["before_id"] is None


def test_compact_schema_leaves_alone_what_it_does_not_understand():
    """A shape the rule does not match is not half-rewritten."""
    exotic = {"anyOf": [{"type": "string"}, {"type": "integer"}], "default": "x"}
    assert mcp_server.compact_schema(exotic) == exotic
    three = {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}],
             "default": None}
    assert mcp_server.compact_schema(three)["anyOf"] == three["anyOf"]
