#!/usr/bin/env python3
"""Static contracts for the app controls in Panel.qml."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


PANEL = Path(__file__).resolve().parents[1] / "Panel.qml"


def text_blocks(source: str) -> list[tuple[int, str]]:
    """(line, body) of every QML `Text { ... }`, nested blocks included."""
    blocks = []
    for match in re.finditer(r"\bText\s*\{", source):
        depth, end = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(source[end], 0)
            end += 1
        blocks.append((source.count("\n", 0, match.start()) + 1,
                       source[match.end():end - 1]))
    return blocks


class PanelCases(unittest.TestCase):
    def test_every_text_is_plain_so_titles_cannot_inject_markup(self) -> None:
        # Marketplace review (omarchy-plugin-marketplace#7522): a saved window
        # title reaches a Text with the default AutoText format, so a title
        # holding <img src=...> made the panel fetch a remote image.
        source = PANEL.read_text(encoding="utf-8")
        blocks = text_blocks(source)
        self.assertGreater(len(blocks), 10)
        for line, body in blocks:
            with self.subTest(line=line):
                self.assertRegex(body, r"\btextFormat:\s*Text\.PlainText\b")

    def test_a_preview_that_cannot_load_goes_back_to_the_current_save(self) -> None:
        # Review of PR #5: thinning can delete the history snapshot the Layout
        # tab previews; every refresh then retried the dead name and the tab
        # stayed on "Nothing saved to draw yet" with no way back.
        source = PANEL.read_text(encoding="utf-8")
        match = re.search(r"id:\s*layoutProc\b(?P<body>.*?)\n  \}\n", source, re.DOTALL)
        self.assertIsNotNone(match)
        failure = re.search(r"if \(!root\.layoutData\)\s*\{(?P<branch>.*?)\n\s*\}",
                            match.group("body"), re.DOTALL)
        self.assertIsNotNone(failure, "no failure branch after loading a layout")
        self.assertIn('root.layoutName !== "last"', failure.group("branch"))
        self.assertIn('loadLayout("last")', failure.group("branch"))

    def test_app_toggles_are_blocked_when_the_cli_is_unavailable(self) -> None:
        source = PANEL.read_text(encoding="utf-8")
        for toggle_id in ("chromeToggle", "ghosttyToggle"):
            match = re.search(
                rf"id:\s*{toggle_id}\b(?P<body>.*?)(?=\n\s*foreground:)",
                source,
                re.DOTALL,
            )
            self.assertIsNotNone(match, toggle_id)
            enabled = re.search(r"\benabled:\s*(?P<expr>.*)$",
                                match.group("body"), re.DOTALL)
            self.assertIsNotNone(enabled, toggle_id)
            expression = enabled.group("expr")
            self.assertIn("!root.mockMode", expression, toggle_id)
            self.assertIn("!root.cliMissing", expression, toggle_id)


if __name__ == "__main__":
    unittest.main()
