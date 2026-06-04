"""peerpostd Unix-domain socket daemon."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import socketserver
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .db import ALLOWED_PRIORITIES, Store
from .paths import (
    PeerpostPaths,
    ensure_home,
    get_paths,
    restrict_file,
    unix_socket_path_length_message,
    unix_socket_path_too_long,
)
from .protocol import (
    MAX_BODY_CHARS,
    MAX_JSON_LINE_BYTES,
    ProtocolError,
    decode_json_line,
    encode_json_line,
    error_response,
    ok_response,
)


LOGGER_NAME = "peerpostd"


def configure_logging(paths: PeerpostPaths) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(paths.log, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    restrict_file(paths.log)
    return logger


class RequestError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Subscriber:
    team: str
    agent: str
    sock: socket.socket
    lock: threading.Lock

    def send(self, message: dict[str, Any]) -> bool:
        event = {"event": "message", "message": message}
        try:
            with self.lock:
                self.sock.sendall(encode_json_line(event))
            return True
        except OSError:
            return False


class PeerpostUnixServer(socketserver.ThreadingUnixStreamServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, paths: PeerpostPaths):
        self.paths = paths
        ensure_home(paths)
        self.logger = logging.getLogger(LOGGER_NAME)
        self._prepare_socket(paths.socket)
        self.store = Store()
        self.subscribers: dict[tuple[str, str], list[Subscriber]] = {}
        self.subscribers_lock = threading.RLock()
        self.shutdown_event = threading.Event()
        super().__init__(str(paths.socket), PeerpostRequestHandler)
        try:
            paths.socket.chmod(0o600)
        except OSError:
            pass
        self.logger.info("peerpostd listening socket=%s db=%s", paths.socket, paths.db)

    def _prepare_socket(self, socket_path: Path) -> None:
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if not socket_path.exists():
            return
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.25)
            probe.connect(str(socket_path))
        except OSError:
            socket_path.unlink(missing_ok=True)
        else:
            raise RuntimeError(f"peerpostd already appears to be running at {socket_path}")
        finally:
            probe.close()

    def register_subscriber(self, subscriber: Subscriber) -> None:
        key = (subscriber.team, subscriber.agent)
        with self.subscribers_lock:
            self.subscribers.setdefault(key, []).append(subscriber)

    def unregister_subscriber(self, subscriber: Subscriber) -> None:
        key = (subscriber.team, subscriber.agent)
        with self.subscribers_lock:
            current = self.subscribers.get(key, [])
            remaining = [item for item in current if item is not subscriber]
            if remaining:
                self.subscribers[key] = remaining
            else:
                self.subscribers.pop(key, None)

    def push_to_subscribers(self, team: str, agent: str, message: dict[str, Any]) -> bool:
        key = (team, agent)
        delivered = False
        stale: list[Subscriber] = []
        with self.subscribers_lock:
            subscribers = list(self.subscribers.get(key, []))
        for subscriber in subscribers:
            if subscriber.send(message):
                delivered = True
            else:
                stale.append(subscriber)
        for subscriber in stale:
            self.unregister_subscriber(subscriber)
        return delivered

    def cleanup(self) -> None:
        self.logger.info("peerpostd cleanup socket=%s pid=%s", self.paths.socket, self.paths.pid)
        self.store.close()
        self.paths.socket.unlink(missing_ok=True)
        self.paths.pid.unlink(missing_ok=True)


class PeerpostRequestHandler(socketserver.StreamRequestHandler):
    server: PeerpostUnixServer

    def handle(self) -> None:
        request_id = None
        request_type = None
        try:
            line = self._read_request_line()
            if not line:
                return
            request = decode_json_line(line)
            request_id = request.get("id")
            request_type = request.get("type")
            if request_type == "subscribe":
                self._handle_subscribe(request)
                return
            data = self._dispatch(request)
            self.wfile.write(encode_json_line(ok_response(request_id, data)))
            self.wfile.flush()
            self.server.logger.info("request ok id=%s type=%s", request_id, request_type)
        except RequestError as exc:
            self.server.logger.warning(
                "request error id=%s type=%s code=%s message=%s",
                request_id,
                request_type,
                exc.code,
                exc,
            )
            self.wfile.write(encode_json_line(error_response(request_id, exc.code, str(exc))))
            self.wfile.flush()
        except ProtocolError as exc:
            self.server.logger.warning(
                "request protocol_error id=%s type=%s message=%s", request_id, request_type, exc
            )
            self.wfile.write(encode_json_line(error_response(request_id, "bad_request", str(exc))))
            self.wfile.flush()
        except Exception as exc:
            self.server.logger.exception(
                "request internal_error id=%s type=%s message=%s", request_id, request_type, exc
            )
            self.wfile.write(encode_json_line(error_response(request_id, "internal_error", str(exc))))
            self.wfile.flush()

    def _read_request_line(self) -> bytes:
        line = self.rfile.readline(MAX_JSON_LINE_BYTES + 1)
        if len(line) > MAX_JSON_LINE_BYTES:
            raise RequestError(
                "request_too_large",
                f"request JSON line exceeds {MAX_JSON_LINE_BYTES} bytes",
            )
        return line

    def _require(self, request: dict[str, Any], key: str) -> Any:
        value = request.get(key)
        if value in (None, ""):
            raise RequestError("bad_request", f"missing required field: {key}")
        return value

    def _require_text(self, request: dict[str, Any], key: str) -> str:
        value = self._require(request, key)
        if not isinstance(value, str):
            raise RequestError("bad_request", f"{key} must be a string")
        return value

    def _optional_text(self, request: dict[str, Any], key: str) -> str | None:
        value = request.get(key)
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            raise RequestError("bad_request", f"{key} must be a string")
        return value

    def _text_default(self, request: dict[str, Any], key: str, default: str) -> str:
        value = request.get(key, default)
        if value in (None, ""):
            return default
        if not isinstance(value, str):
            raise RequestError("bad_request", f"{key} must be a string")
        return value

    def _priority(self, request: dict[str, Any]) -> str:
        value = self._text_default(request, "priority", "normal")
        if value not in ALLOWED_PRIORITIES:
            allowed = ", ".join(sorted(ALLOWED_PRIORITIES))
            raise RequestError("bad_request", f"priority must be one of: {allowed}")
        return value

    def _require_text_list(self, request: dict[str, Any], key: str) -> list[str]:
        value = self._require(request, key)
        if not isinstance(value, list):
            raise RequestError("bad_request", f"{key} must be a list of strings")
        if not all(isinstance(item, str) and item for item in value):
            raise RequestError("bad_request", f"{key} must be a list of non-empty strings")
        return value

    def _bool(self, request: dict[str, Any], key: str, default: bool = False) -> bool:
        value = request.get(key, default)
        if not isinstance(value, bool):
            raise RequestError("bad_request", f"{key} must be a boolean")
        return value

    def _require_body(self, request: dict[str, Any]) -> str:
        body = self._require_text(request, "body")
        if len(body) > MAX_BODY_CHARS:
            raise RequestError("request_too_large", f"body exceeds {MAX_BODY_CHARS} characters")
        return body

    def _positive_int(self, request: dict[str, Any], key: str, default: int) -> int:
        raw_value = request.get(key, default)
        if isinstance(raw_value, bool):
            raise RequestError("bad_request", f"{key} must be an integer")
        try:
            value = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise RequestError("bad_request", f"{key} must be an integer") from exc
        if value < 1:
            raise RequestError("bad_request", f"{key} must be 1 or greater")
        return value

    def _dispatch(self, request: dict[str, Any]) -> Any:
        request_type = request.get("type")
        store = self.server.store
        if request_type == "ping":
            return {"status": "ok", "pid": os.getpid(), "version": __version__}
        if request_type == "shutdown":
            threading.Thread(target=self._shutdown_later, daemon=True).start()
            return {"status": "stopping", "pid": os.getpid()}
        if request_type == "join":
            return store.join_agent(
                self._require_text(request, "agent"),
                self._require_text(request, "agent_type"),
                self._require_text(request, "team"),
                self._optional_text(request, "workspace"),
            )
        if request_type == "leave":
            removed = store.leave_agent(
                self._require_text(request, "agent"),
                self._require_text(request, "team"),
            )
            return {"removed": removed}
        if request_type == "agents":
            return store.list_agents(self._optional_text(request, "team"))
        if request_type == "send":
            return self._send(request)
        if request_type == "reply":
            return self._reply(request)
        if request_type == "inbox":
            messages = store.inbox(
                self._require_text(request, "agent"),
                self._require_text(request, "team"),
                self._bool(request, "include_all"),
            )
            return [message.as_dict() for message in messages]
        if request_type == "read":
            team = self._require_text(request, "team")
            agent = self._require_text(request, "agent")
            message = store.read_message(
                self._resolve_message_id(team, self._require_text(request, "message_id"), agent),
                agent,
                team,
            )
            if message is None:
                raise RequestError("not_found", "message not found for this agent/team")
            return message.as_dict()
        if request_type == "ack":
            team = self._require_text(request, "team")
            agent = self._require_text(request, "agent")
            return {
                "updated": store.ack(
                    self._resolve_message_ids(
                        team,
                        self._require_text_list(request, "message_ids"),
                        agent,
                        require_match=True,
                    ),
                    agent,
                    team,
                )
            }
        if request_type == "done":
            team = self._require_text(request, "team")
            agent = self._require_text(request, "agent")
            return {
                "updated": store.done(
                    self._resolve_message_ids(
                        team,
                        self._require_text_list(request, "message_ids"),
                        agent,
                        require_match=True,
                    ),
                    agent,
                    team,
                )
            }
        if request_type == "drain":
            messages = store.drain(
                self._require_text(request, "agent"),
                self._require_text(request, "team"),
                self._positive_int(request, "limit", 20),
            )
            return [message.as_dict() for message in messages]
        if request_type == "history":
            messages = store.history(
                self._require_text(request, "team"),
                self._optional_text(request, "agent"),
                self._optional_text(request, "with_agent"),
            )
            return [message.as_dict() for message in messages]
        if request_type == "thread":
            team = self._require_text(request, "team")
            messages = store.thread(
                team,
                self._resolve_message_id(team, self._require_text(request, "message_id"), None),
            )
            if not messages:
                raise RequestError("not_found", "message thread not found for this team")
            return [message.as_dict() for message in messages]
        if request_type == "prune":
            return store.prune_done(
                self._require_text(request, "before"),
                self._optional_text(request, "team"),
                self._positive_int(request, "limit", 100),
                self._bool(request, "apply"),
            )
        if request_type == "backup":
            try:
                data = store.backup(
                    Path(self._require_text(request, "output")),
                    overwrite=self._bool(request, "overwrite"),
                )
            except FileExistsError as exc:
                raise RequestError(
                    "already_exists",
                    f"backup output already exists: {exc}; pass --overwrite to replace it",
                ) from exc
            except IsADirectoryError as exc:
                raise RequestError("bad_request", f"backup output is a directory: {exc}") from exc
            self.server.logger.info("database backup path=%s bytes=%s", data["path"], data["bytes"])
            return data
        if request_type == "self_test":
            data = store.self_test()
            self.server.logger.info("self-test ok=%s checks=%s", data["ok"], len(data["checks"]))
            return data
        if request_type == "delivery_health":
            return store.delivery_health(self._optional_text(request, "team"))
        if request_type == "team_status":
            return store.team_status(self._require_text(request, "team"))
        raise RequestError("unknown_request", f"unknown request type: {request_type}")

    def _resolve_message_id(
        self,
        team: str,
        message_id_or_prefix: str,
        agent: str | None = None,
        *,
        require_match: bool = False,
    ) -> str:
        matches = self.server.store.matching_message_ids(team, message_id_or_prefix, agent)
        if len(matches) > 1:
            raise RequestError(
                "ambiguous_message_id",
                f"message id prefix matches multiple messages: {message_id_or_prefix}",
            )
        if not matches and require_match:
            if agent is None:
                raise RequestError(
                    "not_found",
                    f"message id not found for this team: {message_id_or_prefix}",
                )
            raise RequestError(
                "not_found",
                f"message id not found for this agent/team: {message_id_or_prefix}",
            )
        return matches[0] if matches else message_id_or_prefix

    def _resolve_message_ids(
        self,
        team: str,
        message_ids_or_prefixes: list[str],
        agent: str | None = None,
        *,
        require_match: bool = False,
    ) -> list[str]:
        return [
            self._resolve_message_id(
                team,
                message_id_or_prefix,
                agent,
                require_match=require_match,
            )
            for message_id_or_prefix in message_ids_or_prefixes
        ]

    def _send(self, request: dict[str, Any]) -> dict[str, Any]:
        store = self.server.store
        team = self._require_text(request, "team")
        from_agent = self._require_text(request, "from_agent")
        body = self._require_body(request)
        unregistered_from_agent = store.get_agent(from_agent, team) is None
        if self._bool(request, "broadcast"):
            targets = store.broadcast_targets(team, from_agent)
            unregistered_targets: list[str] = []
            if not targets:
                raise RequestError(
                    "no_broadcast_targets",
                    f"no registered broadcast recipients in team {team}",
                )
        else:
            targets = [self._require_text(request, "to_agent")]
            if targets[0] == from_agent:
                raise RequestError("bad_request", "message must have at least one delivery target")
            unregistered_targets = [target for target in targets if store.get_agent(target, team) is None]
        message, targets = store.create_message(
            team,
            from_agent,
            body,
            targets,
            kind=self._text_default(request, "kind", "message"),
            priority=self._priority(request),
            parent_id=self._optional_text(request, "parent_id"),
        )
        return self._deliver_to_live_targets(
            message,
            targets,
            team,
            unregistered_targets,
            unregistered_from_agent,
        )

    def _reply(self, request: dict[str, Any]) -> dict[str, Any]:
        store = self.server.store
        team = self._require_text(request, "team")
        from_agent = self._require_text(request, "from_agent")
        parent_id = self._resolve_message_id(team, self._require_text(request, "message_id"), from_agent)
        body = self._require_body(request)
        unregistered_from_agent = store.get_agent(from_agent, team) is None
        parent = store.get_message_for_agent(parent_id, from_agent, team)
        if parent is None:
            raise RequestError("not_found", "message not found for this agent/team")
        targets = [parent.from_agent]
        if targets[0] == from_agent:
            raise RequestError("bad_request", "message must have at least one delivery target")
        unregistered_targets = [target for target in targets if store.get_agent(target, team) is None]
        message, targets = store.create_message(
            team,
            from_agent,
            body,
            targets,
            kind=self._text_default(request, "kind", "reply"),
            priority=self._priority(request),
            parent_id=parent_id,
        )
        return self._deliver_to_live_targets(
            message,
            targets,
            team,
            unregistered_targets,
            unregistered_from_agent,
        )

    def _deliver_to_live_targets(
        self,
        message: dict[str, Any],
        targets: list[str],
        team: str,
        unregistered_targets: list[str] | None = None,
        unregistered_from_agent: bool = False,
    ) -> dict[str, Any]:
        store = self.server.store
        delivered_now: list[str] = []
        for target in targets:
            payload = {**message, "to_agent": target, "status": "pending"}
            if self.server.push_to_subscribers(team, target, payload):
                store.mark_delivered([message["id"]], target, team)
                delivered_now.append(target)
        self.server.logger.info(
            "message stored id=%s team=%s from=%s targets=%s delivered_now=%s",
            message["id"],
            team,
            message["from_agent"],
            ",".join(targets),
            ",".join(delivered_now),
        )
        return {
            "message": message,
            "targets": targets,
            "delivered_now": delivered_now,
            "unregistered_targets": unregistered_targets or [],
            "unregistered_from_agent": unregistered_from_agent,
        }

    def _handle_subscribe(self, request: dict[str, Any]) -> None:
        request_id = request.get("id")
        team = self._require_text(request, "team")
        agent = self._require_text(request, "agent")
        include_backlog = self._bool(request, "include_backlog")
        self.wfile.write(encode_json_line(ok_response(request_id, {"status": "subscribed"})))
        self.wfile.flush()
        subscriber = Subscriber(team=team, agent=agent, sock=self.request, lock=threading.Lock())
        self.server.register_subscriber(subscriber)
        self.server.logger.info("subscriber connected team=%s agent=%s", team, agent)
        try:
            if include_backlog:
                for message in self.server.store.pending_messages(agent, team, 1000):
                    payload = message.as_dict()
                    if subscriber.send(payload):
                        self.server.store.mark_delivered([message.id], agent, team)
            self.request.settimeout(1.0)
            while not self.server.shutdown_event.is_set():
                try:
                    data = self.request.recv(1)
                except socket.timeout:
                    continue
                if not data:
                    break
        finally:
            self.server.unregister_subscriber(subscriber)
            self.server.logger.info("subscriber disconnected team=%s agent=%s", team, agent)

    def _shutdown_later(self) -> None:
        self.server.shutdown_event.set()
        self.server.shutdown()


def write_pid(paths: PeerpostPaths) -> None:
    paths.pid.write_text(str(os.getpid()), encoding="utf-8")
    restrict_file(paths.pid)


def serve_foreground(paths: PeerpostPaths | None = None) -> None:
    paths = ensure_home(paths or get_paths())
    if unix_socket_path_too_long(paths.socket):
        raise RuntimeError(f"socket path too long: {unix_socket_path_length_message(paths.socket)}")
    logger = configure_logging(paths)
    logger.info("peerpostd starting pid=%s", os.getpid())
    server = PeerpostUnixServer(paths)
    write_pid(paths)

    def _stop(_signum: int, _frame: Any) -> None:
        server.shutdown_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    old_term = signal.signal(signal.SIGTERM, _stop)
    old_int = signal.signal(signal.SIGINT, _stop)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        logger.info("peerpostd stopping pid=%s", os.getpid())
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        server.server_close()
        server.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="peerpostd")
    parser.add_argument("--foreground", action="store_true", help="run in the foreground")
    args = parser.parse_args(argv)
    if not args.foreground:
        print("peerpostd currently runs directly; use --foreground", file=sys.stderr)
        return 2
    try:
        serve_foreground()
    except Exception as exc:
        print(f"peerpostd: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
