from __future__ import annotations

import json
import os
import select
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from peerpost.client import DaemonNotRunning, PeerpostClient, PeerpostClientError
from peerpost.protocol import MAX_BODY_CHARS


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

    def run_peerpost(self, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "peerpost.cli", *args],
            env=self.env,
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
        self.assertIn("claude -> codex: Please review the auth middleware.", drained.stdout)

        second = self.run_peerpost(
            "drain", "--agent", "codex", "--team", "dev", "--format", "plain"
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout, "")

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
        self.assertIn("status: ok", result.stdout)

    def test_doctor_json_reports_actionable_checks(self) -> None:
        result = self.run_peerpost("doctor", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["paths"]["home"], self.env["PEERPOST_HOME"])
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["daemon"]["status"], "ok")
        self.assertEqual(checks["socket"]["status"], "ok")
        self.assertIn("log", checks)

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
        db_path.chmod(0o644)

        result = self.run_peerpost("doctor", "--fix", "--format", "json")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertIn(f"chmod 600 {db_path}", report["repairs"])
        self.assertEqual(db_path.stat().st_mode & 0o777, 0o600)
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["database"]["status"], "ok")

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

    def test_setup_reports_paths_daemon_and_snippets(self) -> None:
        result = self.run_peerpost("setup", "--team", "dev", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["team"], "dev")
        self.assertEqual(data["home"], self.env["PEERPOST_HOME"])
        self.assertIn("pid", data["daemon"])
        self.assertTrue(any(item["adapter"] == "codex" for item in data["snippets"]))

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
