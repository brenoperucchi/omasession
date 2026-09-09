# 001 — resultado da medição: o diagnóstico estava errado, o hook é desnecessário

**Status:** fechado. Medido em 2026-09-08 no lab (Omarchy 4.0.1, Hyprland
0.56.2, Chromium 151.0.7922.173, Google Chrome 152.0.7977.82), dois reboots
reais, um por browser. Protocolo do `omasession-scout`
(`.herdr/scout/001-recomendacao.md`), com o oráculo trocado: três URLs-sentinela
únicas por rodada, e a prova é o que o browser **efetivamente reabre**, lido por
CDP (`/json/list`) — nunca `strings`, que é triagem.

## A tabela

| Fase | Chromium | Chrome | O que prova |
|---|---|---|---|
| `L-PRE` — SIGKILL, restore sem reboot | **3/3** | — | O instrumento funciona; SIGKILL sozinho não perde a sessão |
| `PRE` — cópia com o browser vivo, antes do reboot | **3/3** | **3/3** | As URLs estavam no disco |
| `BOOT` — cópia pós-reboot, **antes de GUI e de qualquer browser** | **3/3** | **3/3** | **Sobreviveram ao reboot** |
| nativo — perfil real, launch sem URLs | **3/3** | — | Vale no caminho de produção, não só no clone |
| `BOOT` **sem** `arm_browser_profile()` | **0/3** | **0/3** | **É aqui que está o mecanismo** |

O arquivo de sessão atravessa o reboot **byte a byte**: mesmo nome
(`Session_13433364697947666`), mesmo tamanho (3382 b), as três sentinelas
presentes na cópia feita antes de qualquer processo gráfico subir.

## O que realmente acontece

Nada se perde no shutdown. O que bloqueia o restore é uma linha do perfil:

```
profile.exit_type = "Crashed"
```

O browser é morto pelo teardown, marca o perfil como crashed, e **recusa o
restore automático** na volta (`startup_browser_creator_impl.cc:804`, na tag
152.0.7977.82). Os dados estão intactos no disco o tempo todo. Com
`arm_browser_profile()` — que já existe em `lib/replay.py:286-287` e força
`exit_type="Normal"` / `exited_cleanly=True` — a mesma cópia restaura 3/3. Sem
ele, 0/3. Nos dois browsers.

`--restore-last-session` também recupera (variante medida: 3/3), então há dois
caminhos; o do `arm()` já está implementado e é o que o replay usa.

## Consequências

1. **O plano 001 fecha sem escrever código.** Não há hook de pre-shutdown a
   construir. Um hook que terminasse os browsers limpo obteria `exit_type =
   "Normal"` pelo caminho natural, mas o replay já força o mesmo valor — de
   graça, e funcionando também no caso que hook nenhum cobre: queda de energia e
   lockup de `amdgpu`, que era a motivação declarada do intervalo de snapshot
   curto.
2. **A medição original era um artefato de método.** O `grep -c` do plano
   (`001:34-43`) roda sobre `Sessions/` *depois* do reboot — e, na prática,
   depois de o browser ter subido e rotacionado a geração. Ele mediu o arquivo
   já substituído. A hipótese de rotação que eu levantei estava certa quanto ao
   efeito, mas a mecânica exata é a promoção lógica descrita em
   `command_storage_backend.cc`, não o `base::Move` do Chromium 80 — o
   `omasession-rev-1` estava certo nesse ponto.
3. **`arm_browser_profile()` sobe de "conveniência" a peça crítica.** Era
   tratada como o passo que evita a barra "Restore pages?". É o que decide se as
   abas voltam ou não. Merece teste e uma mensagem de erro clara quando falha —
   hoje ela retorna `False` em silêncio se o `Preferences` não existir
   (`replay.py:267-268`), e o replay cai no caminho por-janela sem dizer que as
   abas foram perdidas por isso.
4. **A escrita em `replay.py:293` fica ainda mais perigosa.** É `write_text`
   não-atômico sobre o `Preferences`, e agora sabemos que é ela que carrega o
   valor de que tudo depende. `tempfile` + `os.replace()`, e só com o browser
   parado.

## Hipóteses eliminadas, e como

| Hipótese | Veredito | Evidência |
|---|---|---|
| Flush incompleto no teardown | **Falsa** | `BOOT` contém as três sentinelas, capturado pré-GUI |
| Rotação/exclusão no relaunch | **Real, mas não é a causa da perda** | Explica o zero da medição original, não a falha de restore |
| Perfil errado | **Falsa** | Teste nativo no perfil real: 3/3 |
| Formato/leitura (v5 criptografado) | **Falsa aqui** | `Sessions_Encrypted/` não existe; arquivos são v3 claro |
| Rollback CoW da VM | **Falsa** | Marcador com `fsync` intacto após o reboot; inode e processo QEMU idênticos |
| `exit_type = "Crashed"` | **É a causa** | Mesma cópia: com `arm()` 3/3, sem `arm()` 0/3, nos dois browsers |

## Reprodução

`~/VMs/hyprsession-lab/evidence-001-20260908-150939/` (Chromium) e
`evidence-001-chrome-20260908-151443/` (Chrome): cópias completas do
profile-root em cada fase, `pages.json` do CDP, `expected`/`actual` por fase,
marcadores de disco e boot_ids. O harness está em
`.herdr/scout/001-recomendacao.md` (protocolo) e foi executado com as
adaptações registradas abaixo.

Duas mudanças que fiz sobre o protocolo do scout, ambas para reduzir
dependências: sentinelas em `file://` em vez de `example.com` (offline e
determinístico — a rede do guest funciona, medida em 200), e uma variante
`noarm` que o protocolo não previa, que foi justamente a que isolou a causa.
