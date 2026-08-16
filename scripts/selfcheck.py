#!/usr/bin/env python3
"""Integrity check of the scaffolding: MCP <-> daemon <-> core <-> docs <-> agents.

Run: uv run python scripts/selfcheck.py
Sends nothing to Telegram and needs no running daemon: this is a static parse of
the sources. Returns a non-zero code if something has drifted apart.
"""

from __future__ import annotations

import ast
import filecmp
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENT_DIR = pathlib.Path.home() / ".claude" / "agents"

problems: list[str] = []
notes: list[str] = []


def check(ok: bool, message: str) -> None:
    (notes if ok else problems).append(("ok   " if ok else "BAD  ") + "  " + message)


def mcp_tools() -> dict[str, str]:
    """Tool name -> name of the RPC method it calls."""
    src = (ROOT / "tgagent" / "mcp_server.py").read_text()
    tree = ast.parse(src)
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or not node.name.startswith("tg_"):
            continue
        decorated = False
        for d in node.decorator_list:
            target = d.func if isinstance(d, ast.Call) else d
            if isinstance(target, ast.Attribute) and target.attr == "tool":
                decorated = True
        if not decorated:
            continue
        body = ast.get_source_segment(src, node) or ""
        m = re.search(r'\bcall\(\s*"(\w+)"', body)
        out[node.name] = m.group(1) if m else ""
    return out


def dispatch_methods() -> set[str]:
    src = (ROOT / "tgagent" / "daemon.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "dispatch_table":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Dict):
                    return {k.value for k in sub.keys if isinstance(k, ast.Constant)}
    return set()


def core_methods() -> set[str]:
    src = (ROOT / "tgagent" / "core.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "TelegramService":
            return {
                n.name
                for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return set()


def agent_tools(path: pathlib.Path) -> set[str]:
    head = path.read_text().split("---")[1]
    return set(re.findall(r"mcp__telegram__(tg_\w+)", head))


def main() -> int:
    tools = mcp_tools()
    disp = dispatch_methods()
    core = core_methods()
    docs = (ROOT / "docs" / "tools.md").read_text()

    print(f"MCP tools: {len(tools)} | daemon methods: {len(disp)}")

    no_rpc = sorted(t for t, m in tools.items() if not m)
    check(not no_rpc, f"an RPC call was found for every tool: {no_rpc or 'yes'}")

    orphan = sorted({m for m in tools.values() if m} - disp)
    check(not orphan, f"MCP methods missing from dispatch: {orphan or 'none'}")

    unused = sorted(disp - {m for m in tools.values()} - {"whoami"})
    check(not unused, f"daemon methods with no tool: {unused or 'none'}")

    # the daemon's write methods must exist in the core or be methods of the daemon itself
    # These live in the daemon itself: they need the event stream and the bot channel, not Telegram.
    daemon_own = {"status", "accounts", "events", "rules", "alert", "whoami", "wait", "ask"}
    ghost = sorted(
        m for m in disp - daemon_own if m not in core and m.rstrip("_") not in core
    )
    # some of the core methods are named differently (pin -> pin_dialog, unread -> unread_summary)
    aliases = {"pin": "pin_dialog", "unread": "unread_summary"}
    ghost = [m for m in ghost if aliases.get(m) not in core]
    check(not ghost, f"dispatch methods with no implementation in the core: {ghost or 'none'}")

    undoc = sorted(t for t in tools if t not in docs)
    check(not undoc, f"tools with no description in docs/tools.md: {undoc or 'none'}")

    phantom = sorted(re.findall(r"`(tg_\w+)`", docs))
    phantom = sorted({p for p in phantom if p not in tools})
    check(not phantom,
          f"docs/tools.md refers to something that does not exist: {phantom or 'none'}")

    for name, expected in (("telegram.md", None), ("telegram-watch.md", None)):
        src = ROOT / "agents" / name
        listed = agent_tools(src)
        bad = sorted(listed - set(tools))
        check(not bad, f"{name}: tools that do not exist {bad or 'none'}")
        dst = AGENT_DIR / name
        if dst.exists():
            check(filecmp.cmp(src, dst, shallow=False), f"{name} installed in ~/.claude/agents at the current version")
        else:
            check(False, f"{name} is not installed in ~/.claude/agents")

    full = agent_tools(ROOT / "agents" / "telegram.md")
    check(
        full == set(tools),
        f"the telegram agent sees every tool (missing from it: {sorted(set(tools) - full) or 'nothing'})",
    )

    watch = agent_tools(ROOT / "agents" / "telegram-watch.md")
    write_ish = {
        "tg_send_file", "tg_edit", "tg_delete", "tg_forward", "tg_react", "tg_click",
        "tg_poll", "tg_draft", "tg_schedule", "tg_send_location", "tg_block",
        "tg_contact_edit", "tg_create_group", "tg_invite", "tg_moderate",
        "tg_chat_edit", "tg_leave", "tg_folder_edit", "tg_pin", "tg_pin_message",
        "tg_send_sticker", "tg_topic_create", "tg_topic_edit", "tg_bot_edit",
        "tg_cache_clear", "tg_account_use", "tg_rules", "tg_export",
        "tg_sessions",
    }
    leak = sorted(watch & write_ish)
    check(not leak, f"the watcher was given no dangerous tools: {leak or 'yes'}")

    # the counters in the texts must not lie
    for doc in ("README.md", "docs/mcp.md", "docs/architecture.md", "docs/tools.md"):
        text = (ROOT / doc).read_text()
        wrong = [n for n in re.findall(r"(\d+) tools", text) if int(n) != len(tools)]
        check(not wrong, f"{doc}: tool counter {wrong or 'correct'}")

    for line in notes:
        print(line)
    if problems:
        print()
        for line in problems:
            print(line)
        return 1
    print("\neverything lines up")
    return 0


if __name__ == "__main__":
    sys.exit(main())
