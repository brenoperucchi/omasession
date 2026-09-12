#!/usr/bin/env python3
"""Descriptor-relative filesystem primitives shared by bin/omasession and
lib/session-save.sh for every write into the user's own session/config/
state files and autostart.lua.

Generalizes a pattern this project first proved out for root's
Chromium-family policy write (lib/browser_policy.py, removed in the rodada
that took that write out of any code path root ever executes -- see git
history for the original): walk the target path one component at a time,
opening each with O_DIRECTORY | O_NOFOLLOW relative to the descriptor
already open for its parent, so a symlink anywhere in the chain makes that
open() fail outright -- there is no path string left to re-resolve after a
check, because there never is a separate check.

Marketplace security review (github.com/omacom/omarchy-plugin-marketplace/
issues/6243): HANCORE-linux's follow-up held this open even after that
first fix landed, because these paths -- unlike the then-root-owned
policy file -- never cross a privilege boundary; this project's own
judgment was that the risk is much narrower here (exploiting it needs
write access to the user's own $HOME already, which grants nothing a local
attacker did not already have). Implemented anyway, at the reviewer's
insistence, as the same descriptor-relative discipline applied uniformly
rather than only where a privilege boundary is crossed.

A component may be owned by root (ancestors like `/` and `/home` usually
are) or by the calling user (everything under $HOME the user created) --
never by anyone else. `read`/`write`/`copy` operate on a single already-
open directory descriptor; nothing here re-opens a path string once the
chain has been walked.

Achado omasession-17 (rev-2): a primeira versão recusava QUALQUER symlink,
em qualquer posição -- correto para o arquivo de política root em /etc
(browser_policy.py), errado aqui: dotfiles gerenciados por symlink
(chezmoi/stow/yadm/um `ln -s` à mão) são um arranjo comum tanto para
config.json/autostart.lua quanto para um ~/.local/share inteiro apontando
pra outro disco, e a versão anterior recusava os três, um deles
(SESSION_DIR, dentro do timer) em silêncio a cada 30s. Agora um symlink NÃO
é recusado de cara: é resolvido, e o ALVO é verificado do zero -- mesmo
formato recursivo que lib/verify_path.py já usa para o componente final do
PATH, generalizado aqui para qualquer componente (open_dir_chain) e para o
nome de arquivo final (read_bytes/write_bytes). Um symlink que o próprio
usuário criou, apontando para algo que ele também possui, é exatamente o
caso legítimo; um alvo fora disso continua sendo recusado, pela mesma
checagem de sempre.
"""
import os
import stat
import sys

# A session/config file this project writes has no legitimate reason to be
# this large. A read that hits this is treated as corrupt, not silently
# truncated -- a truncated read would just move the "is this valid?"
# question to whatever parses it next, with a less honest error.
MAX_READ_BYTES = 8 * 1024 * 1024

MAX_SYMLINK_HOPS = 10


class Refused(Exception):
    """A real trust problem: wrong owner, world-writable, symlink cycle/too
    deep. Distinct from TooLarge and from a plain absent file/dir -- achado
    omasession-17 (rev-2): antes desta separação, um arquivo grande demais,
    um symlink recusado e um dono errado saíam todos com o mesmo rc=1 e a
    mesma mensagem genérica em bin/omasession, escondendo qual dos três
    realmente aconteceu."""


class TooLarge(Exception):
    """A file exists and is readable, but exceeds MAX_READ_BYTES."""


def read_capped_path(path) -> bytes:
    """Read `path` (a plain pathlib.Path, not a dir_fd-relative name),
    refusing more than MAX_READ_BYTES.

    For callers that read by path rather than by dir_fd -- lib/replay.py's
    session pair and lib/captured.py's panel-facing view -- and want an
    oversized file to raise plain OSError, the same "this half is unusable"
    outcome they already have for a missing/corrupt file. Distinct from the
    dedicated TooLarge read_bytes()/copy_within() raise for dir_fd-based
    reads, which bin/omasession needs to tell apart from Refused (a real
    trust problem). Achado omasession-16 (rev-2): MAX_READ_BYTES já tinha
    uma fonte só entre os três arquivos; a função que a usa continuava
    copiada, idêntica, em lib/replay.py e lib/captured.py.
    """
    with open(path, "rb") as f:
        data = f.read(MAX_READ_BYTES + 1)
    if len(data) > MAX_READ_BYTES:
        raise OSError(f"{path} is larger than {MAX_READ_BYTES} bytes")
    return data


def _check_trusted(fd: int, path: str, expected_uid: int) -> None:
    st = os.fstat(fd)
    if st.st_uid not in (0, expected_uid):
        raise Refused(f"refusing {path}: owned by neither root nor us")
    # Sticky bit tolerado aqui de propósito -- ao contrário de
    # lib/verify_path.py, que recusa QUALQUER escrita de grupo/outros num
    # diretório do PATH (achado omasession-15: sticky não impede criar uma
    # entrada nova, só apagar/renomear uma alheia -- errado pra um diretório
    # de onde se executam binários). Este módulo lida com diretórios de
    # DADOS do usuário, nunca de PATH; um /tmp-like com sticky bit é o caso
    # legítimo que essa exceção existe para não recusar (achado omasession-14
    # rev-2: a exceção já existia sem essa frase explicando por quê).
    world_writable = st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    if world_writable and not (st.st_mode & stat.S_ISVTX):
        raise Refused(f"refusing {path}: writable by others, no sticky bit")


def _require_component(name: str) -> None:
    """Refuse a `name` that is not a single path component.

    Every primitive below takes `name` and hands it straight to
    os.open/os.rename/os.unlink with dir_fd= -- the docstring's whole thesis
    is "no path string is ever re-resolved after the ancestor chain is
    verified", but the kernel resolves dir_fd-relative names exactly like
    any other path, `..` included. A caller passing an unvalidated name
    (this project's callers all use fixed literals or names built from a
    session name the CLI itself controls, so this was never reachable
    end-to-end) would silently escape the verified directory -- achado
    omasession-16 (rev-2), reproduzido: um `name` como "vitima/alvo.txt" ou
    "../fora/escapou.txt" escrevia fora do diretório já verificado.
    """
    if not name or name in (".", "..") or "/" in name:
        raise Refused(f"refusing {name!r}: not a single path component")


def _dir_fd_path(dir_fd: int) -> str:
    return os.readlink(f"/proc/self/fd/{dir_fd}")


def _resolve(path: str, create: bool, final_mode: str):
    """Kernel-style resolution of `path`, shared by open_dir_chain,
    read_bytes and write_bytes.

    A stack of already-open, already-verified directory descriptors
    (stack[0] is always "/") and a queue of components still to process.
    `..` pops the stack (never below "/"); an absolute symlink target
    resets the stack to "/" and replaces the remaining queue; a relative
    target is spliced in place of the component that named it -- exactly
    where it was found, ancestor or final. This is NOT the same as
    resolving one symlink, computing an absolute string with
    `os.path.normpath`, and recursing on that string -- achado
    omasession-18 (rev-1), reproduzido com dois exemplos onde essa segunda
    forma dava um caminho FISICAMENTE diferente do que esta função (ou o
    kernel) realmente resolveria: um `..` dentro do alvo de um symlink
    precisa aplicar sobre o diretório físico onde esse symlink foi
    encontrado, não sobre um prefixo léxico, e os dois divergem assim que
    QUALQUER symlink anterior já foi seguido -- inclusive dentro do
    próprio texto do alvo de um symlink (`ln -s "alias/../target" x`).

    `final_mode` decides what happens once the LAST component is reached --
    but a symlink AT that position is followed the same way as anywhere
    else in all three modes (an ancestor being a symlink and the final
    name being one get identical treatment; only what happens to the
    eventual non-symlink final name differs):
      "dir"    it must be a directory too (open_dir_chain's contract) --
               a missing one is created when `create`.
      "file"   it is opened O_RDONLY|O_NOFOLLOW (read_bytes's need).
      "parent" it is never opened at all -- returns (parent_fd, name) for
               whatever the final, no-longer-a-symlink name turns out to
               be, so the caller decides what to do with it (write_bytes's
               need: publish there directly).
    """
    if not path.startswith("/"):
        raise ValueError(f"expected an absolute path, got {path!r}")
    expected_uid = os.geteuid()
    stack = [os.open("/", os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)]
    queue = [p for p in path.split("/") if p]
    hops = 0

    def _follow(cur: int, part: str, rest: list) -> str:
        nonlocal hops
        hops += 1
        if hops > MAX_SYMLINK_HOPS:
            raise Refused(f"refusing {path}: symlink chain too long or cyclic")
        link = os.readlink(part, dir_fd=cur)
        link_parts = [p for p in link.split("/") if p]
        if link.startswith("/"):
            for extra in stack[1:]:
                os.close(extra)
            del stack[1:]
        queue[:] = link_parts + rest
        return link

    try:
        while queue:
            part = queue.pop(0)
            if part == ".":
                continue
            if part == "..":
                if len(stack) > 1:
                    os.close(stack.pop())
                continue
            cur = stack[-1]
            is_last = not queue
            if is_last and final_mode == "parent":
                try:
                    st = os.lstat(part, dir_fd=cur)
                except FileNotFoundError:
                    for f in stack[:-1]:
                        os.close(f)
                    return stack[-1], part
                if not stat.S_ISLNK(st.st_mode):
                    for f in stack[:-1]:
                        os.close(f)
                    return stack[-1], part
                _follow(cur, part, [])
                continue
            want_dir = final_mode == "dir" or not is_last
            try:
                st = os.lstat(part, dir_fd=cur)
            except FileNotFoundError:
                if not (create and want_dir):
                    raise
                os.mkdir(part, dir_fd=cur)
                os.chmod(part, 0o700, dir_fd=cur)
                st = os.lstat(part, dir_fd=cur)
            if stat.S_ISLNK(st.st_mode):
                _follow(cur, part, queue)
                continue
            if want_dir:
                next_fd = os.open(
                    part, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=cur
                )
                stack.append(next_fd)
                _check_trusted(next_fd, path, expected_uid)
            else:
                final_fd = os.open(part, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=cur)
                for f in stack:
                    os.close(f)
                return final_fd
    except BaseException:
        for f in stack:
            os.close(f)
        raise
    fd = stack.pop()
    for f in stack:
        os.close(f)
    return fd


def open_dir_chain(path: str, create: bool) -> int:
    """Open `path`, verifying every component -- symlink or not.

    Starts at "/", which cannot be a symlink. With `create=True`, a missing
    component is made with a plain `mkdir` relative to the parent already
    verified -- never `mkdir -p` on the path string, which would silently
    tunnel through a symlink placed at an earlier component -- and given
    its own explicit mode, not left to the caller's umask (the same bug
    found and fixed in the root-only version of this: a hardened umask
    silently produced a result unusable the next time it's opened). With
    `create=False`, a missing component raises FileNotFoundError.

    Resolves the way a kernel does: a stack of already-open, already-
    verified directory descriptors (stack[0] is always "/"), and a queue of
    components still to process. `..` pops the stack (never below "/"); an
    absolute symlink target resets the stack to "/" and replaces the
    remaining queue; a relative target is spliced in place of the
    component that named it. This is not the same as resolving one
    component, computing an absolute string with `os.path.normpath`, and
    recursing on that string -- achado omasession-18 (rev-1), reproduzido
    com dois exemplos onde essa segunda forma dava um caminho FISICAMENTE
    diferente do que esta função (ou o kernel) realmente resolveria: um
    `..` dentro do alvo de um symlink precisa aplicar sobre o diretório
    físico onde esse symlink foi encontrado, não sobre o prefixo léxico do
    `path` original, e os dois divergem assim que QUALQUER symlink
    anterior já foi seguido.
    """
    return _resolve(path, create, "dir")


def write_bytes(dir_fd: int, name: str, content: bytes, mode: int = 0o600) -> None:
    """Publish `content` as `name` inside `dir_fd`, atomically.

    If `name` is CURRENTLY a symlink -- config.json/autostart.lua managed
    by dotfiles (chezmoi/stow/yadm/a plain `ln -s`) is a common arrangement,
    not an attack -- this writes through it instead of replacing it: the
    target is resolved and re-verified from "/" via the same kernel-style
    walk `_resolve` uses everywhere else, and the whole write is
    redirected into the target's own directory. Achado omasession-17
    (rev-2): a versão anterior sempre publicava por cima do nome, o que
    teria APAGADO o symlink do usuário na primeira escrita --
    silenciosamente, sem erro, na primeira vez que `config set`/`install`
    rodasse contra um dotfile symlinkado.

    The mode of an EXISTING target is preserved rather than overwritten
    with `mode` -- achado omasession-18 (rev-2): a versão anterior impunha
    0600/644 (o que este projeto usa pros seus PRÓPRIOS arquivos) a um
    arquivo do OUTRO LADO de um symlink, dentro do repositório de dotfiles
    do usuário -- uma mudança que ninguém pediu, e que um gerenciador que
    rastreia modo (chezmoi) reportaria como drift a cada `config set`.
    `mode` só decide o modo de um arquivo NOVO (ainda ausente do lado de
    lá).

    Otherwise (the common case: `name` is absent or already a plain file),
    a fixed temp name is fine (unlike a world-writable /etc directory,
    nothing but this same user -- or root -- can create a colliding entry
    in a directory only one of the two can write into). The `unlink` right
    below already clears any leftover from a run this same process killed
    mid-write before `O_EXCL` ever sees it -- achado omasession-14 rev-2:
    uma versão anterior deste comentário descrevia isso como o `O_EXCL`
    "recusando" o leftover, mas as duas defesas não coexistem: o `unlink`
    incondicional garante que ele nunca chega a existir quando o `open` roda.
    `os.rename()` replaces the final name's directory entry outright; it
    does not follow a symlink planted there, and does not recurse into one
    that names a directory the way a bare `mv` (without `-T`) would.
    """
    _require_component(name)
    try:
        st = os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        st = None
    if st is not None and stat.S_ISLNK(st.st_mode):
        full_path = _dir_fd_path(dir_fd).rstrip("/") + "/" + name
        target_dir_fd, target_name = _resolve(full_path, create=False, final_mode="parent")
        try:
            try:
                target_mode = stat.S_IMODE(os.stat(target_name, dir_fd=target_dir_fd).st_mode)
            except FileNotFoundError:
                target_mode = mode
            write_bytes(target_dir_fd, target_name, content, mode=target_mode)
        finally:
            os.close(target_dir_fd)
        return
    stale = f".omasession.tmp.{os.getpid()}"
    try:
        os.unlink(stale, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    fd = os.open(
        stale, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=dir_fd
    )
    try:
        os.fchmod(fd, mode)  # explicit -- open()'s mode is masked by umask
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        os.unlink(stale, dir_fd=dir_fd)
        raise
    os.rename(stale, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def read_bytes(dir_fd: int, name: str) -> bytes | None:
    """Read `name` from `dir_fd`.

    Returns None if the name is absent -- a session/config file that was
    never written is a normal state this project already treats as such
    (no session saved yet, defaults for a fresh config), not an error.

    If `name` is a symlink, its target is resolved and re-verified from
    "/" via the same kernel-style walk `_resolve` uses everywhere else,
    instead of being refused -- achado omasession-17 (rev-2): a versão
    anterior recusava um config.json/autostart.lua gerenciado por dotfiles
    (arranjo comum), derrubando `status --json`, `config get` e o
    install/uninstall inteiros.
    """
    _require_component(name)
    full_path = _dir_fd_path(dir_fd).rstrip("/") + "/" + name
    try:
        fd = _resolve(full_path, create=False, final_mode="file")
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as f:
        data = f.read(MAX_READ_BYTES + 1)
    if len(data) > MAX_READ_BYTES:
        raise TooLarge(f"refusing {name}: larger than {MAX_READ_BYTES} bytes")
    return data


def rename_within(dir_fd: int, src_name: str, dst_name: str) -> None:
    """Rename `src_name` to `dst_name`, both inside the same verified `dir_fd`.

    Same os.rename() property write_bytes's own publish step already
    relies on: replaces dst_name's directory entry outright, never follows
    a symlink planted there and never nests into one that names a
    directory -- unlike a bare `mv` without `-T`, which is what
    lib/session-save.sh used directly for its own staging/publish steps
    until this was added (achado da rodada em que o usuário pediu pra
    fechar todos os pontos que sobraram da revisão do HANCORE-linux:
    reproduzido ao vivo que `mv -f arquivo symlink-para-diretorio` ANINHA
    dentro do diretório em vez de substituir o nome -- a mesma classe de
    bug já achada e corrigida no writer root-only que existia pra isso
    antes de sair do projeto (`lib/browser_policy.py`, removido na rodada
    20; usava `os.rename()` desde o início, por rodar como root).
    """
    _require_component(src_name)
    _require_component(dst_name)
    os.rename(src_name, dst_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def touch_safe(dir_fd: int, name: str, mode: int = 0o600) -> None:
    """Ensure `name` exists as a real file inside `dir_fd`, never a symlink.

    For callers where a SHELL redirect has to reopen the same name right
    after this returns -- lib/session-save.sh's lock file is opened by bash
    as `exec 9>"$path"`, which (unlike everything else in this module) has
    no O_NOFOLLOW equivalent and WOULD follow a symlink planted there,
    truncating whatever it points to. This guarantees, at the instant it
    returns, that `name` is a plain file the bash redirect can safely
    reopen -- refusing outright if it is currently a symlink, and never
    truncating existing content (no O_TRUNC) since a lock file that
    already exists from a previous run is the normal, expected case, not
    something to reset.

    Unlike read_bytes/write_bytes, a symlink here is never followed, on
    purpose: dotfiles do not manage lock files, so a symlink in this exact
    name is the attack this function exists to catch, not a legitimate
    arrangement. Raises Refused (not a generic OSError) -- achado
    omasession-18 (rev-2): a versão anterior deixava o ELOOP do O_NOFOLLOW
    cair no `except OSError` genérico de main() (rc=5, "outro erro de
    I/O"), classificando o único ataque que este primitivo existe pra
    bloquear como se fosse um erro de disco banal.
    """
    os.close(_open_lock_fd(dir_fd, name, mode))


def _open_lock_fd(dir_fd: int, name: str, mode: int) -> int:
    """The actual safe-open behind touch_safe -- kept separate, and
    returning the fd instead of closing it, for exec_with_lock below,
    which needs the SAME descriptor to still be open after this returns.
    """
    _require_component(name)
    try:
        st = os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        st = None
    if st is not None and stat.S_ISLNK(st.st_mode):
        raise Refused(f"refusing {name}: a symlink where a lock file belongs")
    return os.open(name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, mode, dir_fd=dir_fd)


def exec_with_lock(dir_path: str, name: str, fd_num: int, command: list[str]) -> None:
    """Open `name` inside `dir_path` the same safe way touch_safe does,
    then exec `command` -- replacing this process's own image with it --
    carrying that descriptor along already open at `fd_num`, inheritable
    across the exec.

    For lib/session-save.sh's lock file specifically: bash's own `exec
    N>path` has no O_NOFOLLOW equivalent, and touch_safe-then-`exec N>`
    (the fix two rounds of review had already landed) still reopens the
    name by string a moment later, leaving a window between the check and
    that reopen -- narrowed, never closed. Achado do HANCORE-linux
    (follow-up de 2026-09-12, issue #6243): a única forma estrutural de
    fechar é nunca reabrir por nome -- abrir aqui, com O_NOFOLLOW, a
    partir do dir_fd já verificado, e dar exec preservando o descritor,
    de forma que o bash que continua depois NUNCA faz seu próprio `open()`
    nesse nome. `dir_fd`'s own verification (ownership, no symlink in any
    ancestor) already happened in open_dir_chain by the time this runs;
    only the final name -- the one bash cannot check itself -- needed
    this.
    """
    dir_fd = open_dir_chain(dir_path, create=True)
    try:
        lock_fd = _open_lock_fd(dir_fd, name, 0o600)
    finally:
        os.close(dir_fd)
    try:
        os.dup2(lock_fd, fd_num)
    finally:
        if lock_fd != fd_num:
            os.close(lock_fd)
    os.set_inheritable(fd_num, True)
    os.execvp(command[0], command)


def copy_within(dir_fd: int, src_name: str, dst_name: str) -> bool:
    """Copy `src_name` to `dst_name`, both inside the same verified `dir_fd`.

    For .prev-style backups: reads the source through read_bytes (same
    symlink-resolves-to-a-verified-target discipline, same 8 MiB cap),
    publishes the destination through write_bytes (same write-through-a-
    symlink-if-there-is-one). Returns False (no-op) if the source is
    absent, matching the existing "only copy a whole pair" callers already
    check for themselves before calling this.

    Mode is fixed at 0600 rather than copied from the source (unlike `cp
    -p`, which this replaced) -- deliberate, not an oversight: every other
    file this project writes for itself (config.json, last-save.json) is
    already private, and a .prev backup of session data deserves the same,
    regardless of whatever mode the live pair happens to have. mtime IS
    copied from the source -- achado omasession-16 (rev-2): a versão
    anterior perdia o mtime original, inerte hoje (nada no projeto lê o
    mtime de um .prev), mas um .prev existe justamente pra ser inspecionado
    depois de um desastre, quando a data de origem é informação de
    verdade. `follow_symlinks=False` no destino -- achado omasession-17
    (rev-1): sem isto, se o NOME do destino virasse um symlink entre o
    publish e este utime, os timestamps aplicariam no alvo do link, não no
    backup que acabou de ser publicado.
    """
    data = read_bytes(dir_fd, src_name)
    if data is None:
        return False
    try:
        src_stat = os.stat(src_name, dir_fd=dir_fd)  # segue symlink de propósito
    except OSError:
        src_stat = None
    write_bytes(dir_fd, dst_name, data, mode=0o600)
    if src_stat is not None:
        os.utime(
            dst_name,
            ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns),
            dir_fd=dir_fd,
            follow_symlinks=False,
        )
    return True


def _parse(args: list[str]) -> tuple[bool, list[str]]:
    create = "--create" in args
    return create, [a for a in args if a != "--create"]


def main() -> None:
    if len(sys.argv) < 3:
        print(
            "usage: safe_fs.py verify|read|write|copy|rename|touch|exec-with-lock "
            "[--create] <dir> <args...>",
            file=sys.stderr,
        )
        sys.exit(2)
    verb = sys.argv[1]
    if verb == "exec-with-lock":
        # Formato próprio -- não passa por _parse/`rest` porque o argv do
        # comando embutido (depois do `--`) não pode ser confundido com os
        # argumentos posicionais dos outros verbos.
        try:
            sep = sys.argv.index("--")
            dir_path, name, fd_num_s = sys.argv[2:sep]
            command = sys.argv[sep + 1:]
            if not command:
                raise ValueError
        except ValueError:
            print(
                "usage: safe_fs.py exec-with-lock <dir> <name> <fd_num> -- "
                "<command> [args...]",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            exec_with_lock(dir_path, name, int(fd_num_s), command)
        except SystemExit:
            raise
        except Refused as exc:
            print(f"safe_fs: {exc}", file=sys.stderr)
            sys.exit(1)
        except OSError as exc:
            # Inclui FileNotFoundError -- aqui ela só pode vir do próprio
            # `command[0]` não existir pro execvp, não de "arquivo ausente"
            # no sentido dos outros verbos (aquele caso já foi resolvido
            # com create=True no open_dir_chain acima). rc=3 significa
            # "recusado" pra quem lê bin/omasession -- achado da revisão
            # (omasession-19, rev-2): emprestar esse código aqui faria um
            # `bash` ausente do PATH aparecer no painel como "o save
            # recusou", uma causa específica e falsa.
            print(f"safe_fs: {exc}", file=sys.stderr)
            sys.exit(5)
        return  # exec_with_lock só volta aqui se tiver falhado antes do exec
    create, rest = _parse(sys.argv[2:])
    try:
        if verb == "verify":
            os.close(open_dir_chain(rest[0], create=create))
        elif verb == "read":
            dir_fd = open_dir_chain(rest[0], create=create)
            try:
                data = read_bytes(dir_fd, rest[1])
            finally:
                os.close(dir_fd)
            if data is None:
                sys.exit(3)
            sys.stdout.buffer.write(data)
        elif verb == "write":
            mode = int(rest[2], 8) if len(rest) > 2 else 0o600
            dir_fd = open_dir_chain(rest[0], create=create)
            try:
                write_bytes(dir_fd, rest[1], sys.stdin.buffer.read(), mode=mode)
            finally:
                os.close(dir_fd)
        elif verb == "copy":
            dir_fd = open_dir_chain(rest[0], create=create)
            try:
                if not copy_within(dir_fd, rest[1], rest[2]):
                    sys.exit(3)
            finally:
                os.close(dir_fd)
        elif verb == "rename":
            dir_fd = open_dir_chain(rest[0], create=create)
            try:
                rename_within(dir_fd, rest[1], rest[2])
            finally:
                os.close(dir_fd)
        elif verb == "touch":
            mode = int(rest[2], 8) if len(rest) > 2 else 0o600
            dir_fd = open_dir_chain(rest[0], create=create)
            try:
                touch_safe(dir_fd, rest[1], mode=mode)
            finally:
                os.close(dir_fd)
        else:
            print(f"unknown verb: {verb}", file=sys.stderr)
            sys.exit(2)
    except SystemExit:
        raise
    except FileNotFoundError:
        # Um componente do DIRETÓRIO em si ainda não existe -- instalação
        # nova, nada de errado. Sem isto caía no OSError genérico logo
        # abaixo (exit 1), o mesmo código de uma recusa de confiança de
        # verdade (symlink na cadeia, dono errado) -- achado ao integrar
        # isto em bin/omasession (rodadas omasession-14/15): um STATE_DIR
        # que nunca foi criado saía idêntico a uma recusa de segurança.
        # O.NOFOLLOW recusando um symlink levanta ELOOP, não ENOENT -- um
        # OSError comum, não este -- então a distinção é por tipo de
        # exceção, não por adivinhar a partir da mensagem.
        sys.exit(3)
    except Refused as exc:
        # 1 -- uma recusa de confiança de verdade (dono errado, gravável por
        # outros, cadeia de symlink cíclica/funda demais, nome com `/`/`..`).
        print(f"safe_fs: {exc}", file=sys.stderr)
        sys.exit(1)
    except TooLarge as exc:
        # 4, separado de Refused -- achado omasession-17 (rev-2): antes
        # deste split, um arquivo grande demais e uma recusa de segurança de
        # verdade saíam com o MESMO rc=1 e a mesma mensagem genérica em
        # bin/omasession ("failed the directory-chain trust check"), embora
        # sejam problemas completamente diferentes -- um é corrupção/limite,
        # o outro é um symlink recusado. Quem chama pode decidir diferente
        # pra cada um (cmd_install recusa os dois; o painel pode degradar
        # pros defaults só no de tamanho, nunca no de confiança).
        print(f"safe_fs: {exc}", file=sys.stderr)
        sys.exit(4)
    except OSError as exc:
        # 5 -- qualquer outro erro de I/O que não seja nenhum dos três
        # acima (disco cheio, permissão negada por um motivo que não é
        # dono/modo, etc.) -- não confundir com uma recusa de confiança.
        print(f"safe_fs: {exc}", file=sys.stderr)
        sys.exit(5)


if __name__ == "__main__":
    main()
