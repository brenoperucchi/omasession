#!/usr/bin/env python3
"""Verify every directory in $PATH is root-owned and not writable by anyone
else, so a bare command name resolved through it carries the same guarantee
an absolute path would.

Pinning PATH to a short, fixed list of directories (bin/omasession,
bin/browser-setup, lib/session-save.sh) is not by itself the claim that none
of those directories can be tampered with by a non-root local user -- a
package, an admin script, or a misconfiguration could leave one of them
group- or world-writable. Checked once, right after PATH is pinned, with an
absolute interpreter path (this script cannot verify PATH by resolving
tools *through* the PATH it hasn't verified yet). Achado da revisão de
segurança do marketplace (github.com/omacom/omarchy-plugin-marketplace/
issues/6243).

Achado da rodada de revisão omasession-14 (rev-1): a primeira versão desta
verificação fazia só `realpath` + `stat` no alvo final -- se um ancestral do
caminho (por exemplo `/usr/local`) fosse gravável por outro usuário, mesmo
com `/usr/local/bin` root-owned 0755 no instante do check, esse usuário
poderia trocar a entrada `bin` depois, e o check já teria passado. Agora cada
componente é aberto com O_NOFOLLOW a partir de "/", provando que nenhum
ancestral é substituível por quem não seja root -- só o COMPONENTE FINAL de
cada entrada do PATH pode ser um symlink, porque o Arch usa isso de fábrica
(`/bin -> usr/bin`, `/sbin -> usr/bin`): o pai já verificado impede trocar
essa entrada.

Achado da rodada omasession-15 (rev-1 e rev-2, independentemente): duas
lacunas na versão anterior.

  1. `_check_dir` tolerava grupo/outros graváveis quando havia sticky bit
     (herdado do mesmo helper em lib/safe_fs.py, onde faz sentido pra
     diretórios de usuário tipo /tmp). Pra um diretório do PATH isso é
     errado: sticky só impede apagar/renomear entradas alheias, não impede
     CRIAR uma entrada nova com um nome ainda ausente ali -- um
     `/usr/local/bin` 01777 deixaria qualquer usuário plantar um `python3`
     que nunca existiu, executado como root pelo browser-setup depois.
     Removida a exceção aqui; ela continua válida em safe_fs.py, que é outro
     modelo de ameaça.

  2. Seguir o symlink do componente final com `os.stat` valida o alvo, mas
     nenhum componente DO CAMINHO DO ALVO é caminhado -- só o link em si.
     Um `/bin -> /opt/vendor/bin` com `/opt` gravável por outro usuário e
     `/opt/vendor/bin` root-owned no instante do check passaria: o atacante
     troca `vendor` depois. Agora, quando o componente final é um symlink,
     o destino (absolutizado contra a própria entrada) é verificado
     recursivamente do zero -- ou seja, com o mesmo walk a partir de "/" --
     em vez de só ter seu stat final conferido.
"""
import os
import stat
import sys

MAX_SYMLINK_HOPS = 10


def _check_dir(st: os.stat_result, entry: str) -> None:
    if not stat.S_ISDIR(st.st_mode):
        print(f"verify_path: {entry} is not a directory", file=sys.stderr)
        sys.exit(1)
    if st.st_uid != 0:
        print(f"verify_path: {entry} is not root-owned", file=sys.stderr)
        sys.exit(1)
    # Sem exceção de sticky bit aqui, ao contrário de lib/safe_fs.py: sticky
    # não impede criar uma entrada nova, só apagar/renomear uma alheia.
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        print(f"verify_path: {entry} is writable by group or other", file=sys.stderr)
        sys.exit(1)


def verify_entry(entry: str, _seen: frozenset = frozenset()) -> None:
    if not entry.startswith("/"):
        print(f"verify_path: {entry} is not an absolute path", file=sys.stderr)
        sys.exit(1)
    if entry in _seen or len(_seen) >= MAX_SYMLINK_HOPS:
        print(f"verify_path: {entry} -- symlink chain too long or cyclic", file=sys.stderr)
        sys.exit(1)

    parts = [p for p in entry.split("/") if p]
    fd = os.open("/", os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    follow_target = None
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(
                    part, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
                )
            except OSError as exc:
                print(f"verify_path: cannot open {entry}: {exc}", file=sys.stderr)
                sys.exit(1)
            os.close(fd)
            fd = next_fd
            _check_dir(os.fstat(fd), entry)

        if not parts:
            _check_dir(os.fstat(fd), entry)  # entry is "/" itself
            return

        last = parts[-1]
        try:
            st_last = os.lstat(last, dir_fd=fd)
        except OSError as exc:
            print(f"verify_path: cannot stat {entry}: {exc}", file=sys.stderr)
            sys.exit(1)

        if stat.S_ISLNK(st_last.st_mode):
            try:
                link = os.readlink(last, dir_fd=fd)
            except OSError as exc:
                print(f"verify_path: cannot read link {entry}: {exc}", file=sys.stderr)
                sys.exit(1)
            if link.startswith("/"):
                follow_target = os.path.normpath(link)
            else:
                parent = entry.rsplit("/", 1)[0] or "/"
                follow_target = os.path.normpath(os.path.join(parent, link))
        else:
            _check_dir(st_last, entry)
    finally:
        os.close(fd)

    if follow_target is not None:
        verify_entry(follow_target, _seen | {entry})


def main() -> None:
    path = os.environ.get("PATH", "")
    for entry in path.split(":"):
        if entry:
            verify_entry(entry)


if __name__ == "__main__":
    main()
