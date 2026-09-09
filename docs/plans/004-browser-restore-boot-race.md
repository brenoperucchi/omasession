# 004 — corrida entre o restore de browser e a carga de boot

**Status:** aberto. Observado num teste de reboot real; não reproduzido sob
demanda ainda, e não corrigido às pressas por isso.

## O que foi medido

Teste completo de ponta a ponta em 2026-09-09, com `sudo systemctl reboot` de
verdade (não um restart dentro da sessão): 4 apps salvos (`foot`, `nautilus`,
`obsidian`, `chromium` — este sem nenhuma navegação real, uma aba em branco),
reboot, restauração automática via `autostart.lua` → `omasession restore
--if-enabled`.

Resultado: **5 janelas na tela**, não 4. `foot`/`nautilus`/`obsidian` corretos;
`chromium` apareceu **duas vezes**, mesmo PID (1620) — não é um segundo
processo, é a mesma instância do Chromium com duas janelas.

Tentativa de reprodução manual, mesmo cenário, restore fora do boot (sessão já
estável): **1/1 placed**, sem duplicação. Não reproduziu.

## Diagnóstico provisório

`restore_browser()` em `lib/replay.py` espera o browser reabrir sua própria
janela e decide que ele "terminou" após `BROWSER_QUIET = 4.0` segundos sem
novidade (`lib/replay.py:39`), com um teto de `BROWSER_TIMEOUT = 40.0`. Se o
Chromium demorar mais que isso para reabrir — plausível durante o boot, com
dezenas de serviços do systemd, dbus, portals e o próprio `wayland-wm-app-daemon`
subindo ao mesmo tempo — o replay conclui que a janela salva não voltou, abre
uma substituta via `--new-window` (`lib/replay.py:367-380`), e a original
aparece depois, atrasada. As duas ficam.

A hipótese está alinhada com o que se sabe (não reproduz fora do boot, onde o
sistema já está com carga estável) mas **não foi confirmada por medição
direta** — o restore automático rodou dentro de um scope do
`wayland-wm-app-daemon` (`systemd-run --user --scope ... --quiet --collect`),
e essa combinação não encaminhou o `stdout`/`stderr` do `replay.py` para o
journal, então não há log de qual dos dois caminhos (`wait_for_browser`
retornando cedo, ou o Chromium genuinamente demorando) ocorreu.

## Como medir de verdade, antes de corrigir

1. Trocar temporariamente a linha do `autostart.lua` para redirecionar a saída
   do restore a um arquivo (`... restore --if-enabled >>/tmp/restore-boot.log
   2>&1`), reboot real, e ler o log — ele já imprime
   `[browser] chromium: N window(s) -- letting the browser restore them` e o
   resultado de `wait_for_browser`.
2. Repetir o reboot 3-5 vezes para saber se é sistemático ou intermitente.
3. Só depois disso decidir a correção: candidatos são aumentar `BROWSER_QUIET`
   quando `--if-enabled` for passado (sinal de que é um restore de boot, não
   interativo), ou detectar que o sistema acabou de bootar por outro meio
   (`stat /proc/1` idade, `systemd-analyze`) e escalar o timeout
   proporcionalmente.

## Por que não corrigir agora

Uma correção sem medir de novo seria a mesma classe de erro que este projeto
inteiro existe para evitar: ajustar um número com base numa hipótese plausível,
sem confirmar qual dos dois caminhos realmente disparou. `BROWSER_QUIET` maior
tem custo (um restore de boot mais lento sempre que só um app não-browser
estiver salvo), então vale saber se o problema é real e sistemático antes de
pagá-lo.
