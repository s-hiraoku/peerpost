"""Filesystem paths for peerpost."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


APP_DIR = "peerpost"


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
