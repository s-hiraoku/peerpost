from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from peerpost.client import DaemonNotRunning, PeerpostClient


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

    def test_doctor_reports_running_daemon(self) -> None:
        result = self.run_peerpost("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("home: ok:", result.stdout)
        self.assertIn("daemon: ok: running pid", result.stdout)
        self.assertIn("status: ok", result.stdout)

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
