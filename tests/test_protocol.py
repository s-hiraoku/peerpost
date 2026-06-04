from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

from peerpost.cli import build_parser
from peerpost.formatters import HOOK_SAFETY_PREAMBLE, format_hook, format_monitor, format_plain
from peerpost.protocol import ProtocolError, decode_json_line, encode_json_line


ROOT = Path(__file__).resolve().parents[1]


def _parser_choices(parser: object) -> dict[str, argparse.ArgumentParser]:
    for action in getattr(parser, "_actions", []):
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict) and choices:
            return choices
    raise AssertionError("parser does not define subcommands")


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

    def test_gitignore_excludes_local_runtime_artifacts(self) -> None:
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".DS_Store", gitignore)
        self.assertIn("peerpost.sqlite*", gitignore)
        self.assertIn("peerpost.pid", gitignore)
        self.assertIn("peerpost.log", gitignore)
        self.assertIn("backups/", gitignore)

    def test_ci_workflow_runs_tests_and_wheel_build(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
        self.assertIn("python -m compileall -q src tests", workflow)
        self.assertIn("python -m unittest discover -v", workflow)
        self.assertIn("python -m pip wheel . --no-deps", workflow)

    def test_pages_workflow_publishes_user_guide(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
        index = (ROOT / "docs" / "index.md").read_text(encoding="utf-8")
        config = (ROOT / "docs" / "_config.yml").read_text(encoding="utf-8")
        self.assertIn("actions/jekyll-build-pages", workflow)
        self.assertIn("actions/deploy-pages", workflow)
        self.assertIn("source: ./docs", workflow)
        self.assertIn("# peerpost User Guide", index)
        self.assertIn("[Daily Usage](usage.md)", index)
        self.assertIn("[Agent Adapter Setup](adapters.md)", index)
        self.assertIn("[Command Reference](commands.md)", index)
        self.assertIn("theme: jekyll-theme-minimal", config)

    def test_command_reference_documents_cli_commands(self) -> None:
        reference = (ROOT / "docs" / "commands.md").read_text(encoding="utf-8")
        parser = build_parser()
        top_level = _parser_choices(parser)
        self.assertGreaterEqual(len(top_level), 20)
        for command in sorted(top_level):
            self.assertIn(f"peerpost {command}", reference)
        daemon = top_level["daemon"]
        for command in sorted(_parser_choices(daemon)):
            self.assertIn(f"peerpost daemon {command}", reference)

    def test_release_checklist_documents_runtime_smoke_test(self) -> None:
        checklist = (ROOT / "docs" / "release.md").read_text(encoding="utf-8")
        self.assertIn("peerpost doctor --self-test", checklist)
        self.assertIn("peerpost status --team dev", checklist)
        self.assertIn("peerpost backup", checklist)
        self.assertIn("peerpost restore --input <backup.sqlite>", checklist)
        self.assertIn("peerpost daemon stop", checklist)
        self.assertIn("Pages workflow publishes the user guide", checklist)

    def test_usage_guide_documents_daily_workflow(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        guide = (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
        self.assertIn("[Usage Guide](docs/usage.md)", readme)
        self.assertIn("[Command Reference](docs/commands.md)", readme)
        self.assertIn("[Command Reference](commands.md)", guide)
        self.assertIn("https://s-hiraoku.github.io/peerpost/", readme)
        self.assertIn("peerpost quickstart", guide)
        self.assertIn("peerpost daemon install-autostart", readme)
        self.assertIn("peerpost daemon install-autostart", guide)
        self.assertIn("stale pid/socket/autostart files", readme)
        self.assertIn("stale pid and socket files", guide)
        self.assertIn("private home directory", readme)
        self.assertIn("home directory permissions", guide)
        self.assertIn("backup directory", readme)
        self.assertIn("backup directory permissions", guide)
        self.assertIn("private log/database permissions", readme)
        self.assertIn("log file permissions", guide)
        self.assertIn("missing agent registrations", readme)
        self.assertIn("unregistered sender or recipient ids", readme)
        self.assertIn("invalid historical priority values", readme)
        self.assertIn("invalid delivery status values", readme)
        self.assertIn("messages without delivery rows", readme)
        self.assertIn("invalid metadata JSON", readme)
        self.assertIn("daemon autostart file", guide)
        self.assertIn("registered agents", guide)
        self.assertIn("unregistered sender and recipient ids", guide)
        self.assertIn("invalid historical priority values", guide)
        self.assertIn("invalid delivery status values", guide)
        self.assertIn("messages without delivery rows", guide)
        self.assertIn("invalid metadata JSON", guide)
        self.assertIn("SQLite sidecar", readme)
        self.assertIn("SQLite `quick_check`", readme)
        self.assertIn("SQLite `quick_check` and `foreign_key_check`", readme)
        self.assertIn("SQLite `quick_check`", guide)
        self.assertIn("SQLite `quick_check` and `foreign_key_check`", guide)
        self.assertIn("private database", readme)
        self.assertIn("Keep custom `PEERPOST_SOCKET` values short", readme)
        self.assertIn("`PEERPOST_SOCKET` is too long", guide)
        self.assertIn("client commands report `socket path too long`", readme)
        self.assertIn("Client commands also report `socket path too long`", guide)
        self.assertIn("undeliverable empty broadcast", readme)
        self.assertIn("Broadcast fails if no other agent is registered", guide)
        self.assertIn("Direct sends to yourself are rejected", guide)
        self.assertIn("sender or a direct recipient id is not registered", readme)
        self.assertIn("sender or direct recipient id is not registered", guide)
        self.assertIn("agent-list, and plain log output strips ANSI", readme)
        self.assertIn("future cutoffs", readme)
        self.assertIn("temporary file first", readme)
        self.assertIn("reports whether it was verified", readme)
        self.assertIn("SQLite `quick_check` and `foreign_key_check` on the backup", readme)
        self.assertIn("verification fails, the command exits nonzero", readme)
        self.assertIn("peerpost restore --input ~/peerpost-backup.sqlite", readme)
        self.assertIn("`restore` refuses to run while `peerpostd` is responding", readme)
        self.assertIn(
            "verifies the snapshot with SQLite `quick_check` and `foreign_key_check`",
            guide,
        )
        self.assertIn("verification fails, it exits nonzero", guide)
        self.assertIn("peerpost restore --input ~/peerpost-backup.sqlite", guide)
        self.assertIn("Malformed field types return `bad_request`", readme)
        self.assertIn("numeric limits must be JSON numbers, not booleans", readme)
        self.assertIn("Priority values are `low`, `normal`, `high`, or `urgent`", readme)
        self.assertIn("Stored metadata must be a JSON object", readme)
        self.assertIn("logs --tail` must be zero or greater", guide)
        self.assertIn("drain --limit` and `prune --limit` must be positive", guide)
        self.assertIn("positive age or past `--before` timestamp", guide)
        self.assertIn("prune --team dev --older-than-days 30 --apply --backup-first", readme)
        self.assertIn("does not delete messages", guide)
        self.assertIn("--backup-output <path>` with `--backup-first", readme)
        self.assertIn("--backup-output <path>` with `--backup-first", guide)
        self.assertIn("export PEERPOST_TEAM=dev", guide)
        self.assertIn("peerpost drain", guide)
        self.assertIn("peerpost history", guide)
        self.assertIn("peerpost status", guide)
        self.assertIn("most recent pending message time", readme)
        self.assertIn("most recent pending message time", guide)
        self.assertIn("--stdin", guide)
        self.assertIn("`--stdin` rejects empty input", guide)
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

    def test_plain_format_shows_non_pending_delivery_status(self) -> None:
        message = {
            "id": "msg_1",
            "team": "dev",
            "from_agent": "claude",
            "to_agent": "codex",
            "body": "please review",
            "created_at": "2026-06-04T01:02:03Z",
            "priority": "normal",
            "status": "delivered",
        }
        delivered = format_plain([message])
        pending = format_plain([{**message, "status": "pending"}])

        self.assertIn("status=delivered", delivered)
        self.assertNotIn("status=pending", pending)


if __name__ == "__main__":
    unittest.main()
