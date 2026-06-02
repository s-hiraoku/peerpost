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

from .db import Store
from .paths import PeerpostPaths, ensure_home, get_paths, restrict_file
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

    def _require_body(self, request: dict[str, Any]) -> str:
        body = self._require(request, "body")
        if not isinstance(body, str):
            raise RequestError("bad_request", "body must be a string")
        if len(body) > MAX_BODY_CHARS:
            raise RequestError("request_too_large", f"body exceeds {MAX_BODY_CHARS} characters")
        return body

    def _dispatch(self, request: dict[str, Any]) -> Any:
        request_type = request.get("type")
        store = self.server.store
        if request_type == "ping":
            return {"status": "ok", "pid": os.getpid()}
        if request_type == "shutdown":
            threading.Thread(target=self._shutdown_later, daemon=True).start()
            return {"status": "stopping", "pid": os.getpid()}
        if request_type == "join":
            return store.join_agent(
                self._require(request, "agent"),
                self._require(request, "agent_type"),
                self._require(request, "team"),
                request.get("workspace"),
            )
        if request_type == "leave":
            removed = store.leave_agent(
                self._require(request, "agent"),
                self._require(request, "team"),
            )
            return {"removed": removed}
        if request_type == "agents":
            return store.list_agents(request.get("team"))
        if request_type == "send":
            return self._send(request)
        if request_type == "reply":
            return self._reply(request)
        if request_type == "inbox":
            messages = store.inbox(
                self._require(request, "agent"),
                self._require(request, "team"),
                bool(request.get("include_all", False)),
            )
            return [message.as_dict() for message in messages]
        if request_type == "read":
            message = store.read_message(
                self._require(request, "message_id"),
                self._require(request, "agent"),
                self._require(request, "team"),
            )
            if message is None:
                raise RequestError("not_found", "message not found for this agent/team")
            return message.as_dict()
        if request_type == "ack":
            return {
                "updated": store.ack(
                    self._require(request, "message_ids"),
                    self._require(request, "agent"),
                    self._require(request, "team"),
                )
            }
        if request_type == "done":
            return {
                "updated": store.done(
                    self._require(request, "message_ids"),
                    self._require(request, "agent"),
                    self._require(request, "team"),
                )
            }
        if request_type == "drain":
            messages = store.drain(
                self._require(request, "agent"),
                self._require(request, "team"),
                int(request.get("limit", 20)),
            )
            return [message.as_dict() for message in messages]
        if request_type == "history":
            messages = store.history(request["team"], request.get("agent"), request.get("with_agent"))
            return [message.as_dict() for message in messages]
        if request_type == "thread":
            messages = store.thread(
                self._require(request, "team"),
                self._require(request, "message_id"),
            )
            if not messages:
                raise RequestError("not_found", "message thread not found for this team")
            return [message.as_dict() for message in messages]
        if request_type == "prune":
            return store.prune_done(
                self._require(request, "before"),
                request.get("team"),
                int(request.get("limit", 100)),
                bool(request.get("apply", False)),
            )
        raise RequestError("unknown_request", f"unknown request type: {request_type}")

    def _send(self, request: dict[str, Any]) -> dict[str, Any]:
        store = self.server.store
        team = self._require(request, "team")
        from_agent = self._require(request, "from_agent")
        body = self._require_body(request)
        if request.get("broadcast"):
            targets = store.broadcast_targets(team, from_agent)
        else:
            targets = [self._require(request, "to_agent")]
        message, targets = store.create_message(
            team,
            from_agent,
            body,
            targets,
            kind=request.get("kind", "message"),
            priority=request.get("priority", "normal"),
            parent_id=request.get("parent_id"),
        )
        return self._deliver_to_live_targets(message, targets, team)

    def _reply(self, request: dict[str, Any]) -> dict[str, Any]:
        store = self.server.store
        team = self._require(request, "team")
        from_agent = self._require(request, "from_agent")
        parent_id = self._require(request, "message_id")
        body = self._require_body(request)
        parent = store.get_message_for_agent(parent_id, from_agent, team)
        if parent is None:
            raise RequestError("not_found", "message not found for this agent/team")
        message, targets = store.create_message(
            team,
            from_agent,
            body,
            [parent.from_agent],
            kind=request.get("kind", "reply"),
            priority=request.get("priority", "normal"),
            parent_id=parent_id,
        )
        return self._deliver_to_live_targets(message, targets, team)

    def _deliver_to_live_targets(
        self, message: dict[str, Any], targets: list[str], team: str
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
        return {"message": message, "targets": targets, "delivered_now": delivered_now}

    def _handle_subscribe(self, request: dict[str, Any]) -> None:
        request_id = request.get("id")
        team = self._require(request, "team")
        agent = self._require(request, "agent")
        self.wfile.write(encode_json_line(ok_response(request_id, {"status": "subscribed"})))
        self.wfile.flush()
        subscriber = Subscriber(team=team, agent=agent, sock=self.request, lock=threading.Lock())
        self.server.register_subscriber(subscriber)
        self.server.logger.info("subscriber connected team=%s agent=%s", team, agent)
        try:
            if request.get("include_backlog"):
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
