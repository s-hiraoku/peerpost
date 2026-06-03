"""Command-line interface for peerpost."""

from __future__ import annotations

import argparse
import html
import json
import os
import select
import shlex
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
from .paths import (
    ensure_home,
    get_paths,
    restrict_file,
    sqlite_sidecar_paths,
    unix_socket_path_length_message,
    unix_socket_path_too_long,
)
from .security import safe_field, strip_control_chars


KNOWN_AGENT_TYPES = {"claude-code", "codex", "copilot", "antigravity", "generic"}
HOOK_FORMATS = {"codex-hook", "copilot-hook"}
SNIPPET_ADAPTERS = ("claude-code", "codex", "copilot", "antigravity", "generic")
DAEMON_SNIPPETS = ("launchd", "systemd")
DAEMON_AUTOSTART_TARGETS = ("auto", "launchd", "systemd")
DEFAULT_SETUP_AGENTS = (
    ("claude", "claude-code"),
    ("codex", "codex"),
    ("copilot", "copilot"),
)
QUICKSTART_ADAPTERS = ("claude-code", "codex", "copilot")
AGENT_TYPE_ALIASES = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "codex": "codex",
    "copilot": "copilot",
    "antigravity": "antigravity",
    "generic": "generic",
}


def env_default(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def add_team_arg(
    parser: argparse.ArgumentParser,
    *,
    required: bool = False,
    default: str | None = None,
    help: str | None = None,
) -> None:
    env_team = env_default("PEERPOST_TEAM")
    parser.add_argument(
        "--team",
        required=required and env_team is None,
        default=env_team or default,
        help=help,
    )


def add_agent_arg(
    parser: argparse.ArgumentParser,
    *,
    dest: str = "agent",
    required: bool = False,
    default: str | None = None,
    help: str | None = None,
) -> None:
    env_agent = env_default("PEERPOST_AGENT")
    parser.add_argument(
        "--agent" if dest == "agent" else "--from",
        dest=dest,
        required=required and env_agent is None,
        default=env_agent or default,
        help=help,
    )


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def client() -> PeerpostClient:
    return PeerpostClient()


def daemon_ping() -> dict[str, Any] | None:
    try:
        return client().request("ping")
    except DaemonNotRunning:
        return None


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
        if unix_socket_path_too_long(paths.socket):
            eprint(f"socket path too long: {unix_socket_path_length_message(paths.socket)}")
            eprint("set PEERPOST_SOCKET to a shorter path such as /tmp/peerpost-$(id -u).sock")
            return 2
        if args.foreground:
            from .daemon import serve_foreground

            serve_foreground(paths)
            return 0
        data = daemon_ping()
        if data:
            print(f"peerpostd is running (pid {data.get('pid')}, version {data.get('version', 'unknown')})")
            return 0
        data = start_daemon_background()
        if data:
            print(f"peerpostd started (pid {data.get('pid')}, version {data.get('version', 'unknown')})")
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
        print(
            f"peerpostd is running (pid {data.get('pid')}, "
            f"version {data.get('version', 'unknown')}, pid file: {pid_text})"
        )
        return 0
    if args.daemon_command == "stop":
        try:
            data = client().request("shutdown")
        except DaemonNotRunning:
            eprint(NOT_RUNNING)
            return 1
        print(f"peerpostd stopping (pid {data.get('pid')})")
        return 0
    if args.daemon_command == "install-autostart":
        try:
            target = resolve_daemon_autostart_target(args.target)
            path = daemon_autostart_path(target)
        except ValueError as exc:
            eprint(str(exc))
            return 2
        if path.exists() and not args.overwrite:
            eprint(f"autostart file already exists: {path}; pass --overwrite to replace it")
            return 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(daemon_config_for(target), encoding="utf-8")
        try:
            path.chmod(0o644)
        except OSError:
            pass
        print(f"installed {target} autostart: {path}")
        print(f"enable with: {daemon_autostart_enable_command(target, path)}")
        print(f"disable with: {daemon_autostart_disable_command(target, path)}")
        return 0
    if args.daemon_command == "uninstall-autostart":
        try:
            target = resolve_daemon_autostart_target(args.target)
            path = daemon_autostart_path(target)
        except ValueError as exc:
            eprint(str(exc))
            return 2
        if path.exists():
            path.unlink()
            print(f"removed {target} autostart: {path}")
        else:
            print(f"no {target} autostart file found: {path}")
        print(f"also run: {daemon_autostart_disable_command(target, path)}")
        return 0
    eprint("unknown daemon command")
    return 2


def command_join(args: argparse.Namespace) -> int:
    if args.agent_type not in KNOWN_AGENT_TYPES:
        eprint(f"warning: unknown agent type '{safe_field(args.agent_type)}', allowing it")
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
    print(
        f"joined {safe_field(data['id'])} ({safe_field(data['agent_type'])}) "
        f"in team {safe_field(data['team'])}"
    )
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
        workspace = f" {safe_field(agent['workspace'])}" if agent.get("workspace") else ""
        print(
            f"{safe_field(agent['team'])}/{safe_field(agent['id'])} "
            f"{safe_field(agent['agent_type'])}{workspace}"
        )
    return 0


def command_status(args: argparse.Namespace) -> int:
    data = client().request("team_status", team=args.team)
    if args.output_format == "json":
        print(format_json(data))
        return 0

    print(f"team: {safe_field(data['team'])}")
    agents = data.get("agents", [])
    if not agents:
        print("no agents registered")
    else:
        print("agent            type          pending  delivered  acknowledged  done  last pending")
        for agent in agents:
            print(
                f"{safe_field(agent['id'])[:16]:16} "
                f"{safe_field(agent['agent_type'])[:13]:13} "
                f"{int(agent.get('pending', 0)):7} "
                f"{int(agent.get('delivered', 0)):10} "
                f"{int(agent.get('acknowledged', 0)):12} "
                f"{int(agent.get('done', 0)):4}  "
                f"{safe_field(agent.get('last_pending_at') or '-')}"
            )

    unregistered = data.get("unregistered", [])
    if unregistered:
        print()
        print("unregistered recipients:")
        for item in unregistered:
            print(
                f"  {safe_field(item['to_agent'])}: "
                f"pending={int(item.get('pending', 0))} "
                f"delivered={int(item.get('delivered', 0))} "
                f"acknowledged={int(item.get('acknowledged', 0))} "
                f"done={int(item.get('done', 0))} "
                f"last_pending={safe_field(item.get('last_pending_at') or '-')}"
            )
    return 0


def command_leave(args: argparse.Namespace) -> int:
    data = client().request("leave", agent=args.agent, team=args.team)
    if args.output_format == "json":
        print(format_json({"agent": args.agent, "team": args.team, **data}))
        return 0
    if data["removed"]:
        print(f"left {safe_field(args.agent)} from team {safe_field(args.team)}")
    else:
        print(f"{safe_field(args.agent)} was not registered in team {safe_field(args.team)}")
    return 0


def message_body_arg(args: argparse.Namespace) -> str:
    if args.stdin and args.message is not None:
        raise ValueError("pass either MESSAGE or --stdin, not both")
    if args.stdin:
        return sys.stdin.read()
    if args.message is None:
        raise ValueError("missing message body; pass MESSAGE or --stdin")
    return args.message


def command_send(args: argparse.Namespace) -> int:
    body = message_body_arg(args)
    data = client().request(
        "send",
        from_agent=args.from_agent,
        to_agent=args.to_agent,
        broadcast=args.broadcast,
        team=args.team,
        body=body,
        kind=args.kind,
        priority=args.priority,
        parent_id=args.reply_to,
    )
    warn_unregistered_agents(data)
    if args.output_format == "json":
        print(format_json(data))
        return 0
    targets = ", ".join(safe_field(target) for target in data["targets"]) if data["targets"] else "(none)"
    print(f"sent {safe_field(data['message']['id'])} to {targets}")
    if data.get("delivered_now"):
        delivered = ", ".join(safe_field(target) for target in data["delivered_now"])
        print(f"delivered now: {delivered}")
    return 0


def command_reply(args: argparse.Namespace) -> int:
    body = message_body_arg(args)
    data = client().request(
        "reply",
        from_agent=args.from_agent,
        team=args.team,
        message_id=args.message_id,
        body=body,
        kind=args.kind,
        priority=args.priority,
    )
    warn_unregistered_agents(data)
    if args.output_format == "json":
        print(format_json(data))
        return 0
    targets = ", ".join(safe_field(target) for target in data["targets"]) if data["targets"] else "(none)"
    reply_to = data["message"].get("parent_id") or args.message_id
    print(
        f"sent {safe_field(data['message']['id'])} in reply to "
        f"{safe_field(reply_to)} to {targets}"
    )
    if data.get("delivered_now"):
        delivered = ", ".join(safe_field(target) for target in data["delivered_now"])
        print(f"delivered now: {delivered}")
    return 0


def warn_unregistered_agents(data: dict[str, Any]) -> None:
    if data.get("unregistered_from_agent"):
        message = data["message"]
        eprint(
            "warning: sender agent is not registered: "
            f"{safe_field(message['from_agent'])}. Register with: peerpost join --agent "
            f"{safe_field(message['from_agent'])} --type generic --team {safe_field(message['team'])}"
        )
    targets = data.get("unregistered_targets") or []
    if targets:
        safe_targets = ", ".join(safe_field(target) for target in targets)
        eprint(
            "warning: unregistered recipient id(s): "
            f"{safe_targets}. Check with: peerpost agents --team "
            f"{safe_field(data['message']['team'])}"
        )


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
    if args.limit < 1:
        raise ValueError("--limit must be 1 or greater")
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


def prune_cutoff(args: argparse.Namespace) -> str:
    now = datetime.now(UTC).replace(microsecond=0)
    if args.limit < 1:
        raise ValueError("--limit must be 1 or greater")
    if args.before:
        try:
            cutoff = datetime.fromisoformat(args.before.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("--before must be a valid UTC ISO timestamp") from exc
        if cutoff.tzinfo is None:
            raise ValueError("--before must include a timezone, for example 2026-06-01T00:00:00Z")
        cutoff = cutoff.astimezone(UTC).replace(microsecond=0)
    else:
        if args.older_than_days < 1:
            raise ValueError("--older-than-days must be 1 or greater")
        cutoff = now - timedelta(days=args.older_than_days)
    if cutoff >= now:
        raise ValueError("--before must be in the past")
    return cutoff.isoformat().replace("+00:00", "Z")


def command_prune(args: argparse.Namespace) -> int:
    if args.backup_output and not args.backup_first:
        raise ValueError("--backup-output requires --backup-first")
    before = prune_cutoff(args)
    backup_data: dict[str, Any] | None = None
    if args.backup_first:
        if not args.apply:
            raise ValueError("--backup-first requires --apply")
        backup_output = (
            Path(args.backup_output).expanduser().resolve()
            if args.backup_output
            else default_backup_path()
        )
        backup_data = client().request(
            "backup",
            output=str(backup_output),
            overwrite=False,
        )
        if not backup_data.get("verified"):
            if args.output_format == "json":
                print(format_json({"status": "backup_failed", "backup": backup_data, "prune": None}))
            else:
                eprint("backup integrity check failed; prune was not applied")
                print(f"backup: {backup_data['path']} ({backup_data['bytes']} bytes, integrity unverified)")
            return 1
    data = client().request(
        "prune",
        team=args.team,
        before=before,
        limit=args.limit,
        apply=args.apply,
    )
    if backup_data is not None:
        data = {**data, "backup": backup_data}
    if args.output_format == "json":
        print(format_json(data))
        return 0
    if backup_data is not None:
        print(
            f"backup: {backup_data['path']} "
            f"({backup_data['bytes']} bytes, integrity verified)"
        )
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
        return 0 if data.get("verified") else 1
    status = "verified" if data.get("verified") else "unverified"
    print(f"backup: {data['path']} ({data['bytes']} bytes, integrity {status})")
    if not data.get("verified"):
        eprint("backup integrity check failed; do not use this backup for restore")
        return 1
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
    if args.tail < 0:
        raise ValueError("--tail must be 0 or greater")
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
            print(strip_control_chars(line))
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


def _repair_private_file(path: Path, repairs: list[str]) -> None:
    if path.exists() and _mode_int(path) != 0o600:
        restrict_file(path)
        repairs.append(f"chmod 600 {path}")


def _doctor_autostart_check(checks: list[dict[str, Any]]) -> None:
    try:
        target = resolve_daemon_autostart_target("auto")
        path = daemon_autostart_path(target)
    except ValueError as exc:
        _doctor_check(checks, "autostart", "info", str(exc))
        return

    if not path.exists():
        _doctor_check(
            checks,
            "autostart",
            "info",
            f"{target} autostart is not installed: {path}",
            "peerpost daemon install-autostart",
        )
        return

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        _doctor_check(
            checks,
            "autostart",
            "warn",
            f"{path} exists but could not be read: {exc}",
            "check file permissions",
        )
        return

    if content == daemon_config_for(target):
        _doctor_check(checks, "autostart", "ok", f"{target} autostart installed: {path}")
        return

    _doctor_check(
        checks,
        "autostart",
        "warn",
        f"{target} autostart exists but does not match current peerpost paths/runtime: {path}",
        "peerpost daemon install-autostart --overwrite",
    )


def command_doctor(args: argparse.Namespace) -> int:
    raw_paths = get_paths()
    home_mode_before = _mode_int(raw_paths.home) if raw_paths.home.exists() else None
    try:
        paths = ensure_home(raw_paths)
    except OSError as exc:
        eprint(f"home: error: {exc}")
        return 1

    repairs: list[str] = []
    home_mode_after_ensure = _mode_int(paths.home)
    if (
        args.fix
        and home_mode_before not in (None, 0o700)
        and home_mode_after_ensure == 0o700
    ):
        repairs.append(f"chmod 700 {paths.home}")
    if args.fix:
        _repair_private_file(paths.db, repairs)
        for sidecar in sqlite_sidecar_paths(paths.db):
            _repair_private_file(sidecar, repairs)
        _repair_private_file(paths.pid, repairs)
        _repair_private_file(paths.log, repairs)

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
                quick_check = [
                    row[0] for row in conn.execute("PRAGMA quick_check").fetchall()
                ]
                foreign_key_check = [
                    tuple(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()
                ]
            finally:
                conn.close()
            detail = f"{paths.db} mode {_mode(paths.db)}"
            if version:
                detail += f" schema_version {version[0]}"
            if quick_check == ["ok"]:
                detail += " quick_check ok"
            if not foreign_key_check:
                detail += " foreign_key_check ok"
            db_mode = _mode_int(paths.db)
            sidecar_modes = {
                sidecar: _mode_int(sidecar)
                for sidecar in sqlite_sidecar_paths(paths.db)
                if sidecar.exists()
            }
            bad_sidecars = [
                (sidecar, mode)
                for sidecar, mode in sidecar_modes.items()
                if mode not in (None, 0o600)
            ]
            if quick_check != ["ok"]:
                _doctor_check(
                    checks,
                    "database",
                    "error",
                    f"{detail}; quick_check failed: "
                    f"{'; '.join(str(item) for item in quick_check)}",
                    "restore from a recent peerpost backup",
                )
            elif foreign_key_check:
                foreign_key_detail = "; ".join(
                    f"{table} rowid={rowid} parent={parent} fkid={fkid}"
                    for table, rowid, parent, fkid in foreign_key_check[:5]
                )
                remaining = len(foreign_key_check) - 5
                if remaining > 0:
                    foreign_key_detail += f"; +{remaining} more"
                _doctor_check(
                    checks,
                    "database",
                    "error",
                    f"{detail}; foreign_key_check failed: {foreign_key_detail}",
                    "restore from a recent peerpost backup",
                )
            elif version and version[0] != "1":
                _doctor_check(checks, "database", "error", detail, "unsupported schema; backup the database before migrating")
            elif db_mode not in (None, 0o600):
                _doctor_check(checks, "database", "warn", f"{detail}; expected mode 0o600", f"chmod 600 {paths.db}")
            elif bad_sidecars:
                sidecar_detail = ", ".join(f"{sidecar} mode {oct(mode or 0)}" for sidecar, mode in bad_sidecars)
                _doctor_check(
                    checks,
                    "database",
                    "warn",
                    f"{detail}; SQLite sidecar mode should be 0o600: {sidecar_detail}",
                    "chmod 600 " + " ".join(str(sidecar) for sidecar, _mode_value in bad_sidecars),
                )
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

    daemon_running = False
    daemon_version: str | None = None
    daemon_pid: int | None = None
    try:
        data = client().request("ping")
    except DaemonNotRunning:
        _doctor_check(checks, "daemon", "warn", NOT_RUNNING, "peerpost daemon start")
    except PeerpostClientError as exc:
        _doctor_check(checks, "daemon", "error", str(exc), "peerpost daemon stop; peerpost daemon start")
    else:
        daemon_running = True
        daemon_version = data.get("version")
        raw_daemon_pid = data.get("pid")
        daemon_pid = raw_daemon_pid if isinstance(raw_daemon_pid, int) else None
        daemon_detail = f"running pid {data.get('pid')} version {daemon_version or 'unknown'}"
        _doctor_check(checks, "daemon", "ok", daemon_detail)
        if daemon_version is None:
            _doctor_check(
                checks,
                "version",
                "warn",
                f"cli {__version__}; daemon version unknown",
                "peerpost daemon stop; peerpost daemon start",
            )
        elif daemon_version != __version__:
            _doctor_check(
                checks,
                "version",
                "warn",
                f"cli {__version__}; daemon {daemon_version}",
                "peerpost daemon stop; peerpost daemon start",
            )
        else:
            _doctor_check(checks, "version", "ok", f"cli {__version__}; daemon {daemon_version}")

    if args.fix and paths.pid.exists() and not daemon_running:
        try:
            paths.pid.unlink()
        except OSError as exc:
            _doctor_check(
                checks,
                "pid",
                "warn",
                f"{paths.pid} exists but could not be removed: {exc}",
                f"rm -f {paths.pid}; peerpost daemon start",
            )
        else:
            repairs.append(f"removed stale pid {paths.pid}")

    if paths.pid.exists():
        pid_text = paths.pid.read_text(encoding="utf-8").strip()
        try:
            pid_value = int(pid_text)
        except ValueError:
            if daemon_running:
                _doctor_check(
                    checks,
                    "pid",
                    "warn",
                    f"{paths.pid} contains invalid pid {safe_field(pid_text)!r}",
                    "peerpost daemon stop; peerpost daemon start",
                )
            else:
                _doctor_check(
                    checks,
                    "pid",
                    "warn",
                    f"{paths.pid} contains invalid pid {safe_field(pid_text)!r}",
                    f"rm -f {paths.pid}; peerpost daemon start",
                )
        else:
            if daemon_running and daemon_pid == pid_value:
                _doctor_check(checks, "pid", "ok", f"{paths.pid}: {pid_value}")
            elif daemon_running:
                _doctor_check(
                    checks,
                    "pid",
                    "warn",
                    f"{paths.pid}: {pid_value}; daemon reports pid {daemon_pid}",
                    "peerpost daemon stop; peerpost daemon start",
                )
            elif _pid_is_running(pid_value):
                _doctor_check(
                    checks,
                    "pid",
                    "warn",
                    f"{paths.pid}: {pid_value}; peerpostd is not responding",
                    f"rm -f {paths.pid}; peerpost daemon start",
                )
            else:
                _doctor_check(
                    checks,
                    "pid",
                    "warn",
                    f"{paths.pid}: {pid_value}; process is not running",
                    f"rm -f {paths.pid}; peerpost daemon start",
                )
    elif daemon_running:
        _doctor_check(
            checks,
            "pid",
            "warn",
            f"{paths.pid} is missing while peerpostd is running",
            "peerpost daemon stop; peerpost daemon start",
        )
    else:
        _doctor_check(checks, "pid", "info", f"{paths.pid} is missing")

    self_test: dict[str, Any] | None = None
    if args.self_test:
        if not daemon_running:
            _doctor_check(checks, "self-test", "warn", "skipped because peerpostd is not running", "peerpost daemon start")
        else:
            try:
                self_test = client().request("self_test")
            except PeerpostClientError as exc:
                _doctor_check(checks, "self-test", "error", str(exc))
            else:
                check_status = "ok" if self_test.get("ok") else "error"
                detail = ", ".join(
                    f"{item['name']}={item['detail']}" for item in self_test.get("checks", [])
                )
                _doctor_check(checks, "self-test", check_status, detail or "completed")

    delivery_health: dict[str, Any] | None = None
    team_status: dict[str, Any] | None = None
    if args.team:
        if not daemon_running:
            _doctor_check(
                checks,
                "agents",
                "warn",
                f"skipped team {safe_field(args.team)} because peerpostd is not running",
                "peerpost daemon start",
            )
            _doctor_check(
                checks,
                "deliveries",
                "warn",
                f"skipped team {safe_field(args.team)} because peerpostd is not running",
                "peerpost daemon start",
            )
        else:
            try:
                team_status = client().request("team_status", team=args.team)
            except PeerpostClientError as exc:
                _doctor_check(checks, "agents", "error", str(exc))
            else:
                agent_count = len(team_status.get("agents", []))
                if agent_count:
                    _doctor_check(
                        checks,
                        "agents",
                        "ok",
                        f"team {safe_field(args.team)}: {agent_count} registered agent(s)",
                    )
                else:
                    _doctor_check(
                        checks,
                        "agents",
                        "warn",
                        f"team {safe_field(args.team)} has no registered agents",
                        f"peerpost setup --start-daemon --team {safe_field(args.team)}",
                    )
            try:
                delivery_health = client().request("delivery_health", team=args.team)
            except PeerpostClientError as exc:
                _doctor_check(checks, "deliveries", "error", str(exc))
            else:
                status_counts = delivery_health.get("status_counts", {})
                pending = int(status_counts.get("pending", 0))
                orphan_count = int(delivery_health.get("orphan_count", 0))
                sender_count = int(delivery_health.get("unregistered_sender_count", 0))
                invalid_priority_count = int(delivery_health.get("invalid_priority_count", 0))
                invalid_status_count = int(delivery_health.get("invalid_status_count", 0))
                without_delivery_count = int(
                    delivery_health.get("messages_without_delivery_count", 0)
                )
                invalid_metadata_count = int(delivery_health.get("invalid_metadata_count", 0))
                detail = (
                    f"team {safe_field(args.team)}: {pending} pending, "
                    f"{orphan_count} unregistered recipient(s), "
                    f"{sender_count} unregistered sender(s), "
                    f"{invalid_priority_count} invalid priority value(s), "
                    f"{invalid_status_count} invalid delivery status value(s), "
                    f"{without_delivery_count} message(s) without delivery rows, "
                    f"{invalid_metadata_count} invalid metadata value(s)"
                )
                if (
                    orphan_count
                    or sender_count
                    or invalid_priority_count
                    or invalid_status_count
                    or without_delivery_count
                    or invalid_metadata_count
                ):
                    _doctor_check(
                        checks,
                        "deliveries",
                        "warn",
                        detail,
                        "check agent ids with: peerpost agents --team "
                        f"{safe_field(args.team)}",
                    )
                else:
                    _doctor_check(checks, "deliveries", "ok", detail)

    if unix_socket_path_too_long(paths.socket):
        _doctor_check(
            checks,
            "socket-path",
            "warn",
            unix_socket_path_length_message(paths.socket),
            "export PEERPOST_SOCKET=/tmp/peerpost-$(id -u).sock",
        )

    socket_reported = False
    if args.fix and paths.socket.exists() and not daemon_running:
        try:
            paths.socket.unlink()
        except OSError as exc:
            socket_reported = True
            _doctor_check(
                checks,
                "socket",
                "warn",
                f"{paths.socket} exists but could not be removed: {exc}",
                f"rm -f {paths.socket}; peerpost daemon start",
            )
        else:
            repairs.append(f"removed stale socket {paths.socket}")

    if args.fix and paths.socket.exists() and daemon_running and _mode_int(paths.socket) != 0o600:
        restrict_file(paths.socket)
        repairs.append(f"chmod 600 {paths.socket}")

    if not socket_reported:
        if paths.socket.exists() and daemon_running:
            socket_mode = _mode_int(paths.socket)
            if socket_mode == 0o600:
                _doctor_check(checks, "socket", "ok", f"{paths.socket} mode {_mode(paths.socket)}")
            else:
                _doctor_check(
                    checks,
                    "socket",
                    "warn",
                    f"{paths.socket} mode {_mode(paths.socket)}; expected 0o600",
                    f"chmod 600 {paths.socket}",
                )
        elif paths.socket.exists():
            _doctor_check(checks, "socket", "warn", f"{paths.socket} exists but daemon is not responding", f"rm -f {paths.socket}; peerpost daemon start")
        else:
            _doctor_check(checks, "socket", "info", str(paths.socket), "peerpost daemon start")

    peerpost_bin = shutil.which("peerpost")
    if peerpost_bin:
        _doctor_check(checks, "path", "ok", f"peerpost found at {peerpost_bin}")
    else:
        _doctor_check(checks, "path", "info", "peerpost command was not found on PATH", "python -m pip install -e .")

    if paths.log.exists():
        log_mode = _mode_int(paths.log)
        if log_mode == 0o600:
            _doctor_check(checks, "log", "ok", f"{paths.log} mode {_mode(paths.log)}")
        else:
            _doctor_check(
                checks,
                "log",
                "warn",
                f"{paths.log} mode {_mode(paths.log)}; expected 0o600",
                f"chmod 600 {paths.log}",
            )
    else:
        _doctor_check(checks, "log", "info", f"{paths.log} does not exist yet")
    _doctor_autostart_check(checks)

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
        "version": {
            "cli": __version__,
            "daemon": daemon_version,
        },
        "delivery_health": delivery_health,
        "team_status": team_status,
        "repairs": repairs,
        "self_test": self_test,
        "checks": checks,
    }

    if args.output_format == "json":
        print(format_json(report))
    else:
        for check in checks:
            print(f"{check['name']}: {check['status']}: {check['detail']}")
            if check.get("fix"):
                print(f"  fix: {check['fix']}")
        if args.fix:
            print("repairs:")
            if repairs:
                for repair in repairs:
                    print(f"  {repair}")
            else:
                print("  none")
        print(f"status: {status}")
    if has_errors or (args.strict and has_warnings):
        return 1
    return 0


def snippet_for(adapter: str, agent: str, team: str) -> str:
    agent_arg = shlex.quote(safe_field(agent))
    team_arg = shlex.quote(safe_field(team))
    if adapter == "claude-code":
        return (
            "# Claude Code Monitor\n"
            f"peerpost subscribe --agent {agent_arg} --team {team_arg} --include-backlog --format monitor"
        )
    if adapter == "codex":
        return (
            "# Codex Stop hook command\n"
            f"peerpost drain --agent {agent_arg} --team {team_arg} --format codex-hook"
        )
    if adapter == "copilot":
        return (
            "# Copilot agentStop hook command\n"
            f"peerpost drain --agent {agent_arg} --team {team_arg} --format copilot-hook"
        )
    if adapter == "antigravity":
        return (
            "# Antigravity/local generic receive command\n"
            f"peerpost drain --agent {agent_arg} --team {team_arg} --format plain\n"
            f"# Or keep a live local monitor open:\n"
            f"peerpost subscribe --agent {agent_arg} --team {team_arg} --format monitor"
        )
    return (
        "# Generic local agent receive command\n"
        f"peerpost drain --agent {agent_arg} --team {team_arg} --format plain\n"
        f"# Or keep a live local monitor open:\n"
        f"peerpost subscribe --agent {agent_arg} --team {team_arg} --format monitor"
    )


def resolve_daemon_autostart_target(target: str) -> str:
    if target != "auto":
        return target
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    raise ValueError(
        "cannot detect daemon autostart target on this OS; pass --target launchd or --target systemd"
    )


def daemon_autostart_path(target: str) -> Path:
    if target == "launchd":
        return Path.home() / "Library" / "LaunchAgents" / "local.peerpost.peerpostd.plist"
    if target == "systemd":
        return Path.home() / ".config" / "systemd" / "user" / "peerpostd.service"
    raise ValueError(f"unknown daemon autostart target: {target}")


def daemon_autostart_enable_command(target: str, path: Path) -> str:
    quoted_path = shlex.quote(str(path))
    if target == "launchd":
        return f"launchctl bootstrap gui/$(id -u) {quoted_path}"
    if target == "systemd":
        return "systemctl --user enable --now peerpostd.service"
    raise ValueError(f"unknown daemon autostart target: {target}")


def daemon_autostart_disable_command(target: str, path: Path) -> str:
    if target == "launchd":
        return "launchctl bootout gui/$(id -u)/local.peerpost.peerpostd"
    if target == "systemd":
        return "systemctl --user disable --now peerpostd.service"
    raise ValueError(f"unknown daemon autostart target: {target}")


def daemon_config_for(target: str) -> str:
    paths = ensure_home(get_paths())
    python = str(Path(sys.executable).resolve())
    if target == "launchd":
        return (
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
            "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
            "<plist version=\"1.0\">\n"
            "<dict>\n"
            "  <key>Label</key><string>local.peerpost.peerpostd</string>\n"
            "  <key>ProgramArguments</key>\n"
            "  <array>\n"
            f"    <string>{html.escape(python)}</string>\n"
            "    <string>-m</string>\n"
            "    <string>peerpost.daemon</string>\n"
            "    <string>--foreground</string>\n"
            "  </array>\n"
            "  <key>EnvironmentVariables</key>\n"
            "  <dict>\n"
            f"    <key>PEERPOST_HOME</key><string>{html.escape(str(paths.home))}</string>\n"
            f"    <key>PEERPOST_SOCKET</key><string>{html.escape(str(paths.socket))}</string>\n"
            "  </dict>\n"
            "  <key>RunAtLoad</key><true/>\n"
            "  <key>KeepAlive</key><true/>\n"
            f"  <key>StandardOutPath</key><string>{html.escape(str(paths.log))}</string>\n"
            f"  <key>StandardErrorPath</key><string>{html.escape(str(paths.log))}</string>\n"
            "</dict>\n"
            "</plist>\n"
        )
    if target == "systemd":
        return (
            "[Unit]\n"
            "Description=peerpost local peer-agent message bus\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"Environment=PEERPOST_HOME={shlex.quote(str(paths.home))}\n"
            f"Environment=PEERPOST_SOCKET={shlex.quote(str(paths.socket))}\n"
            f"ExecStart={shlex.quote(python)} -m peerpost.daemon --foreground\n"
            "Restart=on-failure\n"
            "RestartSec=2\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
    raise ValueError(f"unknown daemon autostart target: {target}")


def daemon_snippet_for(target: str) -> str:
    if target == "launchd":
        return (
            "# macOS launchd user agent plist\n"
            f"# Save as: {daemon_autostart_path(target)}\n"
            f"# Load with: {daemon_autostart_enable_command(target, daemon_autostart_path(target))}\n"
            f"# Unload with: {daemon_autostart_disable_command(target, daemon_autostart_path(target))}\n"
            f"{daemon_config_for(target).rstrip()}"
        )
    if target == "systemd":
        return (
            "# Linux systemd user unit\n"
            f"# Save as: {daemon_autostart_path(target)}\n"
            f"# Enable with: {daemon_autostart_enable_command(target, daemon_autostart_path(target))}\n"
            f"# Stop with: {daemon_autostart_disable_command(target, daemon_autostart_path(target))}\n"
            f"{daemon_config_for(target).rstrip()}"
        )
    raise ValueError(f"unknown daemon snippet: {target}")


def command_install_snippets(args: argparse.Namespace) -> int:
    adapters = SNIPPET_ADAPTERS if args.adapter == "all" else (args.adapter,)
    blocks: list[str] = []
    for adapter in adapters:
        if adapter in DAEMON_SNIPPETS:
            blocks.append(daemon_snippet_for(adapter))
        else:
            agent = agent_id_for_adapter(adapter, args.agent)
            blocks.append(snippet_for(adapter, agent, args.team))
    print("\n\n".join(blocks))
    return 0


def agent_id_for_adapter(adapter: str, override: str | None = None) -> str:
    if override:
        return override
    return "claude" if adapter == "claude-code" else adapter


def snippet_items(adapters: tuple[str, ...], team: str, agent_override: str | None = None) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for adapter in adapters:
        agent = agent_id_for_adapter(adapter, agent_override)
        items.append({"adapter": adapter, "agent": agent, "snippet": snippet_for(adapter, agent, team)})
    return items


def _setup_registration_specs(args: argparse.Namespace) -> list[tuple[str, str]]:
    specs: list[tuple[str, str]] = list(DEFAULT_SETUP_AGENTS if args.register_default_agents else ())
    for item in args.register or []:
        if ":" in item:
            agent, agent_type = item.split(":", 1)
        else:
            agent = item
            agent_type = AGENT_TYPE_ALIASES.get(agent, "generic")
        agent = agent.strip()
        agent_type = agent_type.strip()
        if not agent or not agent_type:
            raise ValueError(f"invalid registration spec: {item!r}; use AGENT[:TYPE]")
        specs.append((agent, agent_type))

    deduped: dict[str, str] = {}
    for agent, agent_type in specs:
        deduped[agent] = agent_type
    return sorted(deduped.items())


def command_quickstart(args: argparse.Namespace) -> int:
    paths = ensure_home(get_paths())
    daemon = start_daemon_background()
    if daemon is None:
        eprint("failed to start peerpostd")
        return 1

    registered_agents: list[dict[str, Any]] = []
    for agent, agent_type in DEFAULT_SETUP_AGENTS:
        registered_agents.append(
            client().request(
                "join",
                agent=agent,
                agent_type=agent_type,
                team=args.team,
                workspace=None,
            )
        )

    try:
        self_test = client().request("self_test")
    except PeerpostClientError as exc:
        self_test = {"ok": False, "error": str(exc)}

    snippets = snippet_items(QUICKSTART_ADAPTERS, args.team)
    data = {
        "home": str(paths.home),
        "db": str(paths.db),
        "socket": str(paths.socket),
        "pid": str(paths.pid),
        "log": str(paths.log),
        "daemon": daemon,
        "team": args.team,
        "registered_agents": registered_agents,
        "self_test": self_test,
        "snippets": snippets,
        "shell_defaults": [
            f"export PEERPOST_TEAM={shlex.quote(safe_field(args.team))}",
            "export PEERPOST_AGENT=codex",
        ],
        "try_commands": [
            f"peerpost send --from claude --to codex --team {shlex.quote(safe_field(args.team))} "
            "\"Please review the auth middleware.\"",
            f"peerpost drain --agent codex --team {shlex.quote(safe_field(args.team))}",
        ],
    }
    if args.output_format == "json":
        print(format_json(data))
        return 0 if self_test.get("ok") else 1

    print("peerpost quickstart")
    print(f"daemon: running pid {safe_field(daemon.get('pid'))}")
    print(f"team: {safe_field(args.team)}")
    print(f"home: {paths.home}")
    print()
    print("registered agents:")
    for agent in registered_agents:
        print(
            f"  {safe_field(agent['team'])}/{safe_field(agent['id'])} "
            f"{safe_field(agent['agent_type'])}"
        )
    print()
    print(f"self-test: {'ok' if self_test.get('ok') else 'failed'}")
    if not self_test.get("ok") and self_test.get("error"):
        print(f"  {safe_field(self_test['error'])}")
    print()
    print("optional shell defaults:")
    for command in data["shell_defaults"]:
        print(f"  {command}")
    print()
    print("configure receiving:")
    for item in snippets:
        print()
        print(item["snippet"])
    print()
    print("try it:")
    for command in data["try_commands"]:
        print(f"  {command}")
    print()
    print(f"check later: peerpost doctor --team {shlex.quote(safe_field(args.team))} --self-test")
    return 0 if self_test.get("ok") else 1


def command_setup(args: argparse.Namespace) -> int:
    paths = ensure_home(get_paths())
    try:
        registrations = _setup_registration_specs(args)
    except ValueError as exc:
        eprint(str(exc))
        return 2
    daemon = start_daemon_background() if args.start_daemon else daemon_ping()
    if registrations and daemon is None:
        eprint("agent registration requires peerpostd. Run: peerpost setup --start-daemon")
        return 1

    registered_agents: list[dict[str, Any]] = []
    for agent, agent_type in registrations:
        if agent_type not in KNOWN_AGENT_TYPES:
            eprint(f"warning: unknown agent type '{safe_field(agent_type)}', allowing it")
        registered_agents.append(
            client().request(
                "join",
                agent=agent,
                agent_type=agent_type,
                team=args.team,
                workspace=None,
            )
        )

    adapters = SNIPPET_ADAPTERS if args.adapter == "all" else (args.adapter,)
    snippets = snippet_items(tuple(adapters), args.team, args.agent)

    data = {
        "home": str(paths.home),
        "db": str(paths.db),
        "socket": str(paths.socket),
        "pid": str(paths.pid),
        "log": str(paths.log),
        "daemon": daemon or {"status": "not_running"},
        "team": args.team,
        "registered_agents": registered_agents,
        "snippets": snippets,
        "daemon_snippet": daemon_snippet_for(args.daemon_snippet) if args.daemon_snippet else None,
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
    if registered_agents:
        print()
        print("registered agents:")
        for agent in registered_agents:
            print(
                f"{safe_field(agent['team'])}/{safe_field(agent['id'])} "
                f"{safe_field(agent['agent_type'])}"
            )
    if snippets:
        print()
        print("agent snippets:")
        for item in snippets:
            print()
            print(item["snippet"])
    if args.daemon_snippet:
        print()
        print("daemon autostart snippet:")
        print()
        print(daemon_snippet_for(args.daemon_snippet))
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
    install_autostart = daemon_sub.add_parser("install-autostart")
    install_autostart.add_argument("--target", choices=DAEMON_AUTOSTART_TARGETS, default="auto")
    install_autostart.add_argument("--overwrite", action="store_true")
    uninstall_autostart = daemon_sub.add_parser("uninstall-autostart")
    uninstall_autostart.add_argument("--target", choices=DAEMON_AUTOSTART_TARGETS, default="auto")
    daemon.set_defaults(func=command_daemon)

    join = sub.add_parser("join")
    add_agent_arg(join, required=True)
    join.add_argument("--type", dest="agent_type", required=True)
    add_team_arg(join, required=True)
    join.add_argument("--workspace")
    join.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    join.set_defaults(func=command_join)

    agents = sub.add_parser("agents")
    add_team_arg(agents)
    agents.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    agents.set_defaults(func=command_agents)

    status = sub.add_parser("status")
    add_team_arg(status, required=True)
    status.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    status.set_defaults(func=command_status)

    leave = sub.add_parser("leave")
    add_agent_arg(leave, required=True)
    add_team_arg(leave, required=True)
    leave.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    leave.set_defaults(func=command_leave)

    send = sub.add_parser("send")
    add_agent_arg(send, dest="from_agent", required=True)
    target = send.add_mutually_exclusive_group(required=True)
    target.add_argument("--to", dest="to_agent")
    target.add_argument("--broadcast", action="store_true")
    add_team_arg(send, required=True)
    send.add_argument("--kind", default="message")
    send.add_argument(
        "--priority",
        choices=["low", "normal", "high", "urgent"],
        default="normal",
    )
    send.add_argument("--reply-to", help="parent message id this message replies to")
    send.add_argument("--stdin", action="store_true", help="read message body from stdin")
    send.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    send.add_argument("message", nargs="?")
    send.set_defaults(func=command_send)

    reply = sub.add_parser("reply")
    reply.add_argument("message_id")
    add_agent_arg(reply, dest="from_agent", required=True)
    add_team_arg(reply, required=True)
    reply.add_argument("--kind", default="reply")
    reply.add_argument(
        "--priority",
        choices=["low", "normal", "high", "urgent"],
        default="normal",
    )
    reply.add_argument("--stdin", action="store_true", help="read message body from stdin")
    reply.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    reply.add_argument("message", nargs="?")
    reply.set_defaults(func=command_reply)

    inbox = sub.add_parser("inbox")
    add_agent_arg(inbox, required=True)
    add_team_arg(inbox, required=True)
    inbox.add_argument("--all", dest="include_all", action="store_true")
    inbox.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    inbox.set_defaults(func=command_inbox)

    read = sub.add_parser("read")
    read.add_argument("message_id")
    add_agent_arg(read, required=True)
    add_team_arg(read, required=True)
    read.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    read.set_defaults(func=command_read)

    ack = sub.add_parser("ack")
    ack.add_argument("message_ids", nargs="+")
    add_agent_arg(ack, required=True)
    add_team_arg(ack, required=True)
    ack.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    ack.set_defaults(func=command_ack)

    done = sub.add_parser("done")
    done.add_argument("message_ids", nargs="+")
    add_agent_arg(done, required=True)
    add_team_arg(done, required=True)
    done.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    done.set_defaults(func=command_done)

    drain = sub.add_parser("drain")
    add_agent_arg(drain, required=True)
    add_team_arg(drain, required=True)
    drain.add_argument("--limit", type=int, default=20)
    drain.add_argument(
        "--format",
        dest="output_format",
        choices=["plain", "json", "codex-hook", "copilot-hook"],
        default="plain",
    )
    drain.set_defaults(func=command_drain)

    subscribe = sub.add_parser("subscribe")
    add_agent_arg(subscribe, required=True)
    add_team_arg(subscribe, required=True)
    subscribe.add_argument("--include-backlog", action="store_true")
    subscribe.add_argument(
        "--format",
        dest="output_format",
        choices=["plain", "json", "monitor"],
        default="plain",
    )
    subscribe.set_defaults(func=command_subscribe)

    history = sub.add_parser("history")
    add_team_arg(history, required=True)
    add_agent_arg(history)
    history.add_argument("--with", dest="with_agent")
    history.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    history.set_defaults(func=command_history)

    thread = sub.add_parser("thread")
    thread.add_argument("message_id")
    add_team_arg(thread, required=True)
    thread.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    thread.set_defaults(func=command_thread)

    prune = sub.add_parser("prune")
    add_team_arg(prune)
    prune.add_argument("--older-than-days", type=int, default=30)
    prune.add_argument("--before", help="UTC ISO timestamp cutoff, e.g. 2026-06-01T00:00:00Z")
    prune.add_argument("--limit", type=int, default=100)
    prune.add_argument("--apply", action="store_true", help="delete matched messages")
    prune.add_argument("--backup-first", action="store_true", help="create and verify a backup before applying deletion")
    prune.add_argument("--backup-output", help="backup path to use with --backup-first")
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
    add_team_arg(doctor, help="also check delivery health for one team")
    doctor.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    doctor.add_argument("--strict", action="store_true", help="return nonzero when warnings are present")
    doctor.add_argument(
        "--fix",
        action="store_true",
        help="repair safe local filesystem issues such as permissions and stale pid/socket files",
    )
    doctor.add_argument(
        "--self-test",
        action="store_true",
        help="exercise daemon, SQLite storage, pending delivery, and delivered status checks",
    )
    doctor.set_defaults(func=command_doctor)

    snippets = sub.add_parser("install-snippets", aliases=["snippets"])
    snippets.add_argument(
        "--adapter",
        choices=[*SNIPPET_ADAPTERS, *DAEMON_SNIPPETS, "all"],
        default="all",
        help="agent adapter or daemon autostart snippet to print",
    )
    add_team_arg(snippets, default="dev")
    snippets.add_argument("--agent", help="override the agent id used in the snippet")
    snippets.set_defaults(func=command_install_snippets)

    quickstart = sub.add_parser("quickstart")
    add_team_arg(quickstart, default="dev")
    quickstart.add_argument("--format", dest="output_format", choices=["plain", "json"], default="plain")
    quickstart.set_defaults(func=command_quickstart)

    setup = sub.add_parser("setup")
    add_team_arg(setup, default="dev")
    setup.add_argument(
        "--adapter",
        choices=[*SNIPPET_ADAPTERS, "all"],
        default="all",
        help="agent adapter snippets to print",
    )
    setup.add_argument("--agent", help="override the agent id used in snippets")
    setup.add_argument("--start-daemon", action="store_true")
    setup.add_argument(
        "--daemon-snippet",
        choices=DAEMON_SNIPPETS,
        help="also print a daemon autostart snippet for this OS service manager",
    )
    setup.add_argument(
        "--register-default-agents",
        action="store_true",
        help="register claude, codex, and copilot in the selected team",
    )
    setup.add_argument(
        "--register",
        action="append",
        help="register one agent as AGENT[:TYPE]; repeat for multiple agents",
    )
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
    except ValueError as exc:
        eprint(str(exc))
        return 2
    except PeerpostClientError as exc:
        eprint(str(exc))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
