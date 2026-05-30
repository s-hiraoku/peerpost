"""Newline-delimited JSON protocol helpers."""

from __future__ import annotations

import json
import secrets
from typing import Any, BinaryIO


class ProtocolError(ValueError):
    pass


def make_request_id() -> str:
    return f"req_{secrets.token_hex(8)}"


def encode_json_line(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def decode_json_line(line: bytes | str) -> dict[str, Any]:
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("JSON line must contain an object")
    return payload


def read_json_line(file: BinaryIO) -> dict[str, Any] | None:
    line = file.readline()
    if not line:
        return None
    return decode_json_line(line)


def write_json_line(file: BinaryIO, payload: dict[str, Any]) -> None:
    file.write(encode_json_line(payload))
    file.flush()


def ok_response(request_id: str | None, data: Any = None) -> dict[str, Any]:
    return {"id": request_id, "ok": True, "data": data}


def error_response(request_id: str | None, code: str, message: str) -> dict[str, Any]:
    return {"id": request_id, "ok": False, "error": {"code": code, "message": message}}
