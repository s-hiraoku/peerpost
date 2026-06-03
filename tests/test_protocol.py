from __future__ import annotations

import json
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

from peerpost.formatters import HOOK_SAFETY_PREAMBLE, format_hook, format_monitor, format_plain
from peerpost.protocol import ProtocolError, decode_json_line, encode_json_line


ROOT = Path(__file__).resolve().parents[1]


class ProtocolTest(unittest.TestCase):
    def test_round_trip_json_line(self) -> None:
        payload = {"id": "req_1", "type": "send", "body": "hello"}
        self.assertEqual(decode_json_line(encode_json_line(payload)), payload)

    def test_cli_version(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "peerpost.cli", "--version"],
            cwd=ROOT,
            env={"PYTHONPATH": str(ROOT / "src")},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "peerpost 1.0.0")

    def test_project_metadata_includes_license_file(self) -> None:
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(metadata["project"]["license"]["file"], "LICENSE")
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("MIT License", license_text)
        self.assertIn("Permission is hereby granted", license_text)

    def test_ci_workflow_runs_tests_and_wheel_build(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
        self.assertIn("python -m compileall -q src tests", workflow)
        self.assertIn("python -m unittest discover -v", workflow)
        self.assertIn("python -m pip wheel . --no-deps", workflow)

    def test_release_checklist_documents_runtime_smoke_test(self) -> None:
        checklist = (ROOT / "docs" / "release.md").read_text(encoding="utf-8")
        self.assertIn("peerpost doctor --self-test", checklist)
        self.assertIn("peerpost status --team dev", checklist)
        self.assertIn("peerpost backup", checklist)
        self.assertIn("peerpost daemon stop", checklist)

    def test_usage_guide_documents_daily_workflow(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        guide = (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
        self.assertIn("[Usage Guide](docs/usage.md)", readme)
        self.assertIn("peerpost quickstart", guide)
        self.assertIn("export PEERPOST_TEAM=dev", guide)
        self.assertIn("peerpost drain", guide)
        self.assertIn("peerpost status", guide)
        self.assertIn("--stdin", guide)
        self.assertIn("plain output includes a `msg_...` id", guide)
        self.assertIn("unique message id prefix", guide)

    def test_decode_rejects_non_object_json(self) -> None:
        with self.assertRaises(ProtocolError):
            decode_json_line("[1, 2, 3]\n")

    def test_codex_hook_outputs_empty_object_when_no_messages(self) -> None:
        self.assertEqual(format_hook([]), "{}")

    def test_codex_hook_outputs_decision_block_with_messages(self) -> None:
        output = format_hook(
            [
                {
                    "id": "msg_1",
                    "team": "dev",
                    "from_agent": "claude",
                    "to_agent": "codex",
                    "body": "please review",
                    "created_at": "2026-05-30T12:34:56Z",
                }
            ]
        )
        payload = json.loads(output)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("msg_1", payload["reason"])
        self.assertIn("please review", payload["reason"])

    def test_hook_formatter_includes_safety_preamble(self) -> None:
        output = format_hook(
            [
                {
                    "id": "msg_1",
                    "team": "dev",
                    "from_agent": "claude",
                    "to_agent": "codex",
                    "body": "hello",
                    "created_at": "2026-05-30T12:34:56Z",
                }
            ]
        )
        self.assertTrue(json.loads(output)["reason"].startswith(HOOK_SAFETY_PREAMBLE))

    def test_control_characters_are_stripped_from_hook_output(self) -> None:
        output = format_hook(
            [
                {
                    "id": "msg_1",
                    "team": "dev",
                    "from_agent": "claude",
                    "to_agent": "codex",
                    "body": "\x1b[31mred\x1b[0m\x07",
                    "created_at": "2026-05-30T12:34:56Z",
                }
            ]
        )
        reason = json.loads(output)["reason"]
        self.assertIn("red", reason)
        self.assertNotIn("\x1b", reason)
        self.assertNotIn("\x07", reason)

    def test_terminal_formatters_sanitize_message_metadata_fields(self) -> None:
        message = {
            "id": "msg_1",
            "team": "dev\x1b[0m\nforged",
            "from_agent": "\x1b[31mclaude\x1b[0m\nforged",
            "to_agent": "codex\x07",
            "body": "safe body",
            "created_at": "2026-05-30T12:34:56Z\x1b[0m",
            "priority": "urgent\nforged",
            "kind": "message",
            "parent_id": "msg_parent\nforged",
        }

        plain = format_plain([message])
        monitor = format_monitor(message)
        reason = json.loads(format_hook([message]))["reason"]

        for output in (plain, monitor, reason):
            self.assertNotIn("\x1b", output)
            self.assertNotIn("\x07", output)
            self.assertNotIn("claude\nforged", output)
            self.assertNotIn("urgent\nforged", output)
            self.assertNotIn("msg_parent\nforged", output)
        self.assertNotIn("dev\nforged", monitor)
        self.assertIn("claude forged -> codex", plain)
        self.assertIn("dev forged", monitor)
        self.assertIn("priority=urgent forged", reason)

    def test_plain_and_monitor_formats_include_message_id(self) -> None:
        message = {
            "id": "msg_20260604T010203456Z_a1b2c3",
            "team": "dev",
            "from_agent": "claude",
            "to_agent": "codex",
            "body": "please review",
            "created_at": "2026-06-04T01:02:03Z",
            "priority": "normal",
        }

        self.assertIn("msg_20260604T010203456Z_a1b2c3", format_plain([message]))
        self.assertIn("msg_20260604T010203456Z_a1b2c3", format_monitor(message))


if __name__ == "__main__":
    unittest.main()
