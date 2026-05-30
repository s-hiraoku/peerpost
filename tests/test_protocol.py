from __future__ import annotations

import json
import unittest

from peerpost.formatters import HOOK_SAFETY_PREAMBLE, format_hook
from peerpost.protocol import ProtocolError, decode_json_line, encode_json_line


class ProtocolTest(unittest.TestCase):
    def test_round_trip_json_line(self) -> None:
        payload = {"id": "req_1", "type": "send", "body": "hello"}
        self.assertEqual(decode_json_line(encode_json_line(payload)), payload)

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


if __name__ == "__main__":
    unittest.main()
