#!/usr/bin/env python3
"""Resolve a mapped window back to a command that reopens it.

This is the piece the whitelist approach cannot reach. `dimef.omaresume` keys
its launcher off eight known classes and returns nothing for anything else;
hyprresume does resolve arbitrary apps, but it has been unmaintained since March
2026 and is the last dependency this plugin has. Owning the resolver is the same
work as removing that dependency (docs/plans/003).

Four paths, tried in this order, because the cheap one is also the most
misleading:

  .desktop   the class is matched against StartupWMClass and against the entry
             id, and the Exec= line -- minus its field codes -- is the command.
             This is what brings back nautilus and obsidian with no user
             configuration at all.
  cgroup     systemd names the scope after the launcher, so a Flatpak reads as
             app-flatpak-<appid>-<pid>.scope. Needed because /proc/<pid>/cmdline
             for a Flatpak shows bwrap, not the application.
  cmdline    last resort. It is right for a plain binary and wrong in the two
             cases that matter most: a browser's line contains none of its URLs,
             and an Electron app shows the packaged binary's private path.
  (none)     saying so is a result. A window we cannot resolve must be reported,
             not silently dropped -- that is how a restore reports success it
             never earned.

Standard library only: this runs at login, before anything is guaranteed to be
installed.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

# Exec= field codes, to strip. %% is an escaped percent and is left alone here
# because it is handled before the split.
FIELD_CODES = re.compile(r'(?<!%)%[uUfFickvmdDnNs]')

# Terminals take their working directory as an argument. The class is what
# Hyprland reports; the flag is what the emulator accepts.
TERMINAL_CWD_FLAG = {
    "foot": "--working-directory",
    "Alacritty": "--working-directory",
    "kitty": "--directory",
    "com.mitchellh.ghostty": "--working-directory",
    "org.wezfurlong.wezterm": "--cwd",
}

# Terminals confirmed to accept a bare trailing command (`foot -- cmd args`,
# no -e/-- dialect to get wrong) -- a different fact than TERMINAL_CWD_FLAG's
# cwd-flag dialect, and not assumed to be the same set. `foot` measured
# directly (docs/plans/005): `foot -D dir -- tmux new` reattaches correctly.
# `kitty` is measured indirectly but just as concretely: every real kitty
# window in this project's own captures resolves via `cmdline` and relaunches
# correctly with its trailing args intact (README, "19/19", two custom kitty
# classes). Alacritty/ghostty/wezterm are not in this set because nothing in
# this project has ever launched one -- not because they are known to differ.
TRAILING_ARGV_TERMINALS = {"foot", "kitty"}

# Applications that own every one of their windows from a single process. There
# is no point launching one per saved window: the second invocation talks to the
# first and exits. The replay asks the app to restore its own windows instead.
SINGLE_INSTANCE = {
    "chromium", "google-chrome", "brave-browser", "vivaldi-stable",
    "microsoft-edge", "firefox", "code", "org.gnome.Nautilus",
}


def xdg(name: str, fallback: str) -> str:
    """An XDG variable, treating empty as unset -- which the spec requires.

    `os.environ.get(name, fallback)` returns "" for a variable that is set but
    empty, and "".split(":") yields nothing usable. Measured: with an empty
    XDG_DATA_DIRS the index drops from 139 entries to 32. That matters more than
    it looks, because the resolver runs at login, which is exactly the context
    where a minimal environment shows up.
    """
    value = os.environ.get(name, "")
    return value if value.strip() else fallback


def data_dirs() -> list[Path]:
    """XDG application directories, most specific first."""
    home = Path(xdg("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    system = xdg("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    dirs = [home] + [Path(p) for p in system.split(":") if p]
    # Flatpak exports live outside XDG_DATA_DIRS on some setups.
    dirs += [Path.home() / ".local/share/flatpak/exports/share",
             Path("/var/lib/flatpak/exports/share")]
    return [d / "applications" for d in dirs]


def parse_desktop(path: Path) -> dict | None:
    """The [Desktop Entry] fields we care about, or None if it is not usable."""
    entry, in_section = {}, False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            # Only the main section. Actions carry their own Exec= lines and
            # picking one of those up launches "New Window" instead of the app.
            in_section = line == "[Desktop Entry]"
            continue
        if not in_section or "=" not in line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        entry[key.strip()] = value.strip()
    if not entry.get("Exec"):
        return None
    if entry.get("Hidden", "").lower() == "true":
        return None
    if entry.get("Type", "Application") != "Application":
        return None
    return entry


# Quem reivindica uma classe, e com que autoridade. StartupWMClass é a entrada
# declarando "as janelas com esta classe são minhas"; o nome do arquivo apenas
# coincide com ela. Uma reivindicação declarada vence uma coincidência.
CLAIM_DECLARED, CLAIM_FILENAME = 2, 1


def desktop_index() -> dict[str, dict]:
    """Map lowercased class names to desktop entries.

    Two keys per entry, because neither alone is reliable: StartupWMClass is
    authoritative when present but most entries omit it, and the entry id
    matches the class for the majority that do.

    Precedence used to be "whichever file was read first", which is not
    authority -- it is alphabetical order wearing a costume. Now: a declared
    StartupWMClass beats a filename that merely coincides, and among equals the
    most specific data directory wins, so a user override in ~/.local/share
    beats /usr/share.

    Some collisions cannot be resolved from the data at all. On this machine
    both `com.rtosta.zapzap.desktop` and `com.rtosta.zapzap.nogpu.desktop`
    declare StartupWMClass=zapzap with different Exec lines, and neither is
    NoDisplay: there is nothing in the files that says which one owns the
    window. The pick stays deterministic, and the runners-up are recorded so
    `omasession resolve` can say the choice was a coin toss rather than present
    it as a fact.
    """
    index: dict[str, dict] = {}
    for depth, directory in enumerate(data_dirs()):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.desktop")):
            entry = parse_desktop(path)
            if entry is None:
                continue
            entry["_path"] = str(path)
            claims = [(path.stem, CLAIM_FILENAME)]
            if entry.get("StartupWMClass"):
                claims.append((entry["StartupWMClass"], CLAIM_DECLARED))
            for key, claim in claims:
                key = key.lower()
                current = index.get(key)
                if current is None:
                    index[key] = dict(entry, _claim=claim, _depth=depth, _rivals=[])
                    continue
                mine = (claim, -depth)
                theirs = (current["_claim"], -current["_depth"])
                # Rival só é quem empata em autoridade E propõe outro comando.
                # Uma entrada que perdeu por autoridade não é uma ambiguidade --
                # é precedência resolvida, e chamá-la de empate transformaria o
                # caso normal (override do usuário sobre o do sistema) em ruído.
                tie = mine == theirs and entry["Exec"] != current["Exec"]
                if mine > theirs:
                    index[key] = dict(entry, _claim=claim, _depth=depth,
                                      _rivals=current["_rivals"])
                elif tie:
                    current["_rivals"] = current["_rivals"] + [entry["Exec"]]
    return index


def exec_argv(exec_line: str) -> list[str]:
    """The Exec= line as argv, with field codes removed.

    A leftover %U turns into a literal argument the application then tries to
    open as a file, which is why this cannot just be shlex.split.
    """
    cleaned = FIELD_CODES.sub("", exec_line).replace("%%", "%")
    try:
        argv = shlex.split(cleaned)
    except ValueError:
        argv = cleaned.split()
    # env VAR=1 prog ... -- keep it; it is part of how the entry expects to run.
    return [a for a in argv if a]


def from_cgroup(pid: int) -> list[str] | None:
    """Flatpak app id from the systemd scope, when there is one.

    /proc/<pid>/cmdline for a Flatpak shows bwrap and its sandbox arguments;
    the scope name is the only place the application id survives.
    """
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return None
    match = re.search(r"app-flatpak-([A-Za-z0-9_.\-]+?)-\d+\.scope", text)
    if match:
        return ["flatpak", "run", match.group(1)]
    return None


def from_cmdline(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
    if not argv:
        return None
    return argv


# Processes whose cwd IS the answer, and processes whose cwd merely looks like
# one. A multiplexer client runs wherever it was launched; the shell the user is
# actually in belongs to the server, in a different process tree entirely.
SHELLS = {"bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh", "csh",
          "nu", "elvish", "xonsh", "ion"}
MULTIPLEXERS = {"tmux", "tmux: client", "screen", "abduco", "dtach", "zellij"}


def _comm(pid: str) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def _children(pid) -> list[str]:
    try:
        return Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
    except OSError:
        return []


def child_cwd(pid: int) -> tuple[str | None, str]:
    """The directory a terminal's shell is in, and why, or (None, reason).

    /proc/<pid>/cwd is the emulator's own -- wherever it was started, almost
    never what the user sees -- so the answer is in a descendant. But taking the
    first child is wrong in a way that is invisible: on this machine every kitty
    window's first children are `kitten` and `tmux: client`, all reporting the
    home directory, while the shells the user is actually in live inside the
    tmux server, in another tree. Restoring those windows to $HOME would look
    like a success.

    Nor is "the first thing that looks like a shell" enough, which is what an
    earlier version did. Two cases make it guess:

      * a terminal process serving more than one window has several sibling
        shells, in different directories, and a pid alone cannot say which
        window belongs to which -- so the first one is a coin toss;
      * a shell whose own child is a multiplexer client is sitting in whatever
        directory tmux was started from, not in the user's pane.

    Both now return no directory and a reason. An honest "not recovered" beats a
    confident wrong one: the wrong directory is indistinguishable from success
    until the user looks at the prompt.
    """
    candidates: list[tuple[str, str]] = []   # (cwd, comm)
    saw_mux = False
    seen, queue = set(), [(str(pid), 0)]
    while queue:
        current, depth = queue.pop(0)
        if current in seen or depth > 3:
            continue
        seen.add(current)
        for kid in _children(current):
            comm = _comm(kid)
            if comm in MULTIPLEXERS:
                saw_mux = True
                continue
            if comm in SHELLS:
                # Um shell que é pai de um multiplexador está no diretório de
                # onde o tmux foi lançado, não no painel em que o usuário está.
                if any(_comm(g) in MULTIPLEXERS for g in _children(kid)):
                    saw_mux = True
                    continue
                try:
                    candidates.append((os.readlink(f"/proc/{kid}/cwd"), comm))
                except OSError:
                    pass
                continue
            queue.append((kid, depth + 1))

    distinct = {cwd for cwd, _ in candidates}
    if len(distinct) == 1:
        return candidates[0][0], f"shell: {candidates[0][1]}"
    if len(distinct) > 1:
        return None, (f"{len(distinct)} shells under this terminal; "
                      f"cannot tell which one is this window")
    if saw_mux:
        return None, "cwd lives in the multiplexer server, not in this tree"
    return None, "no shell found under the terminal"


def tmux_session(pid: int) -> tuple[str | None, str | None, str]:
    """The tmux session a terminal's own client is attached to, or (None, None, why).

    Returns (session_name, pane_cwd, why). pane_cwd is tmux's own record of
    the attached client's active pane -- a bonus, not the point: the reattach
    itself is what matters, this just lets the panel say a real directory
    instead of "not recoverable" for a window that is, in fact, going to
    recover it.

    Measured 2026-09-10 (docs/plans/005): a terminal window whose content lives
    in a tmux client resolves today via .desktop/cmdline same as any other
    terminal -- the resolved command reopens the emulator, not the session, and
    a real reboot confirmed the result: two windows came back running a bare
    shell in $HOME, no tmux server even started. tmux itself already tracks
    which session a client is attached to; asking it is cheaper and more
    correct than inferring it from the process tree the way child_cwd() must
    for a plain cwd.

    Deliberately narrower than child_cwd(): only the default tmux socket is
    queried (a client on a different `-L`/`-S` socket is invisible to
    `tmux list-clients` here, same honest gap as the multiplexers child_cwd()
    does not attempt). A terminal serving more than one tmux client cannot say
    which one is this window's, same ambiguity and same refusal as child_cwd().
    """
    clients: list[str] = []
    seen, queue = set(), [(str(pid), 0)]
    while queue:
        current, depth = queue.pop(0)
        if current in seen or depth > 3:
            continue
        seen.add(current)
        for kid in _children(current):
            if _comm(kid) == "tmux: client":
                clients.append(kid)
                continue
            queue.append((kid, depth + 1))

    if not clients:
        return None, None, "no tmux client under this terminal"
    if len(clients) > 1:
        return None, None, (f"{len(clients)} tmux clients under this terminal; "
                            f"cannot tell which one is this window")

    try:
        # Tab, não espaço: tmux aceita espaço em nome de sessão (medido na
        # revisão desta rodada -- "Contratos Thera" é um nome real em uso
        # neste projeto), e um separador que o valor pode conter corrompe
        # tanto o nome quanto o cwd em silêncio. `#{pane_current_path}` em vez
        # de `#{session_path}`: o segundo é o diretório de LANÇAMENTO da
        # sessão, não o do painel ativo -- duas sessões reais aqui mostravam
        # `~` enquanto o painel ativo estava noutro lugar.
        out = subprocess.run(
            ["tmux", "list-clients", "-F",
             "#{client_pid}\t#{session_name}\t#{pane_current_path}"],
            capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None, None, "tmux not reachable"
    if out.returncode != 0:
        return None, None, "no tmux server on the default socket"

    target = clients[0]
    for line in out.stdout.splitlines():
        client_pid, _, rest = line.partition("\t")
        name, _, path = rest.partition("\t")
        if client_pid == target:
            return name, (path or None), f"tmux client {target}"
    return None, None, "tmux client not listed by list-clients (already detached?)"


# `env` flags that take a separate value, so skip_env_wrapper() does not
# mistake the value for the real binary. Measured real case in this project's
# own .desktop set: `Exec=env -u http_proxy ... zapzap`.
_ENV_FLAGS_WITH_VALUE = {"-u", "--unset", "-C", "--chdir"}


def skip_env_wrapper(argv: list[str]) -> list[str]:
    """`argv` with a leading `env [OPTIONS] [KEY=VALUE...]` past, so whoever
    looks at argv[0] sees the program env is launching, not env itself.

    .desktop entries use `env` to set variables for launchers that take none
    as flags -- exec_argv() leaves it alone on purpose, it is not ours to
    remove from the recorded command, only to see past when classifying which
    terminal this is. Round 5 skipped `KEY=VALUE` tokens but not `env` itself,
    which has neither `-` nor `=` and so was never skipped -- found in round 6
    by both reviewers, measured against the exact .desktop this project cites.
    """
    if not argv or Path(argv[0]).name != "env":
        return argv
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in _ENV_FLAGS_WITH_VALUE:
            i += 2
        elif tok.startswith("-"):
            i += 1
        elif "=" in tok:
            i += 1
        else:
            break
    return argv[i:]


def child_command_start(argv: list[str]) -> int | None:
    """Index in `argv` where the terminal's own flags end and whatever it was
    told to run begins, or None if there is nothing after the terminal itself.

    `--` is authoritative when present -- everything after it is the child,
    everything before is the terminal's, full stop, no guessing which `tmux`
    among several is the option value and which is the program (round 6:
    `foot --title tmux -- tmux attach` has "tmux" twice, and a basename check
    with no regard for position picked the wrong one, both reviewers measured
    it independently).

    Without `--`, foot's own launch of Herdr is the shape to get right:
    `foot --app-id=X cmd`, no separator. The first token that is not itself a
    flag AND whose preceding token cannot have consumed it as a value (a
    self-contained `--flag=value` cannot; a bare `-flag`/`--flag` might) is
    where the child starts. Imperfect for a terminal flag not on this project's
    radar that takes a separate-token value, but foot and kitty -- the only
    two this fires for -- do not have one that matters here.
    """
    if "--" in argv:
        return argv.index("--") + 1
    for i in range(1, len(argv)):
        tok = argv[i]
        if tok.startswith("-"):
            continue
        prev = argv[i - 1]
        if prev.startswith("-") and "=" not in prev:
            continue
        return i
    return None


def window_class(window: dict) -> str:
    return window.get("initialClass") or window.get("class") or ""


def resolve(window: dict, index: dict[str, dict] | None = None) -> dict:
    """Resolve one window. Always returns a dict; `argv` is None on failure.

    `via` is part of the result on purpose. A resolver that is wrong and a
    resolver that is right look identical from the outside until the app fails
    to open, so the path taken has to be inspectable (`omasession resolve`).
    """
    if index is None:
        index = desktop_index()

    klass = window_class(window)
    pid = int(window.get("pid") or 0)
    result = {"class": klass, "pid": pid, "argv": None, "cwd": None,
              "via": "unresolved", "single_instance": klass in SINGLE_INSTANCE,
              "note": ""}

    entry = index.get(klass.lower())
    if entry:
        result["argv"] = exec_argv(entry["Exec"])
        result["via"] = ".desktop"
        result["note"] = entry["_path"]
        # Declarar o empate em vez de escondê-lo: relançar pelo comando errado
        # abre a coisa errada com cara de sucesso.
        rivals = [r for r in entry.get("_rivals", []) if r != entry["Exec"]]
        if rivals:
            result["ambiguous"] = rivals
            result["note"] += f"  (+{len(rivals)} other entry claims this class)"

    if result["argv"] is None and pid:
        argv = from_cgroup(pid)
        if argv:
            result["argv"] = argv
            result["via"] = "cgroup"
            result["note"] = "flatpak"

    if result["argv"] is None and pid:
        argv = from_cmdline(pid)
        if argv:
            result["argv"] = argv
            result["via"] = "cmdline"
            result["note"] = "last resort; may not carry the app's own state"

    # A classe do Hyprland é do window manager, não do binário -- uma janela
    # com --app-id/--class custom (o próprio caso que motivou este projeto:
    # `foot --app-id=TUI.tile herdr`) tem klass="TUI.tile", que não bate com
    # nenhuma das duas tabelas abaixo mesmo sendo, de fato, um foot. Medido na
    # revisão da rodada 4: sem checar também o binário já resolvido, esse caso
    # pula o bloco inteiro -- cwd e tmux ficam sem tentar, exatamente a janela
    # que este mecanismo existe para cobrir. skip_env_wrapper() é o que torna
    # isto correto também atrás de um `env FOO=1 kitty` (rodada 6).
    resolved_bin = None
    effective_argv = skip_env_wrapper(result["argv"]) if result["argv"] else None
    if effective_argv:
        resolved_bin = Path(effective_argv[0]).name
    cwd_flag = TERMINAL_CWD_FLAG.get(klass) or TERMINAL_CWD_FLAG.get(resolved_bin)
    wants_tmux = klass in TRAILING_ARGV_TERMINALS or resolved_bin in TRAILING_ARGV_TERMINALS

    if (cwd_flag or wants_tmux) and pid:
        cwd, why = child_cwd(pid)
        result["cwd_note"] = why
        if cwd and cwd_flag:
            result["cwd"] = cwd
            argv = result["argv"]
            if argv and not any(a.startswith(cwd_flag) for a in argv):
                # Antes do `--`, não depois: achado da rodada 6 -- `foot
                # --app-id=X -- bash` virava `foot --app-id=X -- bash
                # --working-directory=Y`, entregando o flag pro bash, não pro
                # foot -- inserir NO índice que child_command_start() devolve
                # (logo depois do `--`) empurra o flag pro lado errado do
                # separador, o mesmo tanto quanto deixá-lo no fim. O `--`,
                # quando existe, é onde o foot para de procurar flags SEUS;
                # o flag tem de ficar antes dele, não no índice que aponta
                # pro que vem depois. Sem `--`, as duas fronteiras coincidem.
                flag_arg = f"{cwd_flag}={cwd}"
                if "--" in argv:
                    insert_at = argv.index("--")
                else:
                    insert_at = child_command_start(argv)
                if insert_at is None:
                    result["argv"] = argv + [flag_arg]
                else:
                    result["argv"] = argv[:insert_at] + [flag_arg] + argv[insert_at:]
        elif not cwd and wants_tmux:
            # child_cwd() found no shell of its own -- the usual reason is a
            # tmux client sitting where the shell should be. Reattaching to
            # its session is a better answer than the cwd we cannot get: the
            # window comes back inside the right session instead of a bare
            # shell in $HOME, and tmux answers its own pane's cwd from there.
            session, session_path, tmux_why = tmux_session(pid)
            result["tmux_note"] = tmux_why
            if session and result["argv"]:
                argv = result["argv"]
                # child_command_start(), não "primeiro token cujo basename é
                # tmux": achado da rodada 6, dos dois revisores, com o mesmo
                # contra-exemplo -- `foot --title tmux -- tmux attach` tem
                # "tmux" duas vezes, uma como VALOR de --title, outra como o
                # programa de verdade depois do --. Uma busca cega pelo
                # primeiro basename=="tmux" cortava no valor da opção,
                # jogando fora o -- e o tmux reais. A fronteira certa é a
                # mesma que decide onde o comando filho começa (rodada 6,
                # achado do cwd acima): só ali faz sentido perguntar "esse
                # comando já é tmux?".
                boundary = child_command_start(argv)
                if boundary is not None and Path(argv[boundary]).name == "tmux":
                    result["argv"] = argv[:boundary] + ["tmux", "new", "-A", "-s", session]
                    result["tmux_session"] = session
                elif boundary is None:
                    # Sem comando filho nenhum (o caso .desktop, foot puro):
                    # nada para confundir com o tmux, acrescenta com --.
                    result["argv"] = argv + ["--", "tmux", "new", "-A", "-s", session]
                    result["tmux_session"] = session
                # Um comando filho que não é tmux (boundary aponta pra outra
                # coisa) não é tocado: tmux_session() só devolve uma sessão
                # quando há um cliente tmux de verdade na árvore, mas isso não
                # prova que o argv resolvido REFLETE esse cliente (um
                # wrapper como `sh -c 'tmux new'` é o caso -- registrado no
                # plano, não vale código para algo tão raro). Deixar como
                # está é mais seguro que reescrever um comando que pode não
                # ser tmux de jeito nenhum.
                if result.get("tmux_session") and session_path:
                    # A cwd para o painel e uma queda-de-pau se o reattach em
                    # si falhar -- replay.py já sabe inserir isto antes de um
                    # `--`, exatamente o caso que seu próprio comentário cita.
                    result["cwd"] = session_path

    return result


def command(result: dict) -> str | None:
    """The resolved argv as one shell-safe string, for exec_cmd."""
    if not result.get("argv"):
        return None
    return " ".join(shlex.quote(a) for a in result["argv"])
