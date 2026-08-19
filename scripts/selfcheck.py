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
        # frozenset({...}) is the same literal, just wrapped: we unwrap it,
        # otherwise the constant would read as empty and the check as passing.
        if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                and value.func.id in ("frozenset", "set") and value.args):
            value = value.args[0]
        if isinstance(value, ast.Dict):
            return {k.value for k in value.keys if isinstance(k, ast.Constant)}
        if isinstance(value, (ast.Set, ast.Tuple, ast.List)):
            return {e.value for e in value.elts if isinstance(e, ast.Constant)}
    return set()


def module_dict_values(module: str, name: str) -> set[str]:
    """Values of a top-level dictionary — as strings.

    In tables of the form "tool -> right" the meaning is carried by the right
    half, while `module_constant` gives back the left one. Pairs of values
    (SERVER_FLAG_TOOLS) count by their first element: it holds the configuration
    key name, the rest is an explanation.
    """
    tree = ast.parse((ROOT / "tgagent" / module).read_text())
    out: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for item in node.value.values:
            if isinstance(item, (ast.Tuple, ast.List)) and item.elts:
                item = item.elts[0]
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                out.add(item.value)
    return out


def class_constant(module: str, cls: str, name: str) -> set[str]:
    """The same as `module_constant`, but for a constant inside a class.

    Some tables live as fields of `TelegramService` rather than of the module,
    and are invisible at the top level: an unnoticed constant would read as empty.
    """
    tree = ast.parse((ROOT / "tgagent" / module).read_text())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == cls):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == name for t in stmt.targets):
                continue
            value = stmt.value
            if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                    and value.func.id in ("frozenset", "set") and value.args):
                value = value.args[0]
            if isinstance(value, (ast.Set, ast.Tuple, ast.List)):
                return {e.value for e in value.elts if isinstance(e, ast.Constant)}
            if isinstance(value, ast.Dict):
                return {k.value for k in value.keys if isinstance(k, ast.Constant)}
    return set()


def app_config_keys_used() -> set[str]:
    """Keys of help.getAppConfig that the code reads by name.

    It looks for `single.get("x")` and `limits.get("x")` calls (including through
    `lim["limits"]`) — that is the only way this configuration is ever read. A key
    taken by name but not declared in the core's tables is a silent loss: `.get`
    returns None, and the capability reports itself available although it was
    never checked.
    """
    used: set[str] = set()
    for module in ("capabilities.py", "core.py"):
        tree = ast.parse((ROOT / "tgagent" / module).read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                continue
            base = node.func.value
            if isinstance(base, ast.Name):
                holder = base.id
            elif isinstance(base, ast.Subscript) and isinstance(base.slice, ast.Constant):
                holder = base.slice.value
            else:
                continue
            if holder in ("single", "limits"):
                used.add(node.args[0].value)
    # A key taken not as a literal but from the "tool -> server flag" table.
    return used | module_dict_values("capabilities.py", "SERVER_FLAG_TOOLS")


def module_number(module: str, name: str) -> int | None:
    """A numeric top-level constant. By the same parse as the lists."""
    tree = ast.parse((ROOT / "tgagent" / module).read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, int):
                return node.value.value
    return None


def write_guarded_core() -> tuple[set[str], set[str]]:
    """Core methods with a write guard: the unconditional and the conditional ones.

    An unconditional `_assert_write()` as the first thing in the body — the method
    does not work at all with TG_ALLOW_WRITE=0. A conditional one — it works, but
    not all of it. The difference is visible only in the code, and the capabilities
    summary promises it to the owner, so it is reconciled here.
    """
    src = (ROOT / "tgagent" / "core.py").read_text()
    tree = ast.parse(src)
    always: set[str] = set()
    partly: set[str] = set()
    for cls in ast.walk(tree):
        if not (isinstance(cls, ast.ClassDef) and cls.name == "TelegramService"):
            continue
        for fn in cls.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if "_assert_write()" not in (ast.get_source_segment(src, fn) or ""):
                continue
            top = any(
                isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
                and getattr(s.value.func, "attr", None) == "_assert_write"
                for s in fn.body
            )
            (always if top else partly).add(fn.name)
    return always, partly


def arch_file_rows() -> dict[str, int]:
    """The file table from architecture.md: path -> the claimed number of lines."""
    text = (ROOT / "docs" / "architecture.md").read_text()
    rows = re.findall(r"\|\s*\**`(tgagent/[\w.]+)`\**\s*\|\s*(\d+)\s*\|", text)
    return {path: int(n) for path, n in rows}


def i18n_assigned(name: str) -> ast.expr | None:
    """A top-level value from i18n.py, annotation and all.

    `SUPPORTED` and `MESSAGES` are declared as `NAME: type = ...`, which the
    helpers above walk past: they only look at `ast.Assign`. A catalog read
    through them would come back empty, and an empty catalog passes every check.
    """
    tree = ast.parse((ROOT / "tgagent" / "i18n.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return node.value
    return None


def i18n_languages() -> list[str]:
    """The languages the catalog promises — `SUPPORTED`, in the declared order."""
    value = i18n_assigned("SUPPORTED")
    if not isinstance(value, (ast.Tuple, ast.List, ast.Set)):
        return []
    return [e.value for e in value.elts if isinstance(e, ast.Constant)]


def i18n_catalog() -> dict[str, dict[str, str]]:
    """`MESSAGES` from i18n.py: key -> language -> template.

    Unlike the other tables, here the value matters and not the key: what is
    checked is the halves of an entry and the substitutions inside them.
    """
    value = i18n_assigned("MESSAGES")
    if not isinstance(value, ast.Dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for key, entry in zip(value.keys, value.values, strict=True):
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            continue
        if not isinstance(entry, ast.Dict):
            continue
        out[key.value] = {
            lang.value: text.value
            for lang, text in zip(entry.keys, entry.values, strict=True)
            if isinstance(lang, ast.Constant) and isinstance(lang.value, str)
            and isinstance(text, ast.Constant) and isinstance(text.value, str)
        }
    return out


# The same rule as `i18n.placeholders`, repeated here rather than imported: the
# script reads the sources, it does not run them.
PLACEHOLDER = re.compile(r"\{(\w+)")


# Daemon methods the core does not have and must not have: they need the event
# stream, the bot channel or the daemon's own file, not a call to Telegram.
DAEMON_OWN = {
    "status", "accounts", "events", "rules", "alert", "whoami", "wait", "ask",
    # reminders need the tick and the bot channel, the action log needs the daemon's file
    "remind", "actions",
    # the default account choice: an installation setting, not an operation in Telegram
    "account_use",
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


def check_capabilities(tools: dict[str, str]) -> None:
    """The capabilities summary: it promises numbers and lists, and it must not lie.

    Everything reconciled here drifts apart silently: a new tool does not move the
    counter, a new writing method does not show up in the list of the ones blocked
    when writing is off, and a renamed one stays in it as a ghost.
    """
    total = module_number("capabilities.py", "TOOLS_TOTAL")
    check(total == len(tools), f"capabilities.TOOLS_TOTAL: {total} against {len(tools)}")

    src = (ROOT / "tgagent" / "capabilities.py").read_text()
    phantom = sorted({t for t in re.findall(r"\btg_\w+", src) if t not in tools})
    check(not phantom, f"capabilities.py: tools that do not exist: {phantom or 'none'}")

    always, partly = write_guarded_core()
    declared = module_constant("capabilities.py", "WRITE_TOOLS")
    expected = {t for t, m in tools.items() if CORE_ALIASES.get(m, m) in always}
    check(declared == expected,
          "WRITE_TOOLS matches the unconditionally writing methods in the core "
          f"(extra: {sorted(declared - expected) or 'none'}, "
          f"forgotten: {sorted(expected - declared) or 'none'})")

    declared_partial = module_constant("capabilities.py", "PARTIAL_WRITE_TOOLS")
    expected_partial = {t for t, m in tools.items() if CORE_ALIASES.get(m, m) in partly}
    check(declared_partial == expected_partial,
          "PARTIAL_WRITE_TOOLS matches the conditionally writing methods in the core "
          f"(extra: {sorted(declared_partial - expected_partial) or 'none'}, "
          f"forgotten: {sorted(expected_partial - declared_partial) or 'none'})")

    # Telegram caps: their names live in the core, and it is the summary that reads
    # them. A removed or renamed key breaks nothing out loud — `.get` returns None,
    # and the limit simply stops being checked.
    declared_keys = (module_constant("core.py", "APP_CONFIG_LIMITS")
                     | module_constant("core.py", "APP_CONFIG_SINGLES"))
    unknown = sorted(app_config_keys_used() - declared_keys)
    check(not unknown,
          f"Telegram configuration keys not declared in the core: {unknown or 'none'}")

    # Chat rights: every right the code uses has a human-readable name. Without a
    # name the refusal is printed as a bare MTProto field.
    rights = (module_dict_values("capabilities.py", "CHAT_TOOL_RIGHTS")
              | class_constant("core.py", "TelegramService", "ADMIN_ONLY_RIGHTS")
              | class_constant("core.py", "TelegramService", "BROADCAST_ADMIN_ONLY"))
    unnamed = sorted(rights - module_constant("capabilities.py", "RIGHT_NAMES"))
    check(not unnamed, f"chat rights with no human-readable name: {unnamed or 'none'}")


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


def check_i18n() -> None:
    """The language catalog: no holes between the languages, no drifted substitutions.

    A key without a translation is a line in a foreign language in the middle of
    the owner's text; a placeholder renamed in one half only is a phrase with a
    hole in it. Both show up at the moment the message is printed, which is
    exactly the moment nobody is watching.
    """
    langs = i18n_languages()
    catalog = i18n_catalog()

    gaps = sorted(
        f"{key}:{lang}"
        for key, entry in catalog.items()
        for lang in langs
        if not entry.get(lang)
    )
    check(
        bool(langs) and not gaps,
        f"i18n: every key translated into {langs or '?'}: {gaps or 'yes'}",
    )

    drifted = []
    for key, entry in sorted(catalog.items()):
        named = {lang: set(PLACEHOLDER.findall(text)) for lang, text in entry.items()}
        if len({frozenset(s) for s in named.values()}) > 1:
            drifted.append(key + ": " + ", ".join(
                f"{lang} {sorted(names) or '-'}" for lang, names in sorted(named.items())
            ))
    check(not drifted, f"i18n: substitutions differ between languages: {drifted or 'none'}")

    check_commands()


def check_commands() -> None:
    """No hint spells the command by hand.

    `uv run tg` is right in a clone and wrong in an installed package, where `tg`
    is already on PATH and there is no project to run inside. Whoever writes the
    hint cannot know which one the reader has, so the spelling is decided at
    runtime by `config.command_prefix()` — reached from the catalog as `{cmd}`
    and from the code directly. A literal that slips back in is only wrong for
    the half of the users who never file the bug.
    """
    literals = ("uv run tg", "uv sync --extra")
    guilty = []
    for path in sorted((ROOT / "tgagent").glob("*.py")):
        # config.py is where the two spellings are decided; it is allowed to name them.
        if path.name == "config.py":
            continue
        text = path.read_text()
        for line_no, line in enumerate(text.splitlines(), 1):
            if any(bad in line for bad in literals):
                guilty.append(f"{path.name}:{line_no}")
    check(not guilty, f"commands spelled by hand instead of config.command_prefix(): "
                      f"{guilty or 'none'}")


def main() -> int:
    tools = mcp_tools()
    disp = dispatch_methods()
    core = core_methods()
    print(f"MCP tools: {len(tools)} | daemon methods: {len(disp)}")

    check_layers(tools, disp, core)
    check_docs(tools)
    watch = check_agents(tools)
    check_counters(tools, watch)
    check_capabilities(tools)
    check_settings(disp)
    check_i18n()

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
