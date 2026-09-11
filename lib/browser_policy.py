#!/usr/bin/env python3
"""Install or remove OmaSession's browser policy file without ever trusting a
path string across two syscalls.

bin/browser-setup used to validate a directory with `realpath`/`stat` and
then reopen that same path by name for `mktemp`/`mv` -- a real TOCTOU window
between the check and the write, on a script that runs as root. Reviewed for
the marketplace security review (github.com/omacom/omarchy-plugin-marketplace
/issues/6243, rounds omasession-12/13): reviewed against code, not live
attacks -- the live reproductions (symlink on the final name, symlink to a
directory, the whole `managed` directory swapped) were run by this project's
own exec, in the lab, after each round's findings, not by either reviewer.

The fix both reviewers pointed at: walk the path one component at a time,
opening each with O_DIRECTORY | O_NOFOLLOW relative to the file descriptor
already open for its parent. A symlink swapped into any component -- before
this runs, or racing it -- makes that open() fail outright; there is no path
string left to re-resolve after the check, because there never was a
separate check. Every later operation (mkdir, the temp file, the final
rename or unlink) is `*at()`, resolved against that same descriptor, never
against a path.

`os.rename()` itself already refuses to follow a destination symlink to
wherever it points -- POSIX rename(2) replaces the directory entry, it does
not recurse into what that entry named. The nesting behavior a bare `mv`
showed is `mv`'s own command-line convenience (it stats the destination
first and, if that resolves to a directory, silently retargets under it) --
calling rename() directly here has no such step to have that opinion in.

Round omasession-13 found two regressions the rewrite itself introduced,
neither a security hole but both a silent functional failure -- exactly the
kind of thing this project exists to refuse to ship quietly:

  - `open()`'s mode argument is masked by the CALLER's umask, same as any
    `mkdir`/`open` in C. The old bash did `chmod 644` *after* writing, which
    bypasses umask entirely; this file lost that when it stopped using a
    separate chmod step. Under `umask 077` (not exotic -- a hardened `sudo`
    profile, or a `systemd-run` invocation, can hand this script exactly
    that) the policy would land 0600, unreadable by the browser running as
    an unprivileged user, while the script still reports success. Fixed with
    an explicit `fchmod`/`chmod` after creation, independent of umask.
  - `remove()` reused the same directory-opening helper as `install()`,
    which creates missing components -- so `--remove` started manufacturing
    empty policy directories in /etc for vendors that were never installed.
    Fixed by giving the walk a `create` flag remove() sets to False, so a
    missing component is reported as nothing to remove, not built.
"""
import os
import stat
import sys

FILENAME = "omasession-no-promo.json"
POLICY = (
    '{"RestoreOnStartup": 1, "PromotionalTabsEnabled": false, '
    '"DefaultBrowserSettingEnabled": false}\n'
)


def _check_trusted(fd: int, path: str) -> None:
    """Refuse a directory this process should not be writing root data into,
    even though O_NOFOLLOW already proved it is a real directory, not a
    symlink. Owned by anyone but root is refused outright. Root-owned but
    writable by group or other, without the sticky bit that would stop
    another user from renaming or deleting entries inside it, is refused
    too -- the same shape of check `/tmp` needs its sticky bit for. Applied
    to every component in the chain, not only the final directory: an
    intermediate directory answering to a different owner does not make the
    final one unsafe by itself, but there is no reason to walk through one
    silently rather than say so.
    """
    st = os.fstat(fd)
    if st.st_uid != 0:
        raise SystemExit(f"refusing {path}: not root-owned")
    world_writable = st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    if world_writable and not (st.st_mode & stat.S_ISVTX):
        raise SystemExit(f"refusing {path}: writable by non-root, no sticky bit")


def open_dir_chain(path: str, create: bool) -> int:
    """Open `path`, refusing any symlink in it, component by component.

    Starts at "/" itself, which cannot be a symlink. With `create=True`, a
    missing component is made with a plain `mkdir` relative to the parent
    already verified -- never `mkdir -p` on the path string, which would
    silently tunnel through a symlink an attacker placed at an earlier
    component -- and its own mode is set explicitly after creation, not left
    to the caller's umask. With `create=False` (the removal path), a missing
    component raises FileNotFoundError instead of being built: deleting a
    policy that was never installed must not manufacture the directory it
    would have lived in. Returns an open fd for the final directory.
    """
    if not path.startswith("/"):
        raise ValueError(f"expected an absolute path, got {path!r}")
    fd = os.open("/", os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for part in path.split("/"):
            if not part:
                continue
            try:
                next_fd = os.open(
                    part, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
                )
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, dir_fd=fd)
                os.chmod(part, 0o755, dir_fd=fd)
                next_fd = os.open(
                    part, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
                )
            os.close(fd)
            fd = next_fd
            _check_trusted(fd, path)
    except BaseException:
        os.close(fd)
        raise
    return fd


def install(path: str) -> None:
    dir_fd = open_dir_chain(path, create=True)
    try:
        # A run killed between here and the rename below leaves this behind
        # forever (BaseException below catches exceptions, not signals) --
        # swept up front so a stale one from an earlier SIGKILL never blocks
        # `O_EXCL` on a reused pid instead of just being silently replaced.
        stale = f".omasession-no-promo.{os.getpid()}"
        try:
            os.unlink(stale, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        fd = os.open(
            stale,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
            dir_fd=dir_fd,
        )
        try:
            # The mode above is what O_CREAT would use with no umask at all --
            # masked in practice by the caller's, same as any open()/mkdir() in
            # C. `chmod 644` in the bash version ran *after* writing for
            # exactly this reason; fchmod here is that same step, not covered
            # by passing a mode to open().
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "w") as f:
                f.write(POLICY)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            os.unlink(stale, dir_fd=dir_fd)
            raise
        # Replaces the FILENAME directory entry outright -- never follows it,
        # whatever it named a moment ago.
        os.rename(stale, FILENAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def remove(path: str) -> bool:
    try:
        dir_fd = open_dir_chain(path, create=False)
    except FileNotFoundError:
        return False
    try:
        try:
            os.unlink(FILENAME, dir_fd=dir_fd)
            return True
        except FileNotFoundError:
            return False
    finally:
        os.close(dir_fd)


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in ("install", "remove"):
        print("usage: browser_policy.py install|remove <dir>", file=sys.stderr)
        sys.exit(2)
    mode, path = sys.argv[1], sys.argv[2]
    try:
        if mode == "install":
            install(path)
            print(f"{path}/{FILENAME}")
        else:
            if remove(path):
                print(f"removed {path}/{FILENAME}")
            else:
                print(f"absent: {path}/{FILENAME}")
    except OSError as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
