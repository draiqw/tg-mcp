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


def skip(message: str) -> None:
    """The check does not apply on this machine. Not an error, but not a pass either."""
    notes.append("skip   " + message)


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


# Daemon methods the core does not have and must not have: they need the event
# stream, the bot channel or the daemon's own file, not a call to Telegram.
DAEMON_OWN = {
    "status", "accounts", "events", "rules", "alert", "whoami", "wait", "ask",
    # reminders need the tick and the bot channel, the action log needs the daemon's file
    "remind", "actions",
}

# Some of the core methods are named differently from the daemon method.
CORE_ALIASES = {"pin": "pin_dialog", "unread": "unread_summary"}

# What the watcher is not entitled to. Not only writes into the account: tg_index
# does not touch Telegram, but it lays the correspondence out on disk and knows
# how to wipe it from there.
WATCH_FORBIDDEN = {
    "tg_send_file", "tg_edit", "tg_delete", "tg_forward", "tg_react", "tg_click",
    "tg_poll", "tg_draft", "tg_schedule", "tg_send_location", "tg_block",
    "tg_contact_edit", "tg_create_group", "tg_invite", "tg_moderate",
    "tg_chat_edit", "tg_leave", "tg_folder_edit", "tg_pin", "tg_pin_message",
    "tg_send_sticker", "tg_topic_create", "tg_topic_edit", "tg_bot_edit",
    "tg_cache_clear", "tg_account_use", "tg_rules", "tg_export",
    "tg_sessions", "tg_remind", "tg_index",
}

COUNTER_DOCS = ("README.md", "docs/mcp.md", "docs/architecture.md", "docs/tools.md")


def check_layers(tools: dict[str, str], disp: set[str], core: set[str]) -> None:
    """The three-layer rule: tool — daemon method — core method."""
    no_rpc = sorted(t for t, m in tools.items() if not m)
    check(not no_rpc, f"an RPC call was found for every tool: {no_rpc or 'yes'}")

    orphan = sorted({m for m in tools.values() if m} - disp)
    check(not orphan, f"MCP methods missing from dispatch: {orphan or 'none'}")

    unused = sorted(disp - set(tools.values()) - {"whoami"})
    check(not unused, f"daemon methods with no tool: {unused or 'none'}")

    ghost = [
        m for m in sorted(disp - DAEMON_OWN)
        if m not in core and m.rstrip("_") not in core
        and CORE_ALIASES.get(m) not in core
    ]
    check(not ghost, f"dispatch methods with no implementation in the core: {ghost or 'none'}")


def check_docs(tools: dict[str, str]) -> None:
    """Every tool is described, and no description points at a tool that does not exist."""
    docs = (ROOT / "docs" / "tools.md").read_text()
    undoc = sorted(t for t in tools if t not in docs)
    check(not undoc, f"tools with no description in docs/tools.md: {undoc or 'none'}")

    phantom = sorted({p for p in re.findall(r"`(tg_\w+)`", docs) if p not in tools})
    check(not phantom,
          f"docs/tools.md refers to something that does not exist: {phantom or 'none'}")


def check_agents(tools: dict[str, str]) -> set[str]:
    """Both subagents: the tools exist, the files are installed, the watcher is safe."""
    for name in ("telegram.md", "telegram-watch.md"):
        src = ROOT / "agents" / name
        bad = sorted(agent_tools(src) - set(tools))
        check(not bad, f"{name}: tools that do not exist {bad or 'none'}")
        dst = AGENT_DIR / name
        if dst.exists():
            check(
                filecmp.cmp(src, dst, shallow=False),
                f"{name} installed in ~/.claude/agents at the current version",
            )
        elif AGENT_DIR.exists():
            check(False, f"{name} is not installed in ~/.claude/agents")
        else:
            # There is no Claude Code on this machine at all: a fresh clone, someone
            # else's computer, CI. There is nothing to compare the installed copy
            # against, and that is not drift — otherwise the repository's integrity
            # check would fail everywhere except the owner's workstation.
            skip(f"{name}: no ~/.claude/agents, nothing to compare the installed copy against")

    full = agent_tools(ROOT / "agents" / "telegram.md")
    unseen = sorted(set(tools) - full)
    check(
        full == set(tools),
        f"the telegram agent sees every tool (missing from it: {unseen or 'nothing'})",
    )

    watch = agent_tools(ROOT / "agents" / "telegram-watch.md")
    leak = sorted(watch & WATCH_FORBIDDEN)
    check(not leak, f"the watcher was given no dangerous tools: {leak or 'yes'}")
    return watch


def check_counters(tools: dict[str, str], watch: set[str]) -> None:
    """The numbers the texts state as fact: how many tools and how many lines."""
    for doc in COUNTER_DOCS:
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

    # The file table in architecture.md drifts apart silently with any edit to the
    # code, and reads as fact.
    listed = arch_file_rows()
    modules = {
        f"tgagent/{p.name}": len(p.read_text().splitlines())
        for p in sorted((ROOT / "tgagent").glob("*.py"))
        if p.name != "__init__.py"
    }
    missing = sorted(set(modules) - set(listed))
    check(not missing, f"architecture.md: modules with no row in the table: {missing or 'none'}")
    stale = sorted(
        f"{f} {listed[f]}≠{modules[f]}"
        for f in listed if f in modules and listed[f] != modules[f]
    )
    check(not stale, f"architecture.md: the line counts have drifted: {stale or 'none'}")


def check_settings(disp: set[str]) -> None:
    """Settings: is every one of them described, and have the confirmation lists drifted."""
    conf_doc = (ROOT / "docs" / "configuration.md").read_text()

    # A rule that is not in configuration.md exists only in the code — the owner
    # will not be able to configure it.
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

    # Write confirmation must not ask about a method that writes nothing, and must
    # not let a writing method slip past the audit.
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
        - module_constant("daemon.py", "CONFIRM_CONDITIONAL")
    )
    undoc_silent = [m for m in silent if f"`{m}`" not in conf_doc]
    check(
        not undoc_silent,
        f"the ones silent in outgoing mode are not described: {undoc_silent or 'none'}",
    )


def main() -> int:
    tools = mcp_tools()
    disp = dispatch_methods()
    core = core_methods()
    print(f"MCP tools: {len(tools)} | daemon methods: {len(disp)}")

    check_layers(tools, disp, core)
    check_docs(tools)
    watch = check_agents(tools)
    check_counters(tools, watch)
    check_settings(disp)

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
