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


def module_constant(module: str, name: str) -> set[str]:
    """String elements of a top-level constant (`FOO = {...}` / `(...)`).

    Through ast rather than an import: the script must remain a static parse and
    not drag Telethon and aiohttp along for the sake of one list.
    """
    tree = ast.parse((ROOT / "tgagent" / module).read_text())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        value = node.value
        if isinstance(value, ast.Dict):
            return {k.value for k in value.keys if isinstance(k, ast.Constant)}
        if isinstance(value, (ast.Set, ast.Tuple, ast.List)):
            return {e.value for e in value.elts if isinstance(e, ast.Constant)}
    return set()


def arch_file_rows() -> dict[str, int]:
    """The file table from architecture.md: path -> the claimed number of lines."""
    text = (ROOT / "docs" / "architecture.md").read_text()
    rows = re.findall(r"\|\s*\**`(tgagent/[\w.]+)`\**\s*\|\s*(\d+)\s*\|", text)
    return {path: int(n) for path, n in rows}


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
    daemon_own = {
        "status", "accounts", "events", "rules", "alert", "whoami", "wait", "ask",
        # reminders need the tick and the bot channel, the action log needs the daemon's file
        "remind", "actions",
    }
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
        "tg_sessions", "tg_remind",
        # tg_index does not touch the account, but it lays the correspondence out on
        # disk and knows how to wipe it from there — the watcher is not entitled to such a tool
        "tg_index",
    }
    leak = sorted(watch & write_ish)
    check(not leak, f"the watcher was given no dangerous tools: {leak or 'yes'}")

    # the counters in the texts must not lie
    for doc in ("README.md", "docs/mcp.md", "docs/architecture.md", "docs/tools.md"):
        text = (ROOT / doc).read_text()
        wrong = [n for n in re.findall(r"(\d+) tools", text) if int(n) != len(tools)]
        check(not wrong, f"{doc}: tool counter {wrong or 'correct'}")

    watch_doc = (ROOT / "docs" / "mcp.md").read_text()
    claimed = re.search(r"\|\s*`telegram-watch`\s*\|[^|]*\|\s*(\d+)", watch_doc)
    check(
        bool(claimed) and int(claimed.group(1)) == len(watch),
        f"docs/mcp.md: the watcher's tool counter "
        f"({claimed.group(1) if claimed else '?'} against {len(watch)})",
    )

    # the file table in architecture.md: both the contents and the number of lines.
    # Drifts apart silently with any edit to the code, and reads as fact.
    listed = arch_file_rows()
    modules = {
        f"tgagent/{p.name}": len(p.read_text().splitlines())
        for p in sorted((ROOT / "tgagent").glob("*.py"))
        if p.name != "__init__.py"
    }
    missing = sorted(set(modules) - set(listed))
    check(not missing, f"architecture.md: modules with no row in the table: {missing or 'none'}")
    stale = sorted(f"{f} {listed[f]}≠{modules[f]}" for f in listed if f in modules and listed[f] != modules[f])
    check(not stale, f"architecture.md: the line counts have drifted: {stale or 'none'}")


    # the rules from config.DEFAULT_RULES must be described, otherwise a new key
    # exists only in the code and nobody will be able to configure it
    conf_doc = (ROOT / "docs" / "configuration.md").read_text()
    rules_keys = module_constant("config.py", "DEFAULT_RULES")
    undoc_rules = sorted(k for k in rules_keys if f"`{k}`" not in conf_doc)
    check(not undoc_rules,
          f"rules with no description in configuration.md: {undoc_rules or 'none'}")

    confirm_keys = module_constant("config.py", "CONFIRM_KEYS")
    check(
        confirm_keys <= rules_keys,
        f"CONFIRM_KEYS with no default value: {sorted(confirm_keys - rules_keys) or 'none'}",
    )

    auto_actions = module_constant("config.py", "AUTO_ACTIONS")
    undoc_auto = sorted(a for a in auto_actions if f"`{a}`" not in conf_doc)
    check(not undoc_auto, f"filter actions with no description: {undoc_auto or 'none'}")

    # write confirmation must not ask about a method that does not write,
    # and must not let a writing method slip past the audit
    write_methods = module_constant("daemon.py", "WRITE_METHODS")
    outbound = module_constant("daemon.py", "CONFIRM_OUTBOUND")
    check(
        outbound <= write_methods,
        f"CONFIRM_OUTBOUND entries that do not write: {sorted(outbound - write_methods) or 'none'}",
    )
    audit_only = module_constant("daemon.py", "AUDIT_ONLY")
    ghost_write = sorted((write_methods | audit_only) - disp)
    check(not ghost_write,
          f"methods in the audit that dispatch does not have: {ghost_write or 'none'}")

    silent = sorted(
        write_methods - outbound - module_constant("daemon.py", "CONFIRM_EXEMPT")
        - set(module_constant("daemon.py", "CONFIRM_CONDITIONAL"))
    )
    undoc_silent = [m for m in silent if f"`{m}`" not in conf_doc]
    check(
        not undoc_silent,
        f"the ones silent in outgoing mode are not described: {undoc_silent or 'none'}",
    )

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
