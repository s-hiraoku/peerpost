"""Client for peerpostd."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .paths import get_paths, unix_socket_path_length_message, unix_socket_path_too_long
from .protocol import decode_json_line, encode_json_line, make_request_id


NOT_RUNNING = "peerpostd is not running. Start it with: peerpost daemon start"


class PeerpostClientError(RuntimeError):
    pass


class DaemonNotRunning(PeerpostClientError):
    pass


class SocketPathTooLong(PeerpostClientError):
    pass


class PeerpostClient:
    def __init__(self, socket_path: str | None = None, timeout: float = 5.0):
        self.socket_path = socket_path or str(get_paths().socket)
        self.timeout = timeout

    def _connect(self) -> socket.socket:
        socket_path = Path(self.socket_path)
        if unix_socket_path_too_long(socket_path):
            raise SocketPathTooLong(
                f"socket path too long: {unix_socket_path_length_message(socket_path)}; "
                "set PEERPOST_SOCKET to a shorter path such as /tmp/peerpost-$(id -u).sock"
            )
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as exc:
            sock.close()
            raise DaemonNotRunning(NOT_RUNNING) from exc
        return sock

    def request(self, request_type: str, **payload: Any) -> Any:
        request_id = make_request_id()
        request = {"id": request_id, "type": request_type, **payload}
        with self._connect() as sock:
            file = sock.makefile("rwb")
            file.write(encode_json_line(request))
            file.flush()
            line = file.readline()
            if not line:
                raise PeerpostClientError("peerpostd closed the connection without a response")
            response = decode_json_line(line)
        if not response.get("ok"):
            error = response.get("error") or {}
            raise PeerpostClientError(error.get("message", "peerpostd returned an error"))
        return response.get("data")

    def subscribe(
        self,
        agent: str,
        team: str,
        include_backlog: bool = False,
    ) -> Iterator[dict[str, Any]]:
        request_id = make_request_id()
        request = {
            "id": request_id,
            "type": "subscribe",
            "agent": agent,
            "team": team,
            "include_backlog": include_backlog,
        }
        sock = self._connect()
        file = sock.makefile("rwb")
        file.write(encode_json_line(request))
        file.flush()
        line = file.readline()
        if not line:
            sock.close()
            raise PeerpostClientError("peerpostd closed the connection without a response")
        response = decode_json_line(line)
        if not response.get("ok"):
            sock.close()
            error = response.get("error") or {}
            raise PeerpostClientError(error.get("message", "peerpostd returned an error"))
        try:
            while True:
                line = file.readline()
                if not line:
                    break
                yield decode_json_line(line)
        finally:
            sock.close()
