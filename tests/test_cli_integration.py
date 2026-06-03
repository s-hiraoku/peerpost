from __future__ import annotations

import json
import os
import select
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from peerpost.client import DaemonNotRunning, PeerpostClient, PeerpostClientError
from peerpost.protocol import MAX_BODY_CHARS, decode_json_line, encode_json_line


ROOT = Path(__file__).resolve().parents[1]


class CliIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = os.environ.copy()
        self.env["PEERPOST_HOME"] = self.tmp.name
        self.env["PEERPOST_SOCKET"] = str(Path(self.tmp.name) / "peerpost.sock")
        existing_path = self.env.get("PYTHONPATH")
        self.env["PYTHONPATH"] = (
            str(ROOT / "src") if not existing_path else f"{ROOT / 'src'}{os.pathsep}{existing_path}"
        )
        self.daemon = subprocess.Popen(
            [sys.executable, "-m", "peerpost.daemon", "--foreground"],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._stop_daemon)
        self._wait_for_daemon()

    def _wait_for_daemon(self) -> None:
        old_home = os.environ.get("PEERPOST_HOME")
        old_socket = os.environ.get("PEERPOST_SOCKET")
        os.environ["PEERPOST_HOME"] = self.env["PEERPOST_HOME"]
        os.environ["PEERPOST_SOCKET"] = self.env["PEERPOST_SOCKET"]
        try:
            client = PeerpostClient()
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    client.request("ping")
                    return
                except DaemonNotRunning:
                    if self.daemon.poll() is not None:
                        _, stderr = self.daemon.communicate(timeout=1)
                        self.fail(f"daemon exited early: {stderr}")
                    time.sleep(0.05)
            self.fail("daemon did not start")
        finally:
            if old_home is None:
                os.environ.pop("PEERPOST_HOME", None)
            else:
                os.environ["PEERPOST_HOME"] = old_home
            if old_socket is None:
                os.environ.pop("PEERPOST_SOCKET", None)
            else:
                os.environ["PEERPOST_SOCKET"] = old_socket

    def _stop_daemon(self) -> None:
        if self.daemon.poll() is None:
            subprocess.run(
                [sys.executable, "-m", "peerpost.cli", "daemon", "stop"],
                env=self.env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            try:
                self.daemon.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.daemon.terminate()
                self.daemon.wait(timeout=3)
        if self.daemon.stdout:
            self.daemon.stdout.close()
        if self.daemon.stderr:
            self.daemon.stderr.close()

    def run_peerpost(
        self,
        *args: str,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "peerpost.cli", *args],
            env=env or self.env,
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )

    def test_send_and_drain_flow(self) -> None:
        self.assertEqual(
            self.run_peerpost("join", "--agent", "claude", "--type", "claude-code", "--team", "dev").returncode,
            0,
        )
        self.assertEqual(
            self.run_peerpost("join", "--agent", "codex", "--type", "codex", "--team", "dev").returncode,
            0,
        )
        sent = self.run_peerpost(
            "send",
            "--from",
            "claude",
            "--to",
            "codex",
            "--team",
            "dev",
            "Please review the auth middleware.",
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertIn("sent msg_", sent.stdout)

        drained = self.run_peerpost(
            "drain", "--agent", "codex", "--team", "dev", "--format", "plain"
        )
        self.assertEqual(drained.returncode, 0, drained.stderr)
        self.assertIn("msg_", drained.stdout)
        self.assertIn("claude -> codex: Please review the auth middleware.", drained.stdout)

        second = self.run_peerpost(
            "drain", "--agent", "codex", "--team", "dev", "--format", "plain"
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout, "")

    def test_agents_plain_output_sanitizes_workspace(self) -> None:
        joined = self.run_peerpost(
            "join",
            "--agent",
            "claude",
            "--type",
            "claude-code",
            "--team",
            "dev",
            "--workspace",
            "\x1b[31m/tmp/project\x1b[0m\nforged",
        )
        self.assertEqual(joined.returncode, 0, joined.stderr)

        agents = self.run_peerpost("agents", "--team", "dev")

        self.assertEqual(agents.returncode, 0, agents.stderr)
        self.assertNotIn("\x1b", agents.stdout)
        self.assertNotIn("project\nforged", agents.stdout)
        self.assertIn("/tmp/project forged", agents.stdout)

    def test_send_warns_for_unregistered_direct_recipient(self) -> None:
        sent = self.run_peerpost(
            "send",
            "--from",
            "claude",
            "--to",
            "cdoex",
            "--team",
            "dev",
            "--format",
            "json",
            "typo target",
        )

        self.assertEqual(sent.returncode, 0)
        payload = json.loads(sent.stdout)
        self.assertEqual(payload["unregistered_targets"], ["cdoex"])
        self.assertIn("warning: unregistered recipient id(s): cdoex", sent.stderr)

    def test_send_warns_for_unregistered_sender(self) -> None:
        self.run_peerpost("join", "--agent", "codex", "--type", "codex", "--team", "dev")

        sent = self.run_peerpost(
            "send",
            "--from",
            "claud",
            "--to",
            "codex",
            "--team",
            "dev",
            "--format",
            "json",
            "typo sender",
        )

        self.assertEqual(sent.returncode, 0, sent.stderr)
        payload = json.loads(sent.stdout)
        self.assertTrue(payload["unregistered_from_agent"])
        self.assertEqual(payload["unregistered_targets"], [])
        self.assertIn("warning: sender agent is not registered: claud", sent.stderr)
        self.assertIn("peerpost join --agent claud --type generic --team dev", sent.stderr)

    def test_send_reply_priority_metadata_is_drained_as_json(self) -> None:
        self.run_peerpost("join", "--agent", "claude", "--type", "claude-code", "--team", "dev")
        self.run_peerpost("join", "--agent", "codex", "--type", "codex", "--team", "dev")
        sent = self.run_peerpost(
            "send",
            "--from",
            "claude",
            "--to",
            "codex",
            "--team",
            "dev",
            "--kind",
            "review",
            "--priority",
            "high",
            "--reply-to",
            "msg_parent",
            "Please review this follow-up.",
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        drained = self.run_peerpost(
            "drain", "--agent", "codex", "--team", "dev", "--format", "json"
        )
        self.assertEqual(drained.returncode, 0, drained.stderr)
        messages = json.loads(drained.stdout)
        self.assertEqual(messages[0]["kind"], "review")
        self.assertEqual(messages[0]["priority"], "high")
        self.assertEqual(messages[0]["parent_id"], "msg_parent")

    def test_send_and_reply_can_read_message_body_from_stdin(self) -> None:
        self.run_peerpost("join", "--agent", "claude", "--type", "claude-code", "--team", "dev")
        self.run_peerpost("join", "--agent", "codex", "--type", "codex", "--team", "dev")
        sent = self.run_peerpost(
            "send",
            "--from",
            "claude",
            "--to",
            "codex",
            "--team",
            "dev",
            "--stdin",
            input_text="line one\nline two\n",
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        message_id = sent.stdout.split()[1]

        drained = self.run_peerpost("drain", "--agent", "codex", "--team", "dev", "--format", "json")
        self.assertEqual(drained.returncode, 0, drained.stderr)
        self.assertEqual(json.loads(drained.stdout)[0]["body"], "line one\nline two\n")

        reply = self.run_peerpost(
            "reply",
            message_id,
            "--from",
            "codex",
            "--team",
            "dev",
            "--stdin",
            input_text="reply line one\nreply line two\n",
        )
        self.assertEqual(reply.returncode, 0, reply.stderr)

        response = self.run_peerpost("drain", "--agent", "claude", "--team", "dev", "--format", "json")
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertEqual(json.loads(response.stdout)[0]["body"], "reply line one\nreply line two\n")

    def test_stdin_message_rejects_double_body_input(self) -> None:
        self.run_peerpost("join", "--agent", "claude", "--type", "claude-code", "--team", "dev")
        result = self.run_peerpost(
            "send",
            "--from",
            "claude",
            "--to",
            "codex",
            "--team",
            "dev",
            "--stdin",
            "body",
            input_text="stdin body",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("pass either MESSAGE or --stdin", result.stderr)

    def test_reply_sends_back_to_original_sender(self) -> None:
        self.run_peerpost("join", "--agent", "claude", "--type", "claude-code", "--team", "dev")
        self.run_peerpost("join", "--agent", "codex", "--type", "codex", "--team", "dev")
        sent = self.run_peerpost(
            "send", "--from", "claude", "--to", "codex", "--team", "dev", "Question for Codex"
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        parent_id = sent.stdout.split()[1]
        reply = self.run_peerpost(
            "reply",
            parent_id,
            "--from",
            "codex",
            "--team",
            "dev",
            "--priority",
            "high",
            "Answer from Codex",
        )
        self.assertEqual(reply.returncode, 0, reply.stderr)
        self.assertIn(f"in reply to {parent_id} to claude", reply.stdout)

        drained = self.run_peerpost(
            "drain", "--agent", "claude", "--team", "dev", "--format", "json"
        )
        self.assertEqual(drained.returncode, 0, drained.stderr)
        messages = json.loads(drained.stdout)
        self.assertEqual(messages[0]["from_agent"], "codex")
        self.assertEqual(messages[0]["to_agent"], "claude")
        self.assertEqual(messages[0]["body"], "Answer from Codex")
        self.assertEqual(messages[0]["kind"], "reply")
        self.assertEqual(messages[0]["priority"], "high")
        self.assertEqual(messages[0]["parent_id"], parent_id)

    def test_thread_returns_conversation_chain(self) -> None:
        self.run_peerpost("join", "--agent", "claude", "--type", "claude-code", "--team", "dev")
        self.run_peerpost("join", "--agent", "codex", "--type", "codex", "--team", "dev")
        sent = self.run_peerpost(
            "send", "--from", "claude", "--to", "codex", "--team", "dev", "Question"
        )
        root_id = sent.stdout.split()[1]
        reply = self.run_peerpost("reply", root_id, "--from", "codex", "--team", "dev", "Answer")
        reply_id = reply.stdout.split()[1]

        thread = self.run_peerpost("thread", reply_id, "--team", "dev", "--format", "json")
        self.assertEqual(thread.returncode, 0, thread.stderr)
        messages = json.loads(thread.stdout)
        self.assertEqual([message["id"] for message in messages], [root_id, reply_id])
        self.assertEqual(messages[1]["parent_id"], root_id)

    def test_message_id_prefixes_work_for_daily_message_commands(self) -> None:
        self.run_peerpost("join", "--agent", "claude", "--type", "claude-code", "--team", "dev")
        self.run_peerpost("join", "--agent", "codex", "--type", "codex", "--team", "dev")
        sent = self.run_peerpost(
            "send",
            "--from",
            "claude",
            "--to",
            "codex",
            "--team",
            "dev",
            "--format",
            "json",
            "Question",
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        message_id = json.loads(sent.stdout)["message"]["id"]
        prefix = message_id[:-2]

        read = self.run_peerpost("read", prefix, "--agent", "codex", "--team", "dev", "--format", "json")
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertEqual(json.loads(read.stdout)["id"], message_id)

        reply = self.run_peerpost("reply", prefix, "--from", "codex", "--team", "dev", "Answer")
        self.assertEqual(reply.returncode, 0, reply.stderr)
        self.assertIn(f"in reply to {message_id} to claude", reply.stdout)
        reply_id = reply.stdout.split()[1]

        thread = self.run_peerpost("thread", reply_id[:-2], "--team", "dev", "--format", "json")
        self.assertEqual(thread.returncode, 0, thread.stderr)
        self.assertEqual([message["id"] for message in json.loads(thread.stdout)], [message_id, reply_id])

        ack = self.run_peerpost("ack", prefix, "--agent", "codex", "--team", "dev", "--format", "json")
        self.assertEqual(ack.returncode, 0, ack.stderr)
        self.assertEqual(json.loads(ack.stdout)["updated"], 1)

        done = self.run_peerpost("done", prefix, "--agent", "codex", "--team", "dev", "--format", "json")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["updated"], 1)

    def test_ambiguous_message_id_prefix_is_rejected(self) -> None:
        self.run_peerpost("join", "--agent", "claude", "--type", "claude-code", "--team", "dev")
        self.run_peerpost("join", "--agent", "codex", "--type", "codex", "--team", "dev")
        self.run_peerpost("send", "--from", "claude", "--to", "codex", "--team", "dev", "one")
        self.run_peerpost("send", "--from", "claude", "--to", "codex", "--team", "dev", "two")

        result = self.run_peerpost("read", "msg_", "--agent", "codex", "--team", "dev")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("message id prefix matches multiple messages", result.stderr)

    def test_ack_and_done_reject_unresolved_message_ids(self) -> None:
        self.run_peerpost("join", "--agent", "claude", "--type", "claude-code", "--team", "dev")
        self.run_peerpost("join", "--agent", "codex", "--type", "codex", "--team", "dev")

        missing_ack = self.run_peerpost("ack", "msg_missing", "--agent", "codex", "--team", "dev")
        self.assertEqual(missing_ack.returncode, 1)
        self.assertIn("message id not found for this agent/team", missing_ack.stderr)

        missing_done = self.run_peerpost("done", "msg_missing", "--agent", "codex", "--team", "dev")
        self.assertEqual(missing_done.returncode, 1)
        self.assertIn("message id not found for this agent/team", missing_done.stderr)

        sent = self.run_peerpost(
            "send",
            "--from",
            "claude",
            "--to",
            "codex",
            "--team",
            "dev",
            "Only codex should mark this.",
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        message_id = sent.stdout.split()[1]

        wrong_agent = self.run_peerpost("done", message_id, "--agent", "claude", "--team", "dev")
        self.assertEqual(wrong_agent.returncode, 1)
        self.assertIn("message id not found for this agent/team", wrong_agent.stderr)

    def test_reply_rejects_message_not_delivered_to_agent(self) -> None:
        self.run_peerpost("join", "--agent", "claude", "--type", "claude-code", "--team", "dev")
        self.run_peerpost("join", "--agent", "codex", "--type", "codex", "--team", "dev")
        sent = self.run_peerpost(
            "send", "--from", "claude", "--to", "codex", "--team", "dev", "Question for Codex"
        )
        parent_id = sent.stdout.split()[1]
        rejected = self.run_peerpost(
            "reply",
            parent_id,
            "--from",
            "claude",
            "--team",
            "dev",
            "This should not work",
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("message not found for this agent/team", rejected.stderr)

    def test_doctor_reports_running_daemon(self) -> None:
        result = self.run_peerpost("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("home: ok:", result.stdout)
        self.assertIn("daemon: ok: running pid", result.stdout)
        self.assertIn("version 1.0.0", result.stdout)
        self.assertIn("version: ok: cli 1.0.0; daemon 1.0.0", result.stdout)
        self.assertIn("status: ok", result.stdout)

    def test_doctor_json_reports_actionable_checks(self) -> None:
        result = self.run_peerpost("doctor", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["paths"]["home"], self.env["PEERPOST_HOME"])
        self.assertEqual(report["version"], {"cli": "1.0.0", "daemon": "1.0.0"})
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["daemon"]["status"], "ok")
        self.assertEqual(checks["version"]["status"], "ok")
        self.assertEqual(checks["database"]["status"], "ok")
        self.assertIn("quick_check ok", checks["database"]["detail"])
        self.assertIn("foreign_key_check ok", checks["database"]["detail"])
        self.assertEqual(checks["socket"]["status"], "ok")
        self.assertEqual(checks["autostart"]["status"], "info")
        self.assertIn("log", checks)

    def test_doctor_reports_foreign_key_violations(self) -> None:
        db_path = Path(self.env["PEERPOST_HOME"]) / "peerpost.sqlite"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "INSERT INTO deliveries (message_id, team, to_agent, status) "
                "VALUES (?, ?, ?, ?)",
                ("msg_missing_fk", "dev", "codex", "pending"),
            )
            conn.commit()
        finally:
            conn.close()

        result = self.run_peerpost("doctor", "--format", "json")

        self.assertEqual(result.returncode, 1, result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "errors")
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["database"]["status"], "error")
        self.assertIn("foreign_key_check failed", checks["database"]["detail"])
        self.assertIn("restore from a recent peerpost backup", checks["database"]["fix"])

    def test_daemon_status_reports_version(self) -> None:
        result = self.run_peerpost("daemon", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("version 1.0.0", result.stdout)

    def test_doctor_team_reports_agent_registration_health(self) -> None:
        empty = self.run_peerpost("doctor", "--team", "empty", "--format", "json")
        self.assertEqual(empty.returncode, 0, empty.stderr)
        empty_report = json.loads(empty.stdout)
        empty_checks = {check["name"]: check for check in empty_report["checks"]}
        self.assertEqual(empty_report["status"], "warnings")
        self.assertEqual(empty_checks["agents"]["status"], "warn")
        self.assertIn("has no registered agents", empty_checks["agents"]["detail"])
        self.assertEqual(empty_checks["agents"]["fix"], "peerpost setup --start-daemon --team empty")
        self.assertEqual(empty_report["team_status"]["agents"], [])

        self.run_peerpost("join", "--agent", "codex", "--type", "codex", "--team", "dev")
        configured = self.run_peerpost("doctor", "--team", "dev", "--format", "json")
        self.assertEqual(configured.returncode, 0, configured.stderr)
        configured_report = json.loads(configured.stdout)
        configured_checks = {check["name"]: check for check in configured_report["checks"]}
        self.assertEqual(configured_report["status"], "ok")
        self.assertEqual(configured_checks["agents"]["status"], "ok")
        self.assertEqual(configured_report["team_status"]["agents"][0]["id"], "codex")

    def test_doctor_self_test_exercises_delivery_path(self) -> None:
        db_path = Path(self.env["PEERPOST_HOME"]) / "peerpost.sqlite"
        before = sqlite3.connect(db_path)
        try:
            before_counts = {
                table: before.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("agents", "messages", "deliveries")
            }
        finally:
            before.close()

        result = self.run_peerpost("doctor", "--self-test", "--format", "json")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "ok")
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["self-test"]["status"], "ok")
        self.assertTrue(report["self_test"]["ok"])
        after = sqlite3.connect(db_path)
        try:
            after_counts = {
                table: after.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("agents", "messages", "deliveries")
            }
        finally:
            after.close()
        self.assertEqual(after_counts, before_counts)

    def test_doctor_team_reports_unregistered_message_agents(self) -> None:
        sent = self.run_peerpost(
            "send",
            "--from",
            "claude",
            "--to",
            "cdoex",
            "--team",
            "dev",
            "typo target",
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)

        result = self.run_peerpost("doctor", "--team", "dev", "--format", "json")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "warnings")
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["deliveries"]["status"], "warn")
        self.assertIn("1 unregistered sender(s)", checks["deliveries"]["detail"])
        self.assertEqual(report["delivery_health"]["orphan_count"], 1)
        self.assertEqual(report["delivery_health"]["orphans"][0]["to_agent"], "cdoex")
        self.assertEqual(report["delivery_health"]["unregistered_sender_count"], 1)
        self.assertEqual(report["delivery_health"]["unregistered_senders"][0]["from_agent"], "claude")

    def test_doctor_team_reports_invalid_message_priority(self) -> None:
        sent = self.run_peerpost(
            "send",
            "--from",
            "claude",
            "--to",
            "codex",
            "--team",
            "dev",
            "--format",
            "json",
            "bad historical priority",
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        message_id = json.loads(sent.stdout)["message"]["id"]
        conn = sqlite3.connect(Path(self.env["PEERPOST_HOME"]) / "peerpost.sqlite")
        try:
            conn.execute(
                "UPDATE messages SET priority = ? WHERE id = ?",
                ("later", message_id),
            )
            conn.commit()
        finally:
            conn.close()

        result = self.run_peerpost("doctor", "--team", "dev", "--format", "json")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "warnings")
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["deliveries"]["status"], "warn")
        self.assertIn("1 invalid priority value(s)", checks["deliveries"]["detail"])
        self.assertEqual(report["delivery_health"]["invalid_priority_count"], 1)
        self.assertEqual(report["delivery_health"]["invalid_priorities"][0]["priority"], "later")

    def test_doctor_team_reports_invalid_delivery_status(self) -> None:
        sent = self.run_peerpost(
            "send",
            "--from",
            "claude",
            "--to",
            "codex",
            "--team",
            "dev",
            "--format",
            "json",
            "bad historical status",
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        message_id = json.loads(sent.stdout)["message"]["id"]
        conn = sqlite3.connect(Path(self.env["PEERPOST_HOME"]) / "peerpost.sqlite")
        try:
            conn.execute(
                "UPDATE deliveries SET status = ? WHERE message_id = ?",
                ("lost", message_id),
            )
            conn.commit()
        finally:
            conn.close()

        result = self.run_peerpost("doctor", "--team", "dev", "--format", "json")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "warnings")
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["deliveries"]["status"], "warn")
        self.assertIn("1 invalid delivery status value(s)", checks["deliveries"]["detail"])
        self.assertEqual(report["delivery_health"]["invalid_status_count"], 1)
        self.assertEqual(report["delivery_health"]["invalid_statuses"][0]["status"], "lost")

    def test_doctor_team_reports_messages_without_deliveries(self) -> None:
        conn = sqlite3.connect(Path(self.env["PEERPOST_HOME"]) / "peerpost.sqlite")
        try:
            conn.execute(
                """
                INSERT INTO messages
                  (id, team, from_agent, body, kind, priority, parent_id, created_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?, '{}')
                """,
                (
                    "msg_20260604T010203456Z_orphan",
                    "dev",
                    "claude",
                    "orphan",
                    "message",
                    "normal",
                    "2026-06-04T01:02:03Z",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        result = self.run_peerpost("doctor", "--team", "dev", "--format", "json")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "warnings")
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["deliveries"]["status"], "warn")
        self.assertIn("1 message(s) without delivery rows", checks["deliveries"]["detail"])
        self.assertEqual(report["delivery_health"]["messages_without_delivery_count"], 1)
        self.assertEqual(report["delivery_health"]["messages_without_deliveries"][0]["team"], "dev")

    def test_doctor_team_reports_invalid_metadata_json(self) -> None:
        sent = self.run_peerpost(
            "send",
            "--from",
            "claude",
            "--to",
            "codex",
            "--team",
            "dev",
            "--format",
            "json",
            "bad metadata",
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        message_id = json.loads(sent.stdout)["message"]["id"]
        conn = sqlite3.connect(Path(self.env["PEERPOST_HOME"]) / "peerpost.sqlite")
        try:
            conn.execute(
                "UPDATE messages SET metadata_json = ? WHERE id = ?",
                ("{bad json", message_id),
            )
            conn.commit()
        finally:
            conn.close()

        drained = self.run_peerpost("drain", "--agent", "codex", "--team", "dev", "--format", "json")
        self.assertEqual(drained.returncode, 0, drained.stderr)
        self.assertEqual(json.loads(drained.stdout)[0]["metadata"], {})

        result = self.run_peerpost("doctor", "--team", "dev", "--format", "json")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "warnings")
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["deliveries"]["status"], "warn")
        self.assertIn("1 invalid metadata value(s)", checks["deliveries"]["detail"])
        self.assertEqual(report["delivery_health"]["invalid_metadata_count"], 1)
        self.assertEqual(report["delivery_health"]["invalid_metadata"][0]["team"], "dev")

    def test_doctor_suggests_daemon_start_when_not_running(self) -> None:
        self._stop_daemon()
        result = self.run_peerpost("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("daemon: warn:", result.stdout)
        self.assertIn("fix: peerpost daemon start", result.stdout)
        strict = self.run_peerpost("doctor", "--strict")
        self.assertEqual(strict.returncode, 1)

    def test_doctor_fix_repairs_database_mode(self) -> None:
        db_path = Path(self.env["PEERPOST_HOME"]) / "peerpost.sqlite"
        sidecars = [
            db_path.with_name(f"{db_path.name}-wal"),
            db_path.with_name(f"{db_path.name}-shm"),
        ]
        db_path.chmod(0o644)
        for sidecar in sidecars:
            self.assertTrue(sidecar.exists(), sidecar)
            sidecar.chmod(0o644)

        result = self.run_peerpost("doctor", "--fix", "--format", "json")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertIn(f"chmod 600 {db_path}", report["repairs"])
        self.assertEqual(db_path.stat().st_mode & 0o777, 0o600)
        for sidecar in sidecars:
            self.assertIn(f"chmod 600 {sidecar}", report["repairs"])
            self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["database"]["status"], "ok")
        self.assertIn("quick_check ok", checks["database"]["detail"])
        self.assertIn("foreign_key_check ok", checks["database"]["detail"])

    def test_doctor_fix_removes_stale_socket(self) -> None:
        self._stop_daemon()
        socket_path = Path(self.env["PEERPOST_SOCKET"])
        socket_path.write_text("stale", encoding="utf-8")

        result = self.run_peerpost("doctor", "--fix", "--format", "json")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertIn(f"removed stale socket {socket_path}", report["repairs"])
        self.assertFalse(socket_path.exists())
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["socket"]["status"], "info")

    def test_doctor_reports_and_repairs_socket_mode(self) -> None:
        socket_path = Path(self.env["PEERPOST_SOCKET"])
        socket_path.chmod(0o666)

        warned = self.run_peerpost("doctor", "--format", "json")
        self.assertEqual(warned.returncode, 0, warned.stderr)
        warned_report = json.loads(warned.stdout)
        warned_checks = {check["name"]: check for check in warned_report["checks"]}
        self.assertEqual(warned_report["status"], "warnings")
        self.assertEqual(warned_checks["socket"]["status"], "warn")
        self.assertIn("expected 0o600", warned_checks["socket"]["detail"])

        fixed = self.run_peerpost("doctor", "--fix", "--format", "json")
        self.assertEqual(fixed.returncode, 0, fixed.stderr)
        fixed_report = json.loads(fixed.stdout)
        fixed_checks = {check["name"]: check for check in fixed_report["checks"]}
        self.assertEqual(fixed_checks["socket"]["status"], "ok")
        self.assertIn(f"chmod 600 {socket_path}", fixed_report["repairs"])
        self.assertEqual(socket_path.stat().st_mode & 0o777, 0o600)

    def test_doctor_reports_autostart_health(self) -> None:
        env = {**self.env, "HOME": self.tmp.name}
        if sys.platform == "darwin":
            autostart_path = Path(self.tmp.name) / "Library" / "LaunchAgents" / "local.peerpost.peerpostd.plist"
        elif sys.platform.startswith("linux"):
            autostart_path = Path(self.tmp.name) / ".config" / "systemd" / "user" / "peerpostd.service"
        else:
            self.skipTest(f"autostart target detection is not supported on {sys.platform}")

        missing = self.run_peerpost("doctor", "--format", "json", env=env)
        self.assertEqual(missing.returncode, 0, missing.stderr)
        missing_checks = {check["name"]: check for check in json.loads(missing.stdout)["checks"]}
        self.assertEqual(missing_checks["autostart"]["status"], "info")
        self.assertEqual(missing_checks["autostart"]["fix"], "peerpost daemon install-autostart")

        install = self.run_peerpost("daemon", "install-autostart", env=env)
        self.assertEqual(install.returncode, 0, install.stderr)

        healthy = self.run_peerpost("doctor", "--format", "json", env=env)
        self.assertEqual(healthy.returncode, 0, healthy.stderr)
        healthy_report = json.loads(healthy.stdout)
        healthy_checks = {check["name"]: check for check in healthy_report["checks"]}
        self.assertEqual(healthy_report["status"], "ok")
        self.assertEqual(healthy_checks["autostart"]["status"], "ok")
        self.assertIn(str(autostart_path), healthy_checks["autostart"]["detail"])

        autostart_path.write_text("stale\n", encoding="utf-8")
        stale = self.run_peerpost("doctor", "--format", "json", env=env)
        self.assertEqual(stale.returncode, 0, stale.stderr)
        stale_report = json.loads(stale.stdout)
        stale_checks = {check["name"]: check for check in stale_report["checks"]}
        self.assertEqual(stale_report["status"], "warnings")
        self.assertEqual(stale_checks["autostart"]["status"], "warn")
        self.assertEqual(stale_checks["autostart"]["fix"], "peerpost daemon install-autostart --overwrite")

    def test_logs_reports_daemon_events_without_message_body(self) -> None:
        self.run_peerpost("join", "--agent", "claude", "--type", "claude-code", "--team", "dev")
        self.run_peerpost("join", "--agent", "codex", "--type", "codex", "--team", "dev")
        self.run_peerpost(
            "send",
            "--from",
            "claude",
            "--to",
            "codex",
            "--team",
            "dev",
            "secret body should not be logged",
        )
        result = self.run_peerpost("logs", "--tail", "20", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        text = "\n".join(payload["lines"])
        self.assertIn("peerpostd starting", text)
        self.assertIn("message stored id=msg_", text)
        self.assertNotIn("secret body should not be logged", text)

    def test_logs_plain_output_strips_control_characters(self) -> None:
        log_path = Path(self.env["PEERPOST_HOME"]) / "peerpost.log"
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write("unsafe \x1b[31mred\x1b[0m \x07line\n")

        result = self.run_peerpost("logs", "--tail", "1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\x1b", result.stdout)
        self.assertNotIn("\x07", result.stdout)
        self.assertIn("unsafe red line", result.stdout)

    def test_install_snippets_prints_adapter_commands(self) -> None:
        codex = self.run_peerpost(
            "install-snippets", "--adapter", "codex", "--agent", "codex", "--team", "dev"
        )
        self.assertEqual(codex.returncode, 0, codex.stderr)
        self.assertIn("Codex Stop hook command", codex.stdout)
        self.assertIn("peerpost drain --agent codex --team dev --format codex-hook", codex.stdout)

        all_snippets = self.run_peerpost("snippets", "--team", "dev")
        self.assertEqual(all_snippets.returncode, 0, all_snippets.stderr)
        self.assertIn("Claude Code Monitor", all_snippets.stdout)
        self.assertIn("Copilot agentStop hook command", all_snippets.stdout)
        self.assertIn("Antigravity/local generic receive command", all_snippets.stdout)
        self.assertIn("Generic local agent receive command", all_snippets.stdout)

    def test_install_snippets_prints_daemon_autostart_snippets(self) -> None:
        launchd = self.run_peerpost("install-snippets", "--adapter", "launchd")
        self.assertEqual(launchd.returncode, 0, launchd.stderr)
        self.assertIn("macOS launchd user agent plist", launchd.stdout)
        self.assertIn("<key>PEERPOST_HOME</key>", launchd.stdout)
        self.assertIn(self.env["PEERPOST_HOME"], launchd.stdout)

        systemd = self.run_peerpost("install-snippets", "--adapter", "systemd")
        self.assertEqual(systemd.returncode, 0, systemd.stderr)
        self.assertIn("Linux systemd user unit", systemd.stdout)
        self.assertIn("ExecStart=", systemd.stdout)
        self.assertIn("peerpost.daemon --foreground", systemd.stdout)

    def test_daemon_install_autostart_writes_and_removes_launchd_file(self) -> None:
        env = {**self.env, "HOME": self.tmp.name}
        plist = Path(self.tmp.name) / "Library" / "LaunchAgents" / "local.peerpost.peerpostd.plist"

        install = self.run_peerpost(
            "daemon",
            "install-autostart",
            "--target",
            "launchd",
            env=env,
        )
        self.assertEqual(install.returncode, 0, install.stderr)
        self.assertIn(f"installed launchd autostart: {plist}", install.stdout)
        self.assertIn("enable with: launchctl bootstrap", install.stdout)
        self.assertTrue(plist.exists())
        content = plist.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("<?xml"))
        self.assertIn("<key>PEERPOST_HOME</key>", content)
        self.assertIn(self.env["PEERPOST_SOCKET"], content)
        self.assertNotIn("# Save as:", content)

        duplicate = self.run_peerpost(
            "daemon",
            "install-autostart",
            "--target",
            "launchd",
            env=env,
        )
        self.assertEqual(duplicate.returncode, 1)
        self.assertIn("pass --overwrite", duplicate.stderr)

        overwrite = self.run_peerpost(
            "daemon",
            "install-autostart",
            "--target",
            "launchd",
            "--overwrite",
            env=env,
        )
        self.assertEqual(overwrite.returncode, 0, overwrite.stderr)

        uninstall = self.run_peerpost(
            "daemon",
            "uninstall-autostart",
            "--target",
            "launchd",
            env=env,
        )
        self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
        self.assertIn(f"removed launchd autostart: {plist}", uninstall.stdout)
        self.assertIn("also run: launchctl bootout gui/$(id -u)/local.peerpost.peerpostd", uninstall.stdout)
        self.assertFalse(plist.exists())

    def test_install_snippets_shell_quotes_agent_and_team_arguments(self) -> None:
        result = self.run_peerpost(
            "install-snippets",
            "--adapter",
            "generic",
            "--agent",
            "code reviewer",
            "--team",
            "dev team",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--agent 'code reviewer'", result.stdout)
        self.assertIn("--team 'dev team'", result.stdout)

    def test_leave_unregisters_agent_and_excludes_from_broadcast(self) -> None:
        self.run_peerpost("join", "--agent", "claude", "--type", "claude-code", "--team", "dev")
        self.run_peerpost("join", "--agent", "codex", "--type", "codex", "--team", "dev")
        self.run_peerpost("join", "--agent", "copilot", "--type", "copilot", "--team", "dev")

        left = self.run_peerpost("leave", "--agent", "copilot", "--team", "dev")
        self.assertEqual(left.returncode, 0, left.stderr)
        self.assertIn("left copilot from team dev", left.stdout)

        agents = self.run_peerpost("agents", "--team", "dev")
        self.assertEqual(agents.returncode, 0, agents.stderr)
        self.assertIn("dev/claude", agents.stdout)
        self.assertIn("dev/codex", agents.stdout)
        self.assertNotIn("dev/copilot", agents.stdout)

        sent = self.run_peerpost(
            "send", "--from", "claude", "--broadcast", "--team", "dev", "broadcast"
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertIn("to codex", sent.stdout)
        self.assertNotIn("copilot", sent.stdout)

    def test_broadcast_rejects_empty_recipient_set(self) -> None:
        self.run_peerpost("join", "--agent", "claude", "--type", "claude-code", "--team", "dev")

        sent = self.run_peerpost(
            "send",
            "--from",
            "claude",
            "--broadcast",
            "--team",
            "dev",
            "nobody is listening",
        )

        self.assertEqual(sent.returncode, 1)
        self.assertIn("no registered broadcast recipients in team dev", sent.stderr)
        db = sqlite3.connect(Path(self.env["PEERPOST_HOME"]) / "peerpost.sqlite")
        try:
            count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            db.close()
        self.assertEqual(count, 0)

    def test_send_rejects_self_delivery_without_storing_message(self) -> None:
        self.run_peerpost("join", "--agent", "claude", "--type", "claude-code", "--team", "dev")

        sent = self.run_peerpost(
            "send",
            "--from",
            "claude",
            "--to",
            "claude",
            "--team",
            "dev",
            "self note",
        )

        self.assertEqual(sent.returncode, 1)
        self.assertIn("message must have at least one delivery target", sent.stderr)
        db = sqlite3.connect(Path(self.env["PEERPOST_HOME"]) / "peerpost.sqlite")
        try:
            count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            db.close()
        self.assertEqual(count, 0)

    def test_core_commands_support_json_output(self) -> None:
        joined = self.run_peerpost(
            "join",
            "--agent",
            "claude",
            "--type",
            "claude-code",
            "--team",
            "dev",
            "--format",
            "json",
        )
        self.assertEqual(joined.returncode, 0, joined.stderr)
        self.assertEqual(json.loads(joined.stdout)["id"], "claude")
        self.run_peerpost("join", "--agent", "codex", "--type", "codex", "--team", "dev")

        agents = self.run_peerpost("agents", "--team", "dev", "--format", "json")
        self.assertEqual(agents.returncode, 0, agents.stderr)
        self.assertEqual([agent["id"] for agent in json.loads(agents.stdout)], ["claude", "codex"])

        sent = self.run_peerpost(
            "send",
            "--from",
            "claude",
            "--to",
            "codex",
            "--team",
            "dev",
            "--format",
            "json",
            "Question",
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        sent_payload = json.loads(sent.stdout)
        message_id = sent_payload["message"]["id"]
        self.assertEqual(sent_payload["targets"], ["codex"])

        inbox = self.run_peerpost("inbox", "--agent", "codex", "--team", "dev", "--format", "json")
        self.assertEqual(inbox.returncode, 0, inbox.stderr)
        self.assertEqual(json.loads(inbox.stdout)[0]["id"], message_id)

        read = self.run_peerpost(
            "read", message_id, "--agent", "codex", "--team", "dev", "--format", "json"
        )
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertEqual(json.loads(read.stdout)["id"], message_id)

        ack = self.run_peerpost(
            "ack", message_id, "--agent", "codex", "--team", "dev", "--format", "json"
        )
        self.assertEqual(ack.returncode, 0, ack.stderr)
        self.assertEqual(json.loads(ack.stdout)["updated"], 1)

        done = self.run_peerpost(
            "done", message_id, "--agent", "codex", "--team", "dev", "--format", "json"
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["updated"], 1)

        left = self.run_peerpost("leave", "--agent", "codex", "--team", "dev", "--format", "json")
        self.assertEqual(left.returncode, 0, left.stderr)
        self.assertTrue(json.loads(left.stdout)["removed"])

    def test_status_reports_agent_delivery_counts(self) -> None:
        self.run_peerpost("join", "--agent", "claude", "--type", "claude-code", "--team", "dev")
        self.run_peerpost("join", "--agent", "codex", "--type", "codex", "--team", "dev")
        self.run_peerpost("send", "--from", "claude", "--to", "codex", "--team", "dev", "pending")
        self.run_peerpost("send", "--from", "claude", "--to", "cdoex", "--team", "dev", "typo")

        plain = self.run_peerpost("status", "--team", "dev")
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertIn("team: dev", plain.stdout)
        self.assertIn("codex", plain.stdout)
        self.assertIn("pending", plain.stdout)
        self.assertIn("unregistered recipients:", plain.stdout)
        self.assertIn("cdoex: pending=1", plain.stdout)

        result = self.run_peerpost("status", "--team", "dev", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        codex = next(agent for agent in data["agents"] if agent["id"] == "codex")
        self.assertEqual(codex["pending"], 1)
        self.assertEqual(data["unregistered"][0]["to_agent"], "cdoex")

    def test_inbox_plain_output_shows_delivered_status(self) -> None:
        self.run_peerpost("join", "--agent", "claude", "--type", "claude-code", "--team", "dev")
        self.run_peerpost("join", "--agent", "codex", "--type", "codex", "--team", "dev")
        self.run_peerpost("send", "--from", "claude", "--to", "codex", "--team", "dev", "check status")
        self.run_peerpost("drain", "--agent", "codex", "--team", "dev")

        inbox = self.run_peerpost("inbox", "--agent", "codex", "--team", "dev")

        self.assertEqual(inbox.returncode, 0, inbox.stderr)
        self.assertIn("status=delivered", inbox.stdout)
        self.assertIn("check status", inbox.stdout)

    def test_setup_reports_paths_daemon_and_snippets(self) -> None:
        result = self.run_peerpost("setup", "--team", "dev", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["team"], "dev")
        self.assertEqual(data["home"], self.env["PEERPOST_HOME"])
        self.assertIn("pid", data["daemon"])
        self.assertTrue(any(item["adapter"] == "codex" for item in data["snippets"]))

    def test_quickstart_registers_defaults_and_prints_minimal_next_steps(self) -> None:
        result = self.run_peerpost("quickstart", "--team", "dev", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["team"], "dev")
        self.assertTrue(data["self_test"]["ok"])
        self.assertIn("pid", data["daemon"])
        registered = {(agent["id"], agent["agent_type"]) for agent in data["registered_agents"]}
        self.assertEqual(
            registered,
            {
                ("claude", "claude-code"),
                ("codex", "codex"),
                ("copilot", "copilot"),
            },
        )
        snippets = "\n".join(item["snippet"] for item in data["snippets"])
        self.assertIn("Claude Code Monitor", snippets)
        self.assertIn("Codex Stop hook command", snippets)
        self.assertIn("Copilot agentStop hook command", snippets)
        self.assertEqual(len(data["try_commands"]), 2)

        plain = self.run_peerpost("quickstart", "--team", "dev")
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertIn("peerpost quickstart", plain.stdout)
        self.assertIn("optional shell defaults:", plain.stdout)
        self.assertIn("export PEERPOST_TEAM=dev", plain.stdout)
        self.assertIn("configure receiving:", plain.stdout)
        self.assertIn("peerpost send --from claude --to codex --team dev", plain.stdout)

    def test_env_defaults_reduce_required_team_and_agent_flags(self) -> None:
        old_team = self.env.get("PEERPOST_TEAM")
        old_agent = self.env.get("PEERPOST_AGENT")
        try:
            self.env["PEERPOST_TEAM"] = "dev"
            self.env["PEERPOST_AGENT"] = "claude"
            joined_claude = self.run_peerpost("join", "--type", "claude-code")
            self.assertEqual(joined_claude.returncode, 0, joined_claude.stderr)

            self.env["PEERPOST_AGENT"] = "codex"
            joined_codex = self.run_peerpost("join", "--type", "codex")
            self.assertEqual(joined_codex.returncode, 0, joined_codex.stderr)

            self.env["PEERPOST_AGENT"] = "claude"
            sent = self.run_peerpost("send", "--to", "codex", "env default hello")
            self.assertEqual(sent.returncode, 0, sent.stderr)
            self.assertIn("sent msg_", sent.stdout)

            self.env["PEERPOST_AGENT"] = "codex"
            drained = self.run_peerpost("drain")
            self.assertEqual(drained.returncode, 0, drained.stderr)
            self.assertIn("claude -> codex", drained.stdout)
            self.assertIn("env default hello", drained.stdout)

            history = self.run_peerpost("history", "--format", "json")
            self.assertEqual(history.returncode, 0, history.stderr)
            self.assertEqual(json.loads(history.stdout)[0]["to_agent"], "codex")
        finally:
            if old_team is None:
                self.env.pop("PEERPOST_TEAM", None)
            else:
                self.env["PEERPOST_TEAM"] = old_team
            if old_agent is None:
                self.env.pop("PEERPOST_AGENT", None)
            else:
                self.env["PEERPOST_AGENT"] = old_agent

    def test_setup_can_include_daemon_autostart_snippet(self) -> None:
        result = self.run_peerpost(
            "setup",
            "--team",
            "dev",
            "--daemon-snippet",
            "launchd",
            "--format",
            "json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertIn("macOS launchd user agent plist", data["daemon_snippet"])
        self.assertIn(self.env["PEERPOST_SOCKET"], data["daemon_snippet"])

    def test_setup_registers_default_agents(self) -> None:
        result = self.run_peerpost(
            "setup",
            "--team",
            "dev",
            "--register-default-agents",
            "--format",
            "json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        registered = {(agent["id"], agent["agent_type"]) for agent in data["registered_agents"]}
        self.assertEqual(
            registered,
            {
                ("claude", "claude-code"),
                ("codex", "codex"),
                ("copilot", "copilot"),
            },
        )

        agents = self.run_peerpost("agents", "--team", "dev", "--format", "json")
        self.assertEqual(agents.returncode, 0, agents.stderr)
        agent_ids = {agent["id"] for agent in json.loads(agents.stdout)}
        self.assertGreaterEqual(agent_ids, {"claude", "codex", "copilot"})

    def test_setup_registers_custom_agent_with_inferred_type(self) -> None:
        result = self.run_peerpost(
            "setup",
            "--team",
            "dev",
            "--register",
            "reviewer",
            "--format",
            "json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(
            [(agent["id"], agent["agent_type"]) for agent in data["registered_agents"]],
            [("reviewer", "generic")],
        )

    def test_prune_dry_run_and_apply(self) -> None:
        self.run_peerpost("join", "--agent", "claude", "--type", "claude-code", "--team", "dev")
        self.run_peerpost("join", "--agent", "codex", "--type", "codex", "--team", "dev")
        sent = self.run_peerpost(
            "send",
            "--from",
            "claude",
            "--to",
            "codex",
            "--team",
            "dev",
            "--format",
            "json",
            "done work",
        )
        message_id = json.loads(sent.stdout)["message"]["id"]
        self.run_peerpost("done", message_id, "--agent", "codex", "--team", "dev")

        db_path = Path(self.env["PEERPOST_HOME"]) / "peerpost.sqlite"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE messages SET created_at = ? WHERE id = ?",
                ("2020-01-01T00:00:00Z", message_id),
            )
            conn.commit()
        finally:
            conn.close()

        dry_run = self.run_peerpost(
            "prune",
            "--team",
            "dev",
            "--before",
            "2021-01-01T00:00:00Z",
            "--format",
            "json",
        )
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        dry_payload = json.loads(dry_run.stdout)
        self.assertTrue(dry_payload["dry_run"])
        self.assertEqual(dry_payload["matched"], 1)
        self.assertEqual(dry_payload["deleted"], 0)

        applied = self.run_peerpost(
            "prune",
            "--team",
            "dev",
            "--before",
            "2021-01-01T00:00:00Z",
            "--apply",
            "--format",
            "json",
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        apply_payload = json.loads(applied.stdout)
        self.assertFalse(apply_payload["dry_run"])
        self.assertEqual(apply_payload["deleted"], 1)

    def test_prune_rejects_unsafe_cutoffs_and_limits(self) -> None:
        cases = [
            (("--older-than-days", "0"), "--older-than-days must be 1 or greater"),
            (("--limit", "0"), "--limit must be 1 or greater"),
            (("--before", "not-a-time"), "--before must be a valid UTC ISO timestamp"),
            (("--before", "2999-01-01T00:00:00Z"), "--before must be in the past"),
        ]
        for extra_args, error in cases:
            with self.subTest(extra_args=extra_args):
                result = self.run_peerpost("prune", *extra_args)
                self.assertEqual(result.returncode, 2)
                self.assertIn(error, result.stderr)

    def test_drain_and_logs_reject_unsafe_limits(self) -> None:
        drain = self.run_peerpost("drain", "--agent", "codex", "--team", "dev", "--limit", "0")
        self.assertEqual(drain.returncode, 2)
        self.assertIn("--limit must be 1 or greater", drain.stderr)

        hook = self.run_peerpost(
            "drain",
            "--agent",
            "codex",
            "--team",
            "dev",
            "--limit",
            "-1",
            "--format",
            "codex-hook",
        )
        self.assertEqual(hook.returncode, 2)
        self.assertEqual(hook.stdout, "")
        self.assertIn("--limit must be 1 or greater", hook.stderr)

        logs = self.run_peerpost("logs", "--tail", "-1")
        self.assertEqual(logs.returncode, 2)
        self.assertIn("--tail must be 0 or greater", logs.stderr)

    def test_daemon_rejects_unsafe_protocol_limits(self) -> None:
        peerpost = PeerpostClient(socket_path=self.env["PEERPOST_SOCKET"])
        with self.assertRaisesRegex(PeerpostClientError, "limit must be 1 or greater"):
            peerpost.request("drain", agent="codex", team="dev", limit=-1)
        with self.assertRaisesRegex(PeerpostClientError, "limit must be an integer"):
            peerpost.request("prune", before="9999-01-01T00:00:00Z", limit="many")

    def test_daemon_rejects_malformed_protocol_fields(self) -> None:
        peerpost = PeerpostClient(socket_path=self.env["PEERPOST_SOCKET"])

        cases = [
            (
                {"type": "history"},
                "missing required field: team",
            ),
            (
                {
                    "type": "send",
                    "from_agent": "claude",
                    "to_agent": "codex",
                    "team": "dev",
                    "body": "hello",
                    "broadcast": "false",
                },
                "broadcast must be a boolean",
            ),
            (
                {"type": "ack", "message_ids": "msg_missing", "agent": "codex", "team": "dev"},
                "message_ids must be a list of strings",
            ),
            (
                {"type": "backup", "output": 123},
                "output must be a string",
            ),
            (
                {
                    "type": "send",
                    "from_agent": "claude",
                    "to_agent": "codex",
                    "team": "dev",
                    "body": "hello",
                    "priority": "later",
                },
                "priority must be one of",
            ),
        ]
        for payload, error in cases:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(PeerpostClientError, error):
                    request_type = payload["type"]
                    request_payload = {key: value for key, value in payload.items() if key != "type"}
                    peerpost.request(request_type, **request_payload)

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(self.env["PEERPOST_SOCKET"])
            file = sock.makefile("rwb")
            file.write(
                encode_json_line(
                    {
                        "id": "req_subscribe_bad",
                        "type": "subscribe",
                        "agent": "codex",
                        "team": "dev",
                        "include_backlog": "true",
                    }
                )
            )
            file.flush()
            response = decode_json_line(file.readline())
        self.assertFalse(response["ok"])
        self.assertIn("include_backlog must be a boolean", response["error"]["message"])

    def test_backup_creates_readable_sqlite_snapshot(self) -> None:
        self.run_peerpost("join", "--agent", "claude", "--type", "claude-code", "--team", "dev")
        self.run_peerpost("join", "--agent", "codex", "--type", "codex", "--team", "dev")
        sent = self.run_peerpost(
            "send",
            "--from",
            "claude",
            "--to",
            "codex",
            "--team",
            "dev",
            "--format",
            "json",
            "snapshot me",
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        message_id = json.loads(sent.stdout)["message"]["id"]
        backup_path = Path(self.tmp.name) / "manual-backup.sqlite"

        backup = self.run_peerpost(
            "backup",
            "--output",
            str(backup_path),
            "--format",
            "json",
        )

        self.assertEqual(backup.returncode, 0, backup.stderr)
        payload = json.loads(backup.stdout)
        self.assertEqual(payload["path"], str(backup_path.resolve()))
        self.assertGreater(payload["bytes"], 0)
        conn = sqlite3.connect(backup_path)
        try:
            row = conn.execute("SELECT body FROM messages WHERE id = ?", (message_id,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "snapshot me")

    def test_daemon_rejects_oversized_message_body(self) -> None:
        peerpost = PeerpostClient(socket_path=self.env["PEERPOST_SOCKET"])
        with self.assertRaisesRegex(PeerpostClientError, f"body exceeds {MAX_BODY_CHARS}"):
            peerpost.request(
                "send",
                from_agent="claude",
                to_agent="codex",
                team="dev",
                body="x" * (MAX_BODY_CHARS + 1),
            )

    def test_subscribe_receives_message_sent_after_subscription(self) -> None:
        self.run_peerpost("join", "--agent", "claude", "--type", "claude-code", "--team", "dev")
        self.run_peerpost("join", "--agent", "codex", "--type", "codex", "--team", "dev")
        subscriber = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "peerpost.cli",
                "subscribe",
                "--agent",
                "codex",
                "--team",
                "dev",
                "--format",
                "monitor",
            ],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: subscriber.terminate() if subscriber.poll() is None else None)
        time.sleep(0.3)
        sent = self.run_peerpost(
            "send", "--from", "claude", "--to", "codex", "--team", "dev", "Realtime ping"
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        ready, _, _ = select.select([subscriber.stdout], [], [], 3)
        self.assertTrue(ready, "subscriber did not emit a message")
        line = subscriber.stdout.readline()
        self.assertIn("peerpost |", line)
        self.assertIn("msg_", line)
        self.assertIn("claude \u2192 codex", line)
        self.assertIn("Realtime ping", line)
        subscriber.terminate()
        subscriber.wait(timeout=3)
        if subscriber.stdout:
            subscriber.stdout.close()
        if subscriber.stderr:
            subscriber.stderr.close()


if __name__ == "__main__":
    unittest.main()
