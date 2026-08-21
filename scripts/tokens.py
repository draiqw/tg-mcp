#!/usr/bin/env python3
"""Token cost of the MCP server: the tool listing and a fixed walk of reading calls.

Run: uv run --with tiktoken python scripts/tokens.py

Needs a running daemon and a signed-in account; it reads and writes nothing.

Both figures are measured where the client actually pays them, which is further
out than it looks. The listing is counted as compact JSON, the way it travels —
counting it with `json.dumps` defaults adds a space after every colon and comma
and overstates the schemas by about a third. The calls are counted as the whole
CallToolResult, not as the daemon's answer: the doubled payload that cost this
server half of everything it sent lived between those two, in the MCP layer, and
a measuring stick that stops at the daemon cannot see it.

The two behave differently in time. The listing is stable: change nothing and it
prints the same figure tomorrow, so a before-and-after on it means something.
The calls are not — they measure this account's actual traffic, and an hour of
unread messages moves them more than most code changes would. Compare those only
within one run, which is why `brief` is measured here against the same data in
the same pass rather than against a figure from earlier.

cl100k is not Claude's tokenizer. It is a BPE that counts whitespace and JSON
punctuation the way any of them does, which is what a comparison of two payload
shapes needs; the absolute figure is an estimate, the delta is not.

tiktoken is deliberately not a dependency of the project: this is a measuring
stick, not something the agent runs.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import tiktoken

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ENC = tiktoken.get_encoding("cl100k_base")

# Tool name and arguments, as the agent would call them.
CASES = [
    ("tg_dialogs", {"limit": 30}),
    ("tg_history", {"chat": "me", "limit": 40}),
    ("tg_unread", {}),
    ("tg_structure", {}),
    ("tg_search", {"query": "http", "limit": 20}),
    ("tg_events", {"limit": 20}),
    ("tg_capabilities", {}),
    ("tg_pending", {"limit": 10}),
    ("tg_contacts", {"limit": 30}),
    ("tg_folders", {}),
]

# Tools that take `brief`. Measured twice so the two shapes meet the same data:
# the account moves between calls, and a saving read off two separate runs would
# be mostly noise.
BRIEF_CAPABLE = {"tg_history", "tg_unread", "tg_search", "tg_mentions"}


def tok(value: object) -> int:
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), default=str)
    return len(ENC.encode(text))


async def listing() -> dict:
    from tgagent.mcp_server import mcp

    tools = await mcp.list_tools()
    desc = sum(tok(t.description or "") for t in tools)
    inp = sum(tok(t.input_schema) for t in tools)
    out = sum(tok(t.output_schema) for t in tools if t.output_schema)
    return {"tools": len(tools), "descriptions": desc, "input_schemas": inp,
            "output_schemas": out, "total": desc + inp + out}


async def one_call(name: str, args: dict) -> dict | None:
    """The whole result object, split into the halves that can each be paid twice."""
    from tgagent.mcp_server import mcp

    try:
        res = await mcp.call_tool(name, args)
    except Exception as exc:  # a failed tool is a row in the table, not a crash
        return {"error": f"{type(exc).__name__}: {exc}"}
    payload = res.model_dump(exclude_none=True, mode="json")
    structured = payload.get("structuredContent") or payload.get("structured_content")
    return {"wire": tok(payload),
            "text": tok(payload.get("content")),
            "structured": tok(structured) if structured else 0}


async def responses() -> dict:
    rows, total = [], 0
    for name, args in CASES:
        full = await one_call(name, args)
        if full is None or "error" in full:
            rows.append((name, full, None))
            continue
        lean = await one_call(name, {**args, "brief": True}) \
            if name in BRIEF_CAPABLE else None
        rows.append((name, full, lean))
        total += full["wire"]
    return {"rows": rows, "total": total}


async def main() -> None:
    lst = await listing()
    print("== listing (paid on every request while the tools are loaded)")
    for k in ("tools", "descriptions", "input_schemas", "output_schemas", "total"):
        print(f"   {k:<16}{lst[k]:>8}")
    res = await responses()
    print("\n== one call each, on this account right now, as the client receives it")
    print(f"   {'':<16}{'wire':>9}{'of it dup':>11}{'brief':>9}")
    for name, full, lean in res["rows"]:
        if full is None or "error" in full:
            print(f"   {name:<16}{'-':>9}   {(full or {}).get('error', '')[:40]}")
            continue
        cut = ""
        if lean and "wire" in lean:
            cut = f"  {100 * (lean['wire'] - full['wire']) / full['wire']:+.0f}%"
        print(f"   {name:<16}{full['wire']:>9}{full['structured']:>11}"
              f"{(lean or {}).get('wire', '') or '':>9}{cut}")
    print(f"   {'TOTAL':<16}{res['total']:>9}")
    print(f"\n== listing + this walk: {lst['total'] + res['total']} tokens")
    dup = sum(r[1]["structured"] for r in res["rows"]
              if r[1] and "wire" in r[1])
    if dup:
        print(f"!! {dup} tokens of that are the same answer sent a second time — "
              "a tool is registered past the @tool wrapper")


asyncio.run(main())
