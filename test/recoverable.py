#!/usr/bin/env python3
"""How many windows the replay would actually restore from a session file.

Exists as its own file because inlining it as a heredoc inside a command
substitution, inside a loop, produced two separate quoting bugs -- and a test
harness that is hard to read is a test nobody trusts.

load_pair's diagnostics go to stdout by design (the user should see "using the
previous generation"), so they are redirected to stderr here and stdout carries
only the number.
"""

import contextlib
import importlib.util
import os
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("replay", os.environ["REPLAY"])
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)

with contextlib.redirect_stdout(sys.stderr):
    windows, _titles, _side = replay.load_pair(Path(sys.argv[1]))
print(len(windows))
