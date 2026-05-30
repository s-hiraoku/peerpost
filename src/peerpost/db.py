"""SQLite storage for peerpost."""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .paths import ensure_home, get_paths, restrict_file


SCHEMA_VERSION = "1"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def make_message_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"msg_{stamp}_{secrets.token_hex(3)}"


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


@dataclass(frozen=True)
class Message:
    id: str
    team: str
    from_agent: str
    to_agent: str
    body: str
    created_at: str
    status: str
    kind: str = "message"
    priority: str = "normal"
    parent_id: str | None = None
    metadata_json: str = "{}"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Message":
        return cls(**row_to_dict(row))

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "team": self.team,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "body": self.body,
            "created_at": self.created_at,
            "status": self.status,
            "kind": self.kind,
            "priority": self.priority,
            "parent_id": self.parent_id,
            "metadata": json.loads(self.metadata_json or "{}"),
        }


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    paths = ensure_home(get_paths())
    db_path = db_path or paths.db
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    restrict_file(db_path)
    migrate(conn)
    restrict_file(db_path)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if version is not None and version["value"] != SCHEMA_VERSION:
            raise RuntimeError(f"unsupported schema_version {version['value']}")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS agents (
              id TEXT NOT NULL,
              team TEXT NOT NULL,
              agent_type TEXT NOT NULL,
              workspace TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (team, id)
            );

            CREATE TABLE IF NOT EXISTS messages (
              id TEXT PRIMARY KEY,
              team TEXT NOT NULL,
              from_agent TEXT NOT NULL,
              body TEXT NOT NULL,
              kind TEXT NOT NULL DEFAULT 'message',
              priority TEXT NOT NULL DEFAULT 'normal',
              parent_id TEXT,
              created_at TEXT NOT NULL,
              metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS deliveries (
              message_id TEXT NOT NULL,
              team TEXT NOT NULL,
              to_agent TEXT NOT NULL,
              status TEXT NOT NULL,
              delivered_at TEXT,
              acknowledged_at TEXT,
              done_at TEXT,
              PRIMARY KEY (message_id, to_agent),
              FOREIGN KEY (message_id) REFERENCES messages(id)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_team_created_at
              ON messages(team, created_at);
            CREATE INDEX IF NOT EXISTS idx_deliveries_to_status
              ON deliveries(to_agent, status);
            CREATE INDEX IF NOT EXISTS idx_deliveries_team_to_status
              ON deliveries(team, to_agent, status);
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )


class Store:
    def __init__(self, conn: sqlite3.Connection | None = None):
        self.conn = conn or connect()

    def close(self) -> None:
        self.conn.close()

    def join_agent(
        self, agent_id: str, agent_type: str, team: str, workspace: str | None = None
    ) -> dict[str, Any]:
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO agents (id, team, agent_type, workspace, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(team, id) DO UPDATE SET
                  agent_type = excluded.agent_type,
                  workspace = excluded.workspace,
                  updated_at = excluded.updated_at
                """,
                (agent_id, team, agent_type, workspace, now, now),
            )
        return self.get_agent(agent_id, team) or {}

    def get_agent(self, agent_id: str, team: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM agents WHERE id = ? AND team = ?", (agent_id, team)
        ).fetchone()
        return row_to_dict(row) if row else None

    def list_agents(self, team: str | None = None) -> list[dict[str, Any]]:
        if team:
            rows = self.conn.execute(
                "SELECT * FROM agents WHERE team = ? ORDER BY id", (team,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM agents ORDER BY team, id").fetchall()
        return [row_to_dict(row) for row in rows]

    def create_message(
        self,
        team: str,
        from_agent: str,
        body: str,
        targets: Iterable[str],
        kind: str = "message",
        priority: str = "normal",
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        message_id = make_message_id()
        created_at = utc_now()
        unique_targets = sorted({target for target in targets if target and target != from_agent})
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO messages
                  (id, team, from_agent, body, kind, priority, parent_id, created_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    team,
                    from_agent,
                    body,
                    kind,
                    priority,
                    parent_id,
                    created_at,
                    json.dumps(metadata or {}, separators=(",", ":")),
                ),
            )
            for target in unique_targets:
                self.conn.execute(
                    """
                    INSERT INTO deliveries
                      (message_id, team, to_agent, status)
                    VALUES (?, ?, ?, 'pending')
                    """,
                    (message_id, team, target),
                )
        return (
            {
                "id": message_id,
                "team": team,
                "from_agent": from_agent,
                "body": body,
                "kind": kind,
                "priority": priority,
                "parent_id": parent_id,
                "metadata": metadata or {},
                "created_at": created_at,
            },
            unique_targets,
        )

    def broadcast_targets(self, team: str, from_agent: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT id FROM agents WHERE team = ? AND id != ? ORDER BY id", (team, from_agent)
        ).fetchall()
        return [row["id"] for row in rows]

    def _messages_query(self, where_sql: str, params: tuple[Any, ...]) -> list[Message]:
        rows = self.conn.execute(
            f"""
            SELECT
              m.id, m.team, m.from_agent, d.to_agent, m.body, m.created_at, d.status,
              m.kind, m.priority, m.parent_id, m.metadata_json
            FROM messages m
            JOIN deliveries d ON d.message_id = m.id
            WHERE {where_sql}
            ORDER BY m.created_at, m.id
            """,
            params,
        ).fetchall()
        return [Message.from_row(row) for row in rows]

    def pending_messages(self, agent_id: str, team: str, limit: int = 20) -> list[Message]:
        rows = self.conn.execute(
            """
            SELECT
              m.id, m.team, m.from_agent, d.to_agent, m.body, m.created_at, d.status,
              m.kind, m.priority, m.parent_id, m.metadata_json
            FROM messages m
            JOIN deliveries d ON d.message_id = m.id
            WHERE d.team = ? AND d.to_agent = ? AND d.status = 'pending'
            ORDER BY m.created_at, m.id
            LIMIT ?
            """,
            (team, agent_id, limit),
        ).fetchall()
        return [Message.from_row(row) for row in rows]

    def drain(self, agent_id: str, team: str, limit: int = 20) -> list[Message]:
        messages = self.pending_messages(agent_id, team, limit)
        self.mark_delivered([message.id for message in messages], agent_id, team)
        return messages

    def inbox(self, agent_id: str, team: str, include_all: bool = False) -> list[Message]:
        if include_all:
            return self._messages_query("d.team = ? AND d.to_agent = ?", (team, agent_id))
        return self._messages_query(
            "d.team = ? AND d.to_agent = ? AND d.status != 'done'", (team, agent_id)
        )

    def history(
        self, team: str, agent: str | None = None, other_agent: str | None = None
    ) -> list[Message]:
        clauses = ["m.team = ?"]
        params: list[Any] = [team]
        if agent and other_agent:
            clauses.append(
                "((m.from_agent = ? AND d.to_agent = ?) OR (m.from_agent = ? AND d.to_agent = ?))"
            )
            params.extend([agent, other_agent, other_agent, agent])
        elif agent:
            clauses.append("(m.from_agent = ? OR d.to_agent = ?)")
            params.extend([agent, agent])
        return self._messages_query(" AND ".join(clauses), tuple(params))

    def read_message(self, message_id: str, agent_id: str, team: str) -> Message | None:
        messages = self._messages_query(
            "m.id = ? AND d.team = ? AND d.to_agent = ?", (message_id, team, agent_id)
        )
        if not messages:
            return None
        self.mark_delivered([message_id], agent_id, team)
        return messages[0]

    def mark_delivered(self, message_ids: Iterable[str], agent_id: str, team: str) -> None:
        ids = list(message_ids)
        if not ids:
            return
        now = utc_now()
        with self.conn:
            self.conn.executemany(
                """
                UPDATE deliveries
                SET status = 'delivered', delivered_at = COALESCE(delivered_at, ?)
                WHERE message_id = ? AND to_agent = ? AND team = ? AND status = 'pending'
                """,
                [(now, message_id, agent_id, team) for message_id in ids],
            )

    def ack(self, message_ids: Iterable[str], agent_id: str, team: str) -> int:
        ids = list(message_ids)
        if not ids:
            return 0
        now = utc_now()
        with self.conn:
            before = self.conn.total_changes
            self.conn.executemany(
                """
                UPDATE deliveries
                SET status = 'acknowledged',
                    delivered_at = COALESCE(delivered_at, ?),
                    acknowledged_at = COALESCE(acknowledged_at, ?)
                WHERE message_id = ? AND to_agent = ? AND team = ?
                  AND status IN ('pending', 'delivered')
                """,
                [(now, now, message_id, agent_id, team) for message_id in ids],
            )
            return self.conn.total_changes - before

    def done(self, message_ids: Iterable[str], agent_id: str, team: str) -> int:
        ids = list(message_ids)
        if not ids:
            return 0
        now = utc_now()
        with self.conn:
            before = self.conn.total_changes
            self.conn.executemany(
                """
                UPDATE deliveries
                SET status = 'done',
                    delivered_at = COALESCE(delivered_at, ?),
                    done_at = COALESCE(done_at, ?)
                WHERE message_id = ? AND to_agent = ? AND team = ?
                  AND status IN ('pending', 'delivered', 'acknowledged')
                """,
                [(now, now, message_id, agent_id, team) for message_id in ids],
            )
            return self.conn.total_changes - before

    def delivery_status(self, message_id: str, agent_id: str, team: str) -> str | None:
        row = self.conn.execute(
            """
            SELECT status FROM deliveries
            WHERE message_id = ? AND to_agent = ? AND team = ?
            """,
            (message_id, agent_id, team),
        ).fetchone()
        return row["status"] if row else None
