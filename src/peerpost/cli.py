"""Command-line interface for peerpost."""

from __future__ import annotations

import argparse
import json
import os
import select
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import __version__
from .client import DaemonNotRunning, NOT_RUNNING, PeerpostClient, PeerpostClientError
from .formatters import format_drain, format_json, format_monitor, format_plain
from .paths import ensure_home, get_paths


KNOWN_AGENT_TYPES = {"claude-code", "codex", "copilot", "antigravity", "generic"}
HOOK_FORMATS = {"codex-hook", "copilot-hook"}
SNIPPET_ADAPTERS = ("claude-code", "codex", "copilot", "antigravity", "generic")


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def client() -> PeerpostClient:
    return PeerpostClient()


def daemon_ping() -> dict[str, Any] | None:
    try:
        return client().request("ping")
    except DaemonNotRunning:
        return None


def start_daemon_background() -> dict[str, Any] | None:
    paths = ensure_home(get_paths())
    data = daemon_ping()
    if data:
        return data
    log = paths.log.open("ab")
    argv = [sys.executable, "-m", "peerpost.daemon", "--foreground"]
    subprocess.Popen(argv, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(30):
        data = daemon_ping()
        if data:
            return data
        time.sleep(0.1)
    return None


def read_hook_input() -> dict[str, Any]:
    if sys.stdin.isatty():
        return {}
    try:
        ready, _, _ = select.select([sys.stdin], [], [], 0)
    except (OSError, ValueError):
        ready = [sys.stdin]
    if not ready:
        return {}
    try:
        raw = sys.stdin.read()
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _contains_repeat_stop(payload: Any) -> bool:
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized = key.replace("-", "_").lower()
            if normalized in {
                "stop_hook_active",
                "agent_stop_active",
                "agentstopactive",
                "copilot_stop_active",
                "copilot_agent_stop_active",
                "copilotagentstopactive",
            } and value is True:
                return True
            if _contains_repeat_stop(value):
                return True
    elif isinstance(payload, list):
        return any(_contains_repeat_stop(item) for item in payload)
    return False


def command_daemon(args: argparse.Namespace) -> int:
    paths = ensure_home(get_paths())
    if args.daemon_command == "start":
        if args.foreground:
            from .daemon import serve_foreground

            serve_foreground(paths)
            return 0
        data = daemon_ping()
        if data:
            print(f"peerpostd is running (pid {data.get('pid')})")
            return 0
        data = start_daemon_background()
        if data:
            print(f"peerpostd started (pid {data.get('pid')})")
            return 0
        eprint("failed to start peerpostd")
        return 1
    if args.daemon_command == "status":
        pid_text = paths.pid.read_text(encoding="utf-8").strip() if paths.pid.exists() else "unknown"
        try:
            data = client().request("ping")
        except DaemonNotRunning:
            print(f"peerpostd is not running (pid file: {pid_text})")
            return 1
        print(f"peerpostd is running (pid {data.get('pid')}, pid file: {pid_text})")
        return 0
    if args.daemon_command == "stop":
        try:
            data = client().request("shutdown")
        except DaemonNotRunning:
            eprint(NOT_RUNNING)
            return 1
        print(f"peerpostd stopping (pid {data.get('pid')})")
        return 0
    eprint("unknown daemon command")
    return 2


def command_join(args: argparse.Namespace) -> int:
    if args.agent_type not in KNOWN_AGENT_TYPES:
        eprint(f"warning: unknown agent type '{args.agent_type}', allowing it")
    data = client().request(
        "join",
        agent=args.agent,
        agent_type=args.agent_type,
        team=args.team,
        workspace=args.workspace,
    )
    if args.output_format == "json":
        print(format_json(data))
        return 0
    print(f"joined {data['id']} ({data['agent_type']}) in team {data['team']}")
    return 0


def command_agents(args: argparse.Namespace) -> int:
    agents = client().request("agents", team=args.team)
    if args.output_format == "json":
        print(format_json(agents))
        return 0
    if not agents:
        print("no agents registered")
        return 0
    for agent in agents:
        workspace = f" {agent['workspace']}" if agent.get("workspace") else ""
        print(f"{agent['team']}/{agent['id']} {agent['agent_type']}{workspace}")
    return 0


def command_leave(args: argparse.Namespace) -> int:
    data = client().request("leave", agent=args.agent, team=args.team)
    if args.output_format == "json":
        print(format_json({"agent": args.agent, "team": args.team, **data}))
        return 0
    if data["removed"]:
        print(f"left {args.agent} from team {args.team}")
    else:
        print(f"{args.agent} was not registered in team {args.team}")
    return 0


def command_send(args: argparse.Namespace) -> int:
    data = client().request(
        "send",
        from_agent=args.from_agent,
        to_agent=args.to_agent,
        broadcast=args.broadcast,
        team=args.team,
        body=args.message,
        kind=args.kind,
        priority=args.priority,
        parent_id=args.reply_to,
    )
    if args.output_format == "json":
        print(format_json(data))
        return 0
    targets = ", ".join(data["targets"]) if data["targets"] else "(none)"
    print(f"sent {data['message']['id']} to {targets}")
    if data.get("delivered_now"):
        print(f"delivered now: {', '.join(data['delivered_now'])}")
    return 0


def command_reply(args: argparse.Namespace) -> int:
    data = client().request(
        "reply",
        from_agent=args.from_agent,
        team=args.team,
        message_id=args.message_id,
        body=args.message,
        kind=args.kind,
        priority=args.priority,
    )
    if args.output_format == "json":
        print(format_json(data))
        return 0
    targets = ", ".join(data["targets"]) if data["targets"] else "(none)"
    print(f"sent {data['message']['id']} in reply to {args.message_id} to {targets}")
    if data.get("delivered_now"):
        print(f"delivered now: {', '.join(data['delivered_now'])}")
    return 0


def print_messages(messages: list[dict[str, Any]], output_format: str = "plain") -> None:
    if output_format == "json":
        print(format_json(messages))
    else:
        text = format_plain(messages)
        if text:
            print(text)


def command_inbox(args: argparse.Namespace) -> int:
    messages = client().request(
        "inbox", agent=args.agent, team=args.team, include_all=args.include_all
    )
    print_messages(messages, args.output_format)
    return 0


def command_read(args: argparse.Namespace) -> int:
    message = client().request(
        "read", message_id=args.message_id, agent=args.agent, team=args.team
    )
    if args.output_format == "json":
        print(format_json(message))
    else:
        print(format_plain([message]))
    return 0


def command_ack(args: argparse.Namespace) -> int:
    data = client().request(
        "ack", message_ids=args.message_ids, agent=args.agent, team=args.team
    )
    if args.output_format == "json":
        print(format_json({"message_ids": args.message_ids, "agent": args.agent, "team": args.team, **data}))
        return 0
    print(f"acknowledged {data['updated']} delivery row(s)")
    return 0


def command_done(args: argparse.Namespace) -> int:
    data = client().request(
        "done", message_ids=args.message_ids, agent=args.agent, team=args.team
    )
    if args.output_format == "json":
        print(format_json({"message_ids": args.message_ids, "agent": args.agent, "team": args.team, **data}))
        return 0
    print(f"marked done {data['updated']} delivery row(s)")
    return 0


def command_drain(args: argparse.Namespace) -> int:
    if args.output_format in HOOK_FORMATS and _contains_repeat_stop(read_hook_input()):
        print("{}")
        return 0
    try:
        messages = client().request(
            "drain", agent=args.agent, team=args.team, limit=args.limit
        )
    except DaemonNotRunning:
        if args.output_format in HOOK_FORMATS:
            print("{}")
            return 0
        raise
    output = format_drain(messages, args.output_format)
    if output:
        print(output)
    return 0


def command_subscribe(args: argparse.Namespace) -> int:
    for event in client().subscribe(args.agent, args.team, include_backlog=args.include_backlog):
        if event.get("event") != "message":
            continue
        message = event["message"]
        if args.output_format == "json":
            print(format_json(message), flush=True)
        elif args.output_format == "monitor":
            print(format_monitor(message), flush=True)
        else:
            print(format_plain([message]), flush=True)
    return 0


def command_history(args: argparse.Namespace) -> int:
    messages = client().request(
        "history", team=args.team, agent=args.agent, with_agent=args.with_agent
    )
    print_messages(messages, args.output_format)
    return 0


def command_thread(args: argparse.Namespace) -> int:
    messages = client().request("thread", team=args.team, message_id=args.message_id)
    print_messages(messages, args.output_format)
    return 0


def command_prune(args: argparse.Namespace) -> int:
    before = (
        args.before
        or (datetime.now(UTC) - timedelta(days=args.older_than_days))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    data = client().request(
        "prune",
        team=args.team,
        before=before,
        limit=args.limit,
        apply=args.apply,
    )
    if args.output_format == "json":
        print(format_json(data))
        return 0
    action = "deleted" if args.apply else "would delete"
    print(
        f"prune: {action} {data['matched'] if not args.apply else data['deleted']} "
        f"done message(s) before {data['before']}"
    )
    if not args.apply:
        print("dry run only; pass --apply to delete")
    for message_id in data["message_ids"]:
        print(message_id)
    return 0


def default_backup_path() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return get_paths().home / "backups" / f"peerpost-{stamp}.sqlite"


def command_backup(args: argparse.Namespace) -> int:
    output = (Path(args.output).expanduser() if args.output else default_backup_path()).resolve()
    data = client().request("backup", output=str(output), overwrite=args.overwrite)
    if args.output_format == "json":
        print(format_json(data))
        return 0
    print(f"backup: {data['path']} ({data['bytes']} bytes)")
    return 0


def command_paths(_args: argparse.Namespace) -> int:
    paths = get_paths()
    print(f"home: {paths.home}")
    print(f"db: {paths.db}")
    print(f"socket: {paths.socket}")
    print(f"pid: {paths.pid}")
    print(f"log: {paths.log}")
    return 0


def command_logs(args: argparse.Namespace) -> int:
    paths = get_paths()
    if not paths.log.exists():
        if args.output_format == "json":
            print(format_json({"path": str(paths.log), "lines": []}))
        else:
            print(f"no log file found: {paths.log}")
        return 0
    lines = paths.log.read_text(encoding="utf-8", errors="replace").splitlines()
    if args.tail >= 0:
        lines = lines[-args.tail :] if args.tail else []
    if args.output_format == "json":
        print(format_json({"path": str(paths.log), "lines": lines}))
    else:
        for line in lines:
            print(line)
    return 0


def _mode(path: Path) -> str:
    try:
        return oct(path.stat().st_mode & 0o777)
    except OSError:
        return "unknown"


def _mode_int(path: Path) -> int | None:
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return None


def _doctor_check(
    checks: list[dict[str, Any]],
    name: str,
    status: str,
    detail: str,
    fix: str | None = None,
) -> None:
    check = {"name": name, "status": status, "detail": detail}
    if fix:
        check["fix"] = fix
    checks.append(check)


def command_doctor(args: argparse.Namespace) -> int:
    try:
        paths = ensure_home(get_paths())
    except OSError as exc:
        eprint(f"home: error: {exc}")
        return 1

    checks: list[dict[str, Any]] = []
    home_mode = _mode_int(paths.home)
    if home_mode == 0o700:
        _doctor_check(checks, "home", "ok", f"{paths.home} mode {_mode(paths.home)}")
    else:
        _doctor_check(
            checks,
            "home",
            "warn",
            f"{paths.home} mode {_mode(paths.home)}; expected 0o700",
            f"chmod 700 {paths.home}",
        )

    if paths.db.exists():
        try:
            conn = sqlite3.connect(f"file:{paths.db}?mode=ro", uri=True)
            try:
                version = conn.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
            finally:
                conn.close()
            detail = f"{paths.db} mode {_mode(paths.db)}"
            if version:
                detail += f" schema_version {version[0]}"
            db_mode = _mode_int(paths.db)
            if version and version[0] != "1":
                _doctor_check(checks, "database", "error", detail, "unsupported schema; backup the database before migrating")
            elif db_mode not in (None, 0o600):
                _doctor_check(checks, "database", "warn", f"{detail}; expected mode 0o600", f"chmod 600 {paths.db}")
            else:
                _doctor_check(checks, "database", "ok", detail)
        except sqlite3.Error as exc:
            _doctor_check(checks, "database", "error", f"{paths.db}: {exc}", "check peerpost.sqlite or restore from backup")
    else:
        _doctor_check(
            checks,
            "database",
            "info",
            f"{paths.db} does not exist yet",
            "peerpost setup --start-daemon",
        )

    pid_detail = paths.pid.read_text(encoding="utf-8").strip() if paths.pid.exists() else "missing"
    _doctor_check(checks, "pid", "ok" if paths.pid.exists() else "info", str(pid_detail))

    daemon_running = False
    try:
        data = client().request("ping")
    except DaemonNotRunning:
        _doctor_check(checks, "daemon", "warn", NOT_RUNNING, "peerpost daemon start")
    except PeerpostClientError as exc:
        _doctor_check(checks, "daemon", "error", str(exc), "peerpost daemon stop; peerpost daemon start")
    else:
        daemon_running = True
        _doctor_check(checks, "daemon", "ok", f"running pid {data.get('pid')}")

    if paths.socket.exists() and daemon_running:
        _doctor_check(checks, "socket", "ok", str(paths.socket))
    elif paths.socket.exists():
        _doctor_check(checks, "socket", "warn", f"{paths.socket} exists but daemon is not responding", f"rm -f {paths.socket}; peerpost daemon start")
    else:
        _doctor_check(checks, "socket", "info", str(paths.socket), "peerpost daemon start")

    peerpost_bin = shutil.which("peerpost")
    if peerpost_bin:
        _doctor_check(checks, "path", "ok", f"peerpost found at {peerpost_bin}")
    else:
        _doctor_check(checks, "path", "info", "peerpost command was not found on PATH", "python -m pip install -e .")

    _doctor_check(checks, "log", "info", str(paths.log))

    has_errors = any(check["status"] == "error" for check in checks)
    has_warnings = any(check["status"] == "warn" for check in checks)
    status = "errors" if has_errors else "warnings" if has_warnings else "ok"
    report = {
        "status": status,
        "paths": {
            "home": str(paths.home),
            "db": str(paths.db),
            "socket": str(paths.socket),
            "pid": str(paths.pid),
            "log": str(paths.log),
        },
        "checks": checks,
    }

    if args.output_format == "json":
        print(format_json(report))
    else:
        for check in checks:
            print(f"{check['name']}: {check['status']}: {check['detail']}")
            if check.get("fix"):
                print(f"  fix: {check['fix']}")
        print(f"status: {status}")
    if has_errors or (args.strict and has_warnings):
        return 1
    return 0


def snippet_for(adapter: str, agent: str, team: str) -> str:
    if adapter == "claude-code":
        return (
            "# Claude Code Monitor\n"
            f"peerpost subscribe --agent {agent} --team {team} --include-backlog --format monitor"
        )
    if adapter == "codex":
        return (
            "# Codex Stop hook command\n"
            f"peerpost drain --agent {agent} --team {team} --format codex-hook"
        )
    if adapter == "copilot":
        return (
            "# Copilot agentStop hook command\n"
            f"peerpost drain --agent {agent} --team {team} --format copilot-hook"
        )
    if adapter == "antigravity":
        return (
            "# Antigravity/local generic receive command\n"
            f"peerpost drain --agent {agent} --team {team} --format plain\n"
            f"# Or keep a live local monitor open:\n"
            f"peerpost subscribe --agent {agent} --team {team} --format monitor"
        )
    return (
        "# Generic local agent receive command\n"
        f"peerpost drain --agent {agent} --team {team} --format plain\n"
        f"# Or keep a live local monitor open:\n"
        f"peerpost subscribe --agent {agent} --team {team} --format monitor"
    )


def command_install_snippets(args: argparse.Namespace) -> int:
    adapters = SNIPPET_ADAPTERS if args.adapter == "all" else (args.adapter,)
    blocks: list[str] = []
    for adapter in adapters:
        agent = args.agent or ("claude" if adapter == "claude-code" else adapter)
        blocks.append(snippet_for(adapter, agent, args.team))
    print("\n\n".join(blocks))
    return 0


def command_setup(args: argparse.Namespace) -> int:
    paths = ensure_home(get_paths())
    daemon = start_daemon_background() if args.start_daemon else daemon_ping()
    adapters = SNIPPET_ADAPTERS if args.adapter == "all" else (args.adapter,)
    snippets = []
    for adapter in adapters:
        agent = args.agent or ("claude" if adapter == "claude-code" else adapter)
        snippets.append({"adapter": adapter, "agent": agent, "snippet": snippet_for(adapter, agent, args.team)})

    data = {
        "home": str(paths.home),
        "db": str(paths.db),
        "socket": str(paths.socket),
        "pid": str(paths.pid),
        "log": str(paths.log),
        "daemon": daemon or {"status": "not_running"},
        "team": args.team,
        "snippets": snippets,
    }
    if args.output_format == "json":
        print(format_json(data))
        return 0

    print("peerpost setup")
    print(f"home: {paths.home}")
    print(f"db: {paths.db}")
    print(f"socket: {paths.socket}")
    if daemon:
        print(f"daemon: running pid {daemon.get('pid')}")
    else:
        print("daemon: not running (start with: peerpost daemon start)")
    if snippets:
        print()
        print("agent snippets:")
        for item in snippets:
            print()
            print(item["snippet"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="peerpost")
    parser.add_argument("--version", action="version", version=f"peerpost {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    daemon = sub.add_parser("daemon")
    daemon_sub = daemon.add_subparsers(dest="daemon_command", required=True)
    start = daemon_sub.add_parser("start")
    start.add_argument("--foreground", action="store_true")
    daemon_sub.add_parser("status")
    daemon_sub.add_parser("stop")
    daemon.set_defaults(func=command_daemon)

    join = sub.add_parser("join")
    join.add_argument("--agent", required=True)
    join.add_argument("--type", dest="agent_type", required=True)
    join.add_argument("--team", required=True)
    join.add_argument("--workspace")
    join.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    join.set_defaults(func=command_join)

    agents = sub.add_parser("agents")
    agents.add_argument("--team")
    agents.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    agents.set_defaults(func=command_agents)

    leave = sub.add_parser("leave")
    leave.add_argument("--agent", required=True)
    leave.add_argument("--team", required=True)
    leave.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    leave.set_defaults(func=command_leave)

    send = sub.add_parser("send")
    send.add_argument("--from", dest="from_agent", required=True)
    target = send.add_mutually_exclusive_group(required=True)
    target.add_argument("--to", dest="to_agent")
    target.add_argument("--broadcast", action="store_true")
    send.add_argument("--team", required=True)
    send.add_argument("--kind", default="message")
    send.add_argument(
        "--priority",
        choices=["low", "normal", "high", "urgent"],
        default="normal",
    )
    send.add_argument("--reply-to", help="parent message id this message replies to")
    send.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    send.add_argument("message")
    send.set_defaults(func=command_send)

    reply = sub.add_parser("reply")
    reply.add_argument("message_id")
    reply.add_argument("--from", dest="from_agent", required=True)
    reply.add_argument("--team", required=True)
    reply.add_argument("--kind", default="reply")
    reply.add_argument(
        "--priority",
        choices=["low", "normal", "high", "urgent"],
        default="normal",
    )
    reply.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    reply.add_argument("message")
    reply.set_defaults(func=command_reply)

    inbox = sub.add_parser("inbox")
    inbox.add_argument("--agent", required=True)
    inbox.add_argument("--team", required=True)
    inbox.add_argument("--all", dest="include_all", action="store_true")
    inbox.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    inbox.set_defaults(func=command_inbox)

    read = sub.add_parser("read")
    read.add_argument("message_id")
    read.add_argument("--agent", required=True)
    read.add_argument("--team", required=True)
    read.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    read.set_defaults(func=command_read)

    ack = sub.add_parser("ack")
    ack.add_argument("message_ids", nargs="+")
    ack.add_argument("--agent", required=True)
    ack.add_argument("--team", required=True)
    ack.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    ack.set_defaults(func=command_ack)

    done = sub.add_parser("done")
    done.add_argument("message_ids", nargs="+")
    done.add_argument("--agent", required=True)
    done.add_argument("--team", required=True)
    done.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    done.set_defaults(func=command_done)

    drain = sub.add_parser("drain")
    drain.add_argument("--agent", required=True)
    drain.add_argument("--team", required=True)
    drain.add_argument("--limit", type=int, default=20)
    drain.add_argument(
        "--format",
        dest="output_format",
        choices=["plain", "json", "codex-hook", "copilot-hook"],
        default="plain",
    )
    drain.set_defaults(func=command_drain)

    subscribe = sub.add_parser("subscribe")
    subscribe.add_argument("--agent", required=True)
    subscribe.add_argument("--team", required=True)
    subscribe.add_argument("--include-backlog", action="store_true")
    subscribe.add_argument(
        "--format",
        dest="output_format",
        choices=["plain", "json", "monitor"],
        default="plain",
    )
    subscribe.set_defaults(func=command_subscribe)

    history = sub.add_parser("history")
    history.add_argument("--team", required=True)
    history.add_argument("--agent")
    history.add_argument("--with", dest="with_agent")
    history.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    history.set_defaults(func=command_history)

    thread = sub.add_parser("thread")
    thread.add_argument("message_id")
    thread.add_argument("--team", required=True)
    thread.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    thread.set_defaults(func=command_thread)

    prune = sub.add_parser("prune")
    prune.add_argument("--team")
    prune.add_argument("--older-than-days", type=int, default=30)
    prune.add_argument("--before", help="UTC ISO timestamp cutoff, e.g. 2026-06-01T00:00:00Z")
    prune.add_argument("--limit", type=int, default=100)
    prune.add_argument("--apply", action="store_true", help="delete matched messages")
    prune.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    prune.set_defaults(func=command_prune)

    backup = sub.add_parser("backup")
    backup.add_argument("--output", help="backup SQLite path; default is <PEERPOST_HOME>/backups/peerpost-<timestamp>.sqlite")
    backup.add_argument("--overwrite", action="store_true", help="replace the output file if it already exists")
    backup.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    backup.set_defaults(func=command_backup)

    paths = sub.add_parser("paths")
    paths.set_defaults(func=command_paths)

    logs = sub.add_parser("logs")
    logs.add_argument("--tail", type=int, default=50)
    logs.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    logs.set_defaults(func=command_logs)

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    doctor.add_argument("--strict", action="store_true", help="return nonzero when warnings are present")
    doctor.set_defaults(func=command_doctor)

    snippets = sub.add_parser("install-snippets", aliases=["snippets"])
    snippets.add_argument(
        "--adapter",
        choices=[*SNIPPET_ADAPTERS, "all"],
        default="all",
        help="agent adapter snippet to print",
    )
    snippets.add_argument("--team", default="dev")
    snippets.add_argument("--agent", help="override the agent id used in the snippet")
    snippets.set_defaults(func=command_install_snippets)

    setup = sub.add_parser("setup")
    setup.add_argument("--team", default="dev")
    setup.add_argument(
        "--adapter",
        choices=[*SNIPPET_ADAPTERS, "all"],
        default="all",
        help="agent adapter snippets to print",
    )
    setup.add_argument("--agent", help="override the agent id used in snippets")
    setup.add_argument("--start-daemon", action="store_true")
    setup.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    setup.set_defaults(func=command_setup)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except DaemonNotRunning:
        eprint(NOT_RUNNING)
        return 1
    except PeerpostClientError as exc:
        eprint(str(exc))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
