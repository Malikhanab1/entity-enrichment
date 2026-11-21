"""Regression coverage for CLI response selection."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import (  # noqa: E402
    _current_turn_messages,
    _final_text_response,
    _last_tool_summary,
)


def message(
    message_type: str,
    content: str,
    *,
    tool_calls: list[object] | None = None,
    name: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        type=message_type,
        content=content,
        tool_calls=tool_calls or [],
        name=name,
    )


class FinalResponseTests(unittest.TestCase):
    def test_skips_empty_intermediate_tool_call_message(self) -> None:
        intermediate = message("ai", "", tool_calls=[{"name": "write_text_file"}])
        final = message("ai", "Saved the decision-maker research to wissen.md.")

        self.assertIs(_final_text_response([intermediate, final]), final)

    def test_empty_terminal_response_is_not_printed_as_an_answer(self) -> None:
        intermediate = message("ai", "", tool_calls=[{"name": "write_text_file"}])
        tool_result = message(
            "tool", '{"status": "written", "path": "wissen.md"}', name="write_text_file"
        )
        empty_final = message("ai", "")

        self.assertIsNone(_final_text_response([intermediate, tool_result, empty_final]))
        summary = _last_tool_summary([intermediate, tool_result, empty_final])
        self.assertIn("write_text_file", summary or "")
        self.assertIn("wissen.md", summary or "")

    def test_only_uses_messages_from_the_current_turn(self) -> None:
        old_answer = message("ai", "A prior answer that must not be repeated.")
        request = message("human", "Write the current answer.")
        empty_final = message("ai", "")

        current_turn = _current_turn_messages([old_answer, request, empty_final])
        self.assertEqual(current_turn, [empty_final])
        self.assertIsNone(_final_text_response(current_turn))


if __name__ == "__main__":
    unittest.main()
