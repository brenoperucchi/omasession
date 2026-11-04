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


    def test_apps_and_log_are_tabs_like_the_others(self) -> None:
        source = PANEL.read_text(encoding="utf-8")
        tabs = re.search(r"id:\s*viewTabs\b(?P<body>.*?)\n\s*\}\n", source, re.DOTALL)
        self.assertIsNotNone(tabs)
        values = re.findall(r'value:\s*"(\w+)"', tabs.group("body"))
        self.assertEqual(values, ["windows", "layout", "saved", "apps", "log"])
        # The app card shows only on its own tab, not above every view.
        card = re.search(r"Rectangle \{\s*visible:(?P<cond>[^\n]*)\n(?:(?!Rectangle \{).)*?id:\s*appsCol\b",
                         source, re.DOTALL)
        self.assertIsNotNone(card)
        self.assertIn('root.view === "apps"', card.group("cond"))
        self.assertNotIn("Restore Windows on Restart", source)

    def test_a_report_never_goes_through_a_shell_string(self) -> None:
        # A restore name and the report folder come from files on disk; they
        # reach the CLI and xdg-open as argv, never re-read by bash.
        source = PANEL.read_text(encoding="utf-8")
        self.assertRegex(source, r'reportProc\.command = \[root\.cliPath, "report", "--restore", name,')
        self.assertIn('Util.execArgv(["xdg-open", path])', source)
        self.assertNotIn("execDetached", source)
        self.assertNotIn("openUrlExternally", source)

    def test_the_log_tab_reads_the_cli_not_the_files(self) -> None:
        source = PANEL.read_text(encoding="utf-8")
        self.assertIn('command: [root.cliPath, "restores", "--json"]', source)
        self.assertNotIn("FileView", source)

    def test_the_restore_list_loads_only_while_the_panel_is_open(self) -> None:
        # Review omasession-50: the 30 s timer ran `restores --json` all day
        # for a dot and a header nobody could see with the panel closed.
        source = PANEL.read_text(encoding="utf-8")
        refresh = re.search(r"function refresh\(\) \{(?P<body>.*?)\n  \}", source, re.DOTALL)
        self.assertIsNotNone(refresh)
        self.assertIn("if (opened) loadRestores()", refresh.group("body"))
        self.assertRegex(source, r"readonly property bool attention:\s*guardRefused\n")

    def test_login_restore_off_outranks_a_bad_last_restore(self) -> None:
        source = PANEL.read_text(encoding="utf-8")
        self.assertLess(source.index('!root.restoreOnLogin ? "RESTORE ON LOGIN IS OFF"'),
                        source.index('root.lastRestoreBad ? "LAST RESTORE HAD PROBLEMS"'))

    def test_the_issue_link_opens_only_after_the_person_confirms(self) -> None:
        # Marketplace review (omarchy-plugin-marketplace#13759): Report a
        # problem opened the prefilled link at once, so GitHub and the browser
        # history got its text before the person had read it.
        source = PANEL.read_text(encoding="utf-8")
        exited = re.search(r"id:\s*reportProc\b.*?onExited: function\(code\) \{(?P<body>.*?)\n    \}\n",
                           source, re.DOTALL)
        self.assertIsNotNone(exited)
        self.assertNotIn("openPath(", exited.group("body"))
        self.assertIn("root.reportDraft = ", exited.group("body"))
        # The draft is on screen, says what opening sends, and the link opens
        # from one button only.
        self.assertIn("text: root.reportDraft", source)
        self.assertIn("before you submit", source)
        self.assertEqual(source.count("root.openPath(root.reportUrl)"), 1)
        confirm = re.search(r'text: "Open on GitHub"(?P<body>.*?)\n\s*\}', source, re.DOTALL)
        self.assertIsNotNone(confirm)
        self.assertIn("root.openPath(root.reportUrl)", confirm.group("body"))

if __name__ == "__main__":
    unittest.main()
