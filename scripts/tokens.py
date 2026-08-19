#!/usr/bin/env python3
"""Token cost of the MCP server: the tool listing and a fixed walk of reading calls.

Run: uv run --with tiktoken python scripts/tokens.py

Needs a running daemon and a signed-in account; it reads and writes nothing. The
numbers come out of this account, so they are not comparable between machines —
what is comparable is the same machine before and after a change, which is what
this is for.

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

import aiohttp
import tiktoken

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from tgagent import config

ENC = tiktoken.get_encoding("cl100k_base")
CASES = [
    ("dialogs", {"limit": 30}),
    ("history", {"chat": "me", "limit": 40}),
    ("unread", {}),
    ("structure", {}),
    ("search", {"query": "http", "limit": 20}),
    ("events", {"limit": 20}),
    ("capabilities", {}),
    ("pending", {"limit": 10}),
    ("contacts", {"limit": 30}),
    ("folders", {}),
]


def tok(s: str) -> int:
    return len(ENC.encode(s))


async def daemon(method: str, params: dict):
    conn = aiohttp.UnixConnector(path=str(config.SOCKET))
    async with aiohttp.ClientSession(connector=conn) as s:
        async with s.post("http://localhost/call",
                          json={"method": method, "params": params}) as r:
            return await r.json()


async def listing() -> dict:
    from tgagent.mcp_server import mcp

    tools = await mcp.list_tools()
    desc = sum(tok(t.description or "") for t in tools)
    inp = sum(tok(json.dumps(t.input_schema, ensure_ascii=False)) for t in tools)
    out = sum(tok(json.dumps(t.output_schema, ensure_ascii=False))
              for t in tools if t.output_schema)
    return {"tools": len(tools), "descriptions": desc, "input_schemas": inp,
            "output_schemas": out, "total": desc + inp + out}


async def responses() -> dict:
    from tgagent.mcp_server import mcp

    rows, total = [], 0
    for method, params in CASES:
        raw = await daemon(method, params)
        if not raw.get("ok"):
            rows.append((method, None))
            continue
        name = "tg_" + method
        try:
            res = await mcp.call_tool(name, params)
        except Exception:
            res = None
        if res is not None:
            body = res.model_dump(exclude_none=True)
            n = tok(json.dumps(body, ensure_ascii=False))
        else:
            n = tok(json.dumps(raw["result"], ensure_ascii=False))
        rows.append((method, n))
        total += n
    return {"rows": rows, "total": total}


async def main() -> None:
    lst = await listing()
    print("== listing (paid on every request while the tools are loaded)")
    for k in ("tools", "descriptions", "input_schemas", "output_schemas", "total"):
        print(f"   {k:<16}{lst[k]:>8}")
    res = await responses()
    print("\n== one call each, as the client receives it")
    for name, n in res["rows"]:
        print(f"   {name:<16}{'-' if n is None else n:>8}")
    print(f"   {'TOTAL':<16}{res['total']:>8}")
    print(f"\n== listing + this walk: {lst['total'] + res['total']} tokens")


asyncio.run(main())
