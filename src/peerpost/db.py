"""SQLite storage for peerpost."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .paths import ensure_home, get_paths, restrict_file, restrict_sqlite_files


SCHEMA_VERSION = "1"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def make_message_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"msg_{stamp}_{secrets.token_hex(3)}"


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def like_prefix(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


def locked_method(method):
    def wrapper(self: "Store", *args: Any, **kwargs: Any):
        with self.lock:
            return method(self, *args, **kwargs)

    return wrapper


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
    restrict_sqlite_files(db_path)
    migrate(conn)
    restrict_sqlite_files(db_path)
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
        self.lock = threading.RLock()

    @locked_method
    def close(self) -> None:
        self.conn.close()

    @locked_method
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

    @locked_method
    def get_agent(self, agent_id: str, team: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM agents WHERE id = ? AND team = ?", (agent_id, team)
        ).fetchone()
        return row_to_dict(row) if row else None

    @locked_method
    def list_agents(self, team: str | None = None) -> list[dict[str, Any]]:
        if team:
            rows = self.conn.execute(
                "SELECT * FROM agents WHERE team = ? ORDER BY id", (team,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM agents ORDER BY team, id").fetchall()
        return [row_to_dict(row) for row in rows]

    @locked_method
    def leave_agent(self, agent_id: str, team: str) -> bool:
        with self.conn:
            cursor = self.conn.execute(
                "DELETE FROM agents WHERE id = ? AND team = ?", (agent_id, team)
            )
        return cursor.rowcount > 0

    @locked_method
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

    @locked_method
    def broadcast_targets(self, team: str, from_agent: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT id FROM agents WHERE team = ? AND id != ? ORDER BY id", (team, from_agent)
        ).fetchall()
        return [row["id"] for row in rows]

    @locked_method
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

    @locked_method
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

    @locked_method
    def drain(self, agent_id: str, team: str, limit: int = 20) -> list[Message]:
        messages = self.pending_messages(agent_id, team, limit)
        self.mark_delivered([message.id for message in messages], agent_id, team)
        return messages

    @locked_method
    def inbox(self, agent_id: str, team: str, include_all: bool = False) -> list[Message]:
        if include_all:
            return self._messages_query("d.team = ? AND d.to_agent = ?", (team, agent_id))
        return self._messages_query(
            "d.team = ? AND d.to_agent = ? AND d.status != 'done'", (team, agent_id)
        )

    @locked_method
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

    @locked_method
    def thread(self, team: str, message_id: str) -> list[Message]:
        rows = self.conn.execute(
            "SELECT id, parent_id FROM messages WHERE team = ? ORDER BY created_at, id",
            (team,),
        ).fetchall()
        parents = {row["id"]: row["parent_id"] for row in rows}
        if message_id not in parents:
            return []

        root = message_id
        seen = {root}
        while parents[root] in parents and parents[root] not in seen:
            root = parents[root]
            seen.add(root)

        children: dict[str | None, list[str]] = {}
        for child_id, parent_id in parents.items():
            children.setdefault(parent_id, []).append(child_id)

        thread_ids: list[str] = []
        stack = [root]
        while stack:
            current = stack.pop()
            thread_ids.append(current)
            stack.extend(reversed(children.get(current, [])))

        placeholders = ",".join("?" for _ in thread_ids)
        return self._messages_query(
            f"m.team = ? AND m.id IN ({placeholders})", tuple([team, *thread_ids])
        )

    @locked_method
    def prune_done(
        self,
        before: str,
        team: str | None = None,
        limit: int = 100,
        apply: bool = False,
    ) -> dict[str, Any]:
        clauses = ["m.created_at < ?"]
        params: list[Any] = [before]
        if team:
            clauses.append("m.team = ?")
            params.append(team)
        where_sql = " AND ".join(clauses)
        rows = self.conn.execute(
            f"""
            SELECT m.id
            FROM messages m
            JOIN deliveries d ON d.message_id = m.id
            WHERE {where_sql}
            GROUP BY m.id
            HAVING COUNT(d.message_id) > 0
               AND SUM(CASE WHEN d.status != 'done' THEN 1 ELSE 0 END) = 0
            ORDER BY m.created_at, m.id
            LIMIT ?
            """,
            tuple([*params, limit]),
        ).fetchall()
        message_ids = [row["id"] for row in rows]
        deleted = 0
        if apply and message_ids:
            placeholders = ",".join("?" for _ in message_ids)
            with self.conn:
                self.conn.execute(
                    f"DELETE FROM deliveries WHERE message_id IN ({placeholders})",
                    tuple(message_ids),
                )
                cursor = self.conn.execute(
                    f"DELETE FROM messages WHERE id IN ({placeholders})",
                    tuple(message_ids),
                )
                deleted = cursor.rowcount
        return {
            "before": before,
            "team": team,
            "limit": limit,
            "dry_run": not apply,
            "matched": len(message_ids),
            "deleted": deleted,
            "message_ids": message_ids,
        }

    @locked_method
    def backup(self, output_path: Path, overwrite: bool = False) -> dict[str, Any]:
        output_path = output_path.expanduser().resolve()
        output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            output_path.parent.chmod(0o700)
        except OSError:
            pass
        if output_path.exists() and output_path.is_dir():
            raise IsADirectoryError(str(output_path))
        if output_path.exists() and not overwrite:
            raise FileExistsError(str(output_path))
        temp_path = output_path.with_name(f".{output_path.name}.tmp-{secrets.token_hex(4)}")
        try:
            destination = sqlite3.connect(temp_path)
            try:
                self.conn.backup(destination)
            finally:
                destination.close()
            restrict_file(temp_path)
            temp_path.replace(output_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        restrict_file(output_path)
        return {
            "path": str(output_path),
            "bytes": output_path.stat().st_size,
            "created_at": utc_now(),
        }

    @locked_method
    def delivery_health(self, team: str | None = None) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if team:
            clauses.append("team = ?")
            params.append(team)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        status_rows = self.conn.execute(
            f"""
            SELECT status, COUNT(*) AS count
            FROM deliveries
            {where_sql}
            GROUP BY status
            ORDER BY status
            """,
            tuple(params),
        ).fetchall()
        status_counts = {row["status"]: row["count"] for row in status_rows}

        orphan_clauses = ["a.id IS NULL", "d.status != 'done'"]
        orphan_params: list[Any] = []
        if team:
            orphan_clauses.append("d.team = ?")
            orphan_params.append(team)
        orphan_rows = self.conn.execute(
            f"""
            SELECT
              d.team,
              d.to_agent,
              COUNT(*) AS total,
              SUM(CASE WHEN d.status = 'pending' THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN d.status != 'done' THEN 1 ELSE 0 END) AS non_done
            FROM deliveries d
            LEFT JOIN agents a ON a.team = d.team AND a.id = d.to_agent
            WHERE {' AND '.join(orphan_clauses)}
            GROUP BY d.team, d.to_agent
            ORDER BY d.team, d.to_agent
            """,
            tuple(orphan_params),
        ).fetchall()
        orphans = [row_to_dict(row) for row in orphan_rows]

        sender_clauses = ["a.id IS NULL"]
        sender_params: list[Any] = []
        if team:
            sender_clauses.append("m.team = ?")
            sender_params.append(team)
        sender_rows = self.conn.execute(
            f"""
            SELECT
              m.team,
              m.from_agent,
              COUNT(*) AS total,
              MAX(m.created_at) AS last_message_at
            FROM messages m
            LEFT JOIN agents a ON a.team = m.team AND a.id = m.from_agent
            WHERE {' AND '.join(sender_clauses)}
            GROUP BY m.team, m.from_agent
            ORDER BY m.team, m.from_agent
            """,
            tuple(sender_params),
        ).fetchall()
        unregistered_senders = [row_to_dict(row) for row in sender_rows]
        return {
            "team": team,
            "status_counts": status_counts,
            "orphan_count": len(orphans),
            "orphans": orphans,
            "unregistered_sender_count": len(unregistered_senders),
            "unregistered_senders": unregistered_senders,
        }

    @locked_method
    def team_status(self, team: str) -> dict[str, Any]:
        agents = self.list_agents(team)
        status_rows = self.conn.execute(
            """
            SELECT to_agent, status, COUNT(*) AS count
            FROM deliveries
            WHERE team = ?
            GROUP BY to_agent, status
            ORDER BY to_agent, status
            """,
            (team,),
        ).fetchall()
        counts_by_agent: dict[str, dict[str, int]] = {}
        totals: dict[str, int] = {}
        for row in status_rows:
            agent_counts = counts_by_agent.setdefault(row["to_agent"], {})
            agent_counts[row["status"]] = row["count"]
            totals[row["status"]] = totals.get(row["status"], 0) + row["count"]

        last_rows = self.conn.execute(
            """
            SELECT d.to_agent, MAX(m.created_at) AS last_message_at
            FROM deliveries d
            JOIN messages m ON m.id = d.message_id
            WHERE d.team = ?
            GROUP BY d.to_agent
            """,
            (team,),
        ).fetchall()
        last_by_agent = {row["to_agent"]: row["last_message_at"] for row in last_rows}

        agent_ids = {agent["id"] for agent in agents}
        agent_reports: list[dict[str, Any]] = []
        for agent in agents:
            counts = counts_by_agent.get(agent["id"], {})
            pending = int(counts.get("pending", 0))
            delivered = int(counts.get("delivered", 0))
            acknowledged = int(counts.get("acknowledged", 0))
            done = int(counts.get("done", 0))
            agent_reports.append(
                {
                    **agent,
                    "pending": pending,
                    "delivered": delivered,
                    "acknowledged": acknowledged,
                    "done": done,
                    "non_done": pending + delivered + acknowledged,
                    "last_message_at": last_by_agent.get(agent["id"]),
                }
            )

        unregistered: list[dict[str, Any]] = []
        for to_agent, counts in sorted(counts_by_agent.items()):
            if to_agent in agent_ids:
                continue
            pending = int(counts.get("pending", 0))
            delivered = int(counts.get("delivered", 0))
            acknowledged = int(counts.get("acknowledged", 0))
            done = int(counts.get("done", 0))
            unregistered.append(
                {
                    "team": team,
                    "to_agent": to_agent,
                    "pending": pending,
                    "delivered": delivered,
                    "acknowledged": acknowledged,
                    "done": done,
                    "non_done": pending + delivered + acknowledged,
                    "last_message_at": last_by_agent.get(to_agent),
                }
            )

        return {
            "team": team,
            "agents": agent_reports,
            "totals": totals,
            "unregistered": unregistered,
        }

    @locked_method
    def self_test(self) -> dict[str, Any]:
        team = f"peerpost-self-test-{secrets.token_hex(4)}"
        sender = "peerpost-self-sender"
        receiver = "peerpost-self-receiver"
        message_id = make_message_id()
        created_at = utc_now()
        checks: list[dict[str, Any]] = []
        self.conn.execute("SAVEPOINT peerpost_self_test")
        try:
            self.conn.executemany(
                """
                INSERT INTO agents (id, team, agent_type, workspace, created_at, updated_at)
                VALUES (?, ?, 'generic', NULL, ?, ?)
                """,
                [
                    (sender, team, created_at, created_at),
                    (receiver, team, created_at, created_at),
                ],
            )
            agent_count = self.conn.execute(
                "SELECT COUNT(*) AS count FROM agents WHERE team = ?",
                (team,),
            ).fetchone()["count"]
            checks.append({"name": "agents", "ok": agent_count == 2, "detail": f"{agent_count} registered"})

            self.conn.execute(
                """
                INSERT INTO messages
                  (id, team, from_agent, body, kind, priority, parent_id, created_at, metadata_json)
                VALUES (?, ?, ?, ?, 'self-test', 'normal', NULL, ?, '{}')
                """,
                (message_id, team, sender, "peerpost self-test", created_at),
            )
            self.conn.execute(
                """
                INSERT INTO deliveries (message_id, team, to_agent, status)
                VALUES (?, ?, ?, 'pending')
                """,
                (message_id, team, receiver),
            )
            pending_count = self.conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM messages m
                JOIN deliveries d ON d.message_id = m.id
                WHERE d.team = ? AND d.to_agent = ? AND d.status = 'pending'
                """,
                (team, receiver),
            ).fetchone()["count"]
            checks.append({"name": "pending", "ok": pending_count == 1, "detail": f"{pending_count} pending"})

            delivered_at = utc_now()
            self.conn.execute(
                """
                UPDATE deliveries
                SET status = 'delivered', delivered_at = ?
                WHERE message_id = ? AND team = ? AND to_agent = ? AND status = 'pending'
                """,
                (delivered_at, message_id, team, receiver),
            )
            status = self.conn.execute(
                """
                SELECT status FROM deliveries
                WHERE message_id = ? AND team = ? AND to_agent = ?
                """,
                (message_id, team, receiver),
            ).fetchone()["status"]
            checks.append({"name": "delivery", "ok": status == "delivered", "detail": status})

            ok = all(check["ok"] for check in checks)
            return {
                "ok": ok,
                "team": team,
                "message_id": message_id,
                "checks": checks,
            }
        finally:
            self.conn.execute("ROLLBACK TO peerpost_self_test")
            self.conn.execute("RELEASE peerpost_self_test")

    @locked_method
    def read_message(self, message_id: str, agent_id: str, team: str) -> Message | None:
        messages = self._messages_query(
            "m.id = ? AND d.team = ? AND d.to_agent = ?", (message_id, team, agent_id)
        )
        if not messages:
            return None
        self.mark_delivered([message_id], agent_id, team)
        return messages[0]

    @locked_method
    def get_message_for_agent(self, message_id: str, agent_id: str, team: str) -> Message | None:
        messages = self._messages_query(
            "m.id = ? AND d.team = ? AND d.to_agent = ?", (message_id, team, agent_id)
        )
        return messages[0] if messages else None

    @locked_method
    def matching_message_ids(
        self, team: str, message_id_or_prefix: str, agent_id: str | None = None, limit: int = 2
    ) -> list[str]:
        if agent_id:
            exact = self.conn.execute(
                """
                SELECT m.id
                FROM messages m
                JOIN deliveries d ON d.message_id = m.id
                WHERE m.team = ? AND d.team = ? AND d.to_agent = ? AND m.id = ?
                """,
                (team, team, agent_id, message_id_or_prefix),
            ).fetchall()
            if exact:
                return [row["id"] for row in exact]
            rows = self.conn.execute(
                """
                SELECT m.id
                FROM messages m
                JOIN deliveries d ON d.message_id = m.id
                WHERE m.team = ? AND d.team = ? AND d.to_agent = ?
                  AND m.id LIKE ? ESCAPE '\\'
                ORDER BY m.id
                LIMIT ?
                """,
                (team, team, agent_id, like_prefix(message_id_or_prefix), limit),
            ).fetchall()
        else:
            exact = self.conn.execute(
                "SELECT id FROM messages WHERE team = ? AND id = ?",
                (team, message_id_or_prefix),
            ).fetchall()
            if exact:
                return [row["id"] for row in exact]
            rows = self.conn.execute(
                """
                SELECT id
                FROM messages
                WHERE team = ? AND id LIKE ? ESCAPE '\\'
                ORDER BY id
                LIMIT ?
                """,
                (team, like_prefix(message_id_or_prefix), limit),
            ).fetchall()
        return [row["id"] for row in rows]

    @locked_method
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

    @locked_method
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

    @locked_method
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

    @locked_method
    def delivery_status(self, message_id: str, agent_id: str, team: str) -> str | None:
        row = self.conn.execute(
            """
            SELECT status FROM deliveries
            WHERE message_id = ? AND to_agent = ? AND team = ?
            """,
            (message_id, agent_id, team),
        ).fetchone()
        return row["status"] if row else None
