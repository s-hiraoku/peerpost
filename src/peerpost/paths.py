"""Filesystem paths for peerpost."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


APP_DIR = "peerpost"
MAX_UNIX_SOCKET_PATH_BYTES = 100


@dataclass(frozen=True)
class PeerpostPaths:
    home: Path
    db: Path
    pid: Path
    log: Path
    socket: Path


def default_home() -> Path:
    configured = os.environ.get("PEERPOST_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / APP_DIR


def default_socket() -> Path:
    configured = os.environ.get("PEERPOST_SOCKET")
    if configured:
        return Path(configured).expanduser()
    return Path(f"/tmp/peerpost-{os.getuid()}.sock")


def unix_socket_path_bytes(path: Path) -> int:
    return len(os.fsencode(str(path)))


def unix_socket_path_too_long(path: Path) -> bool:
    return unix_socket_path_bytes(path) >= MAX_UNIX_SOCKET_PATH_BYTES


def unix_socket_path_length_message(path: Path) -> str:
    return (
        f"{path} is {unix_socket_path_bytes(path)} bytes; "
        f"keep PEERPOST_SOCKET below {MAX_UNIX_SOCKET_PATH_BYTES} bytes"
    )


def get_paths() -> PeerpostPaths:
    home = default_home()
    return PeerpostPaths(
        home=home,
        db=home / "peerpost.sqlite",
        pid=home / "peerpost.pid",
        log=home / "peerpost.log",
        socket=default_socket(),
    )


def ensure_home(paths: PeerpostPaths | None = None) -> PeerpostPaths:
    paths = paths or get_paths()
    paths.home.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        paths.home.chmod(0o700)
    except OSError:
        pass
    return paths


def restrict_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def sqlite_sidecar_paths(db_path: Path) -> tuple[Path, Path]:
    return (
        db_path.with_name(f"{db_path.name}-wal"),
        db_path.with_name(f"{db_path.name}-shm"),
    )


def restrict_sqlite_files(db_path: Path) -> None:
    restrict_file(db_path)
    for sidecar in sqlite_sidecar_paths(db_path):
        if sidecar.exists():
            restrict_file(sidecar)
