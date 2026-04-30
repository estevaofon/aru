# Ctrl+C durante streaming + TUI invadindo o terminal — investigação

**Data:** 2026-04-30
**Branch:** develop
**Status:** Ctrl+C resolvido. TUI invadindo é pendência conhecida com
instrumentação pronta para a próxima ocorrência.

Este doc cobre dois sintomas correlacionados que apareceram na mesma
sessão de investigação:

* **Sintoma A — Ctrl+C parando durante stream / continuando surdo
  depois:** resolvido. Causa real e fix abaixo.
* **Sintoma B — TUI invadida pelo terminal, escapes brutos de mouse
  (`[555;X;YM`) aparecendo:** observado uma vez, não retornou.
  Hipótese e instrumentação preparada para investigar quando voltar.

---

## 1. Tentativas que **não** funcionaram (Layers 15–19)

A primeira sessão da investigação gastou ~720 linhas em 5 camadas que
foram inteiramente revertidas. Todas partiram da hipótese **errada** de
que o gargalo era saturação do main loop. O log do tracer mostrou que
isso era responsável por ~1% do tempo da sessão — não pelo bug.

### Layer 15 — `MAX_QUEUED_WRITES` 30 → 10 000

Hipótese: produtor bloqueando em `Queue.put` saturada do `WriterThread`.
Resultado: **nenhum efeito observável**. O sintoma reapareceu no mesmo
ponto da stream com 30 ou com 10 000.

### Layer 16 — `finalize_render` off-thread via `asyncio.to_thread`

Hipótese: re-parse markdown sync de 700ms+ bloqueava o loop em toda
fronteira de tool. Resultado: removeu o bloqueio (medido em <50ms),
mas o sintoma persistiu.

### Layer 17 — agent stream em thread OS separada

Hipótese: GIL contention agente vs UI loop. Implementado como
`run_agent_capture_threaded` + `asyncio.to_thread`. **Introduziu bug
novo**: `asyncio.run` per-turno fechava o loop, e clientes httpx do
agno cacheavam pools loop-bound — segundo turno quebrava com
"Error from OpenAI API: Event loop is closed".

### Layer 18a — `WriterThread` com deque ilimitada

Atacou o mesmo alvo morto da Layer 15. Ineficaz pelo mesmo motivo.

### Layer 18b — abort out-of-loop via hook em `Driver.process_message`

Hipótese: flipar `abort_event` antes do pump dispatch. Em teoria
correto, mas atacou um caminho que o trace mostra **nem está sendo
acionado** — Ctrl+C nem chega a `Driver.process_message` (ver Seção 2).

### Layer 19 — agent loop persistente (singleton thread)

Corrigia o bug que Layer 17 criou, mas não tocava o sintoma do
usuário. Tudo descartado junto.

### O que aprendemos com elas

* **Não é** `Queue.put` saturada (Layers 15+18a).
* **Não é** re-parse markdown sync (Layer 16).
* **Não é** GIL contention agente vs UI loop (Layer 17).

Três suspeitos óbvios eliminados experimentalmente. Esse foi o único
valor produzido por aquelas 720 linhas — um caro processo de
eliminação por força bruta. A próxima fase começou com instrumentação
em vez de código.

---

## 2. Sintoma A — Ctrl+C: causa real e fix aplicado

### Diagnóstico (do trace, não de hipótese)

A instrumentação `aru/_debug/loop_tracer.py` foi adicionada com 8
pontos: heartbeat 20Hz no main loop, hook em `Driver.process_message`
(thread `textual-input`), hook em `App._post_message` (loop thread),
entry de `action_ctrl_c`, sampler do hot-loop em `streaming.run_stream`,
duração de `finalize_render`, instrumentação do shutdown
(`action_quit_app`, `on_unmount`, `run_tui_finally`, `atexit`,
`WriterThread.stop`), e sniff de `term_mode_escape`.

Reprodução real em sessão do usuário (log: `~/.aru/loop-trace.log`,
~7700 linhas) revelou:

* `1182 driver.process_message` registrados — **input thread está viva**
  e processando teclas (e, o, n, t, espaço, enter, etc.).
* **Zero entries com `key=ctrl+c`**. Outras teclas chegam normalmente.
* `loop_blocked` apareceu 7 vezes no total, max 1250ms. **1% da
  sessão.** Insuficiente para explicar Ctrl+C inteiramente surdo.

Conclusão: **o byte do Ctrl+C nunca chega ao parser XTerm da Textual.**

### Causa raiz

No Windows, mesmo com `ENABLE_PROCESSED_INPUT=0` (Textual desliga em
`win32.enable_application_mode`), o **Console Control Handler** do
Python continua interceptando `CTRL_C_EVENT` e disparando `SIGINT`. O
caminho é separado do input buffer que `ReadConsoleInputW` drena —
mesmo desabilitando aquele flag, o keystroke é roubado antes de
chegar ao buffer.

O código original em `aru/tui/app.py:on_mount` registrava um
`signal.signal(SIGINT, lambda _sig, _frame: None)` — handler no-op.
A intenção era **absorver** o sinal pra não tirar o app, mas o
side-effect era que **a tecla também sumia**. Ctrl+C não disparava
nada: não vinha como keystroke (Console Control Handler comeu) e o
handler no-op não fazia nada.

### Fix

`aru/tui/app.py` — duas mudanças em ~25 linhas:

1. O SIGINT handler agora usa `loop.call_soon_threadsafe` para hop ao
   loop e disparar `_on_sigint_from_handler`, que por sua vez chama
   `action_ctrl_c()`. Mesmo caminho que um keystroke teria seguido.

2. Novo método `_on_sigint_from_handler` no App: loga
   `sigint_dispatched_to_loop` e chama `action_ctrl_c()` com `try/except`
   para não tombar o app caso uma action handler falhe.

### Validação

Trace de sessão pós-fix:

```
242250  textual-input  driver.process_message  type=Key key=ctrl+c    (← 4 vezes
271608  textual-input  driver.process_message  type=Key key=ctrl+c        chegaram via
291608  textual-input  driver.process_message  type=Key key=ctrl+c        keystroke também
370765  textual-input  driver.process_message  type=Key key=ctrl+c        — o caminho dos
                                                                          dois funciona)
512578  MainThread     sigint_received                                ← 9 vezes via
512578  MainThread     sigint_dispatched_to_loop                          SIGINT path
512578  MainThread     action_ctrl_c           busy=True

(...)

13 action_ctrl_c total — todas dispatadas, sem latência mensurável
```

`action_ctrl_c` disparou inclusive durante períodos em que o loop
estava genuinamente bloqueado (`gap_ms=703`, `578`, `485`), porque o
caminho via SIGINT **não passa pelo loop saturado** — o handler é
chamado pelo Python signal delivery thread, e
`call_soon_threadsafe` enfileira sem bloquear. Quando o loop libera,
roda. Tipicamente em <0ms na medição.

---

## 3. Sintoma B — TUI invadindo o terminal (pendente)

### O que foi observado

Na mesma sessão de validação, num momento intermediário, o terminal
"foi invadido por baixo" — o alt-screen sumiu, o conteúdo do shell
voltou a aparecer, e os bytes brutos do mouse SGR tracking
(`\x1b[<button;x;yM`) começaram a aparecer como texto literal:

```
@estev → starfox-ursina git(main) ⊗ ^C
[555;83;41M[555;83;40M[555;83;39M[555;83;38M...
```

Centenas de linhas. O usuário precisou fechar a janela.

### Hipótese principal

* Algo escreveu `\x1b[?1049l` (leave alt-screen) durante a sessão —
  **não** o `stop_application_mode` da Textual no shutdown, que é o
  uso legítimo. Suspeitos:
  - Output de `! shell_command` que continha o byte cru
  - Log do agno passando pelo `log_bridge` sem sanitização
  - Output de tool não filtrado pelo `_SanitizedRenderable`
* Após a saída do alt-screen, `_self_heal_terminal_state` (Layer 9,
  roda a cada 3s) **continuou re-emitindo** os enables de mouse
  tracking (`\x1b[?1000h`, `\x1b[?1003h`, etc).
* Resultado: mouse tracking ativo no buffer normal do terminal → cada
  movimento de mouse vira escape SGR cru visível como texto.

Não é nova — é o tema central das Layers 7–14 documentadas em
`aru/tui/widgets/chat.py` (post-mortem inline). Há proteção parcial
via `_SanitizedRenderable` e similares, mas o vetor exato da invasão
desta vez não foi confirmado.

### Status

Não voltou a aparecer em sessões subsequentes. Pode ter sido vazamento
de um caminho específico que não foi exercitado de novo.

### Como investigar quando voltar

A instrumentação `term_mode_escape` em `aru/_debug/loop_tracer.py`
está pronta. Toda escrita de escape de modo do terminal
(`\x1b[?1049[hl]`, `\x1b[?1000[hl]`, `\x1b[?1003[hl]`,
`\x1b[?1006[hl]`, `\x1b[?1015[hl]`, `\x1b[?1004[hl]`,
`\x1b[?2004[hl]`, `\x1b[?25[hl]`) é registrada com 8 bytes de
contexto antes/depois.

**Playbook quando o sintoma voltar:**

1. Ative o tracer:
   ```bash
   ARU_DEBUG_LOOP=1 aru          # bash / git bash
   ```
   ```powershell
   $env:ARU_DEBUG_LOOP=1; aru    # PowerShell
   ```

2. Reproduza o sintoma. Quando notar a invasão começar, anote
   mentalmente qual ação acabou de fazer (paste, comando shell,
   tool grande, etc.).

3. Saia (`Ctrl+Q`) e rode:
   ```bash
   python -m aru._debug.analyze_trace
   ```

4. No log bruto (`~/.aru/loop-trace.log`), procure entries de
   `term_mode_escape mode=1049 action=l` que **não** estão no início
   (startup, esperado: `mode=1049 action=h`) ou no final (shutdown).
   Qualquer um desses no meio da sessão é o smoking gun.

5. O campo `sample=` mostra os 8 bytes vizinhos à esquerda e direita
   do escape. Se aparece junto com texto que parece output de tool,
   resposta de agente, ou comando shell, isso identifica a origem.

6. Correlacione com timestamps próximos:
   * Logo antes: qual `tool.called` ou `app._post_message` aconteceu?
   * Loop bloqueou (`loop_blocked`) por perto? — pode ter cortado
     uma escrita pela metade.

### Fixes candidatas (a serem escolhidas após confirmar o vetor)

* **F1 — sanitização adicional:** se um caminho específico vazar
  (ex: `log_bridge`, `! shell_command`), fortalecer o filtro de
  control bytes daquele caminho. Mudança cirúrgica em um arquivo.

* **F2 — shake periódico do alt-screen:** `_self_heal_terminal_state`
  já re-emite enables de mouse; pode também re-emitir `\x1b[?1049h`
  para garantir alt-screen ativo. Risco: piscar a tela em terminais
  que reportam display refresh.

* **F3 — desligar mouse tracking quando alt-screen falhar:** detectar
  via heurística (cursor em posição inesperada, eco no input) e
  desligar mouse para evitar a invasão. Mais elaborado.

A escolha depende de qual vetor o trace identificar. Sem o vetor,
qualquer fix é chute — exatamente o erro que a Seção 1 documenta.

---

## 4. Estado final pós-investigação

### Mudanças mantidas

* `aru/tui/app.py` — fix do SIGINT handler (`_sigint_handler` +
  `_on_sigint_from_handler`).
* `aru/_debug/loop_tracer.py` — tracer com gate por env var
  (`ARU_DEBUG_LOOP=1`). Custo zero quando desligado.
* `aru/_debug/analyze_trace.py` — analyzer que executa a árvore de
  decisão Q1-Q5 do plano original (mantido como ferramenta).
* Hooks em `aru/streaming.py` (StreamSampler), `aru/tui/widgets/chat.py`
  (TimedSection no `finalize_render`), e ganchos no `aru/tui/app.py`
  para shutdown path.
* `AGENTS.md` atualizado com entradas dos dois novos módulos.

### Mudanças descartadas

Tudo das Layers 15–19. Working tree foi `git restore`-ado completamente
antes de aplicar a fix correta.

### Erros do meu processo (lições)

1. **Não pedi reprodução instrumentada antes de codar.** Layers 15–19
   foram puro chute. A instrumentação que apareceu na fase 1 deveria
   ter sido o primeiro código escrito.

2. **Empilhei hipóteses sem confirmar a anterior.** Layer 16 não foi
   confirmada como solução antes de Layer 17. Layers 18a/b foram
   aplicadas sem teste prático contra o sintoma.

3. **Layer 17 introduziu bug novo (loop fechado) que precisou de Layer
   19 para não quebrar o agno.** Mais código para corrigir código que
   ainda nem tinha resolvido o problema original.

4. **Mudanças cross-cutting (5 arquivos por layer) tornam reverter
   caro.** Cada layer deveria ter sido uma branch separada.

5. **Não tinha `analyze_trace.py` antes do log existir.** Quando o
   primeiro trace chegou, tive que ler 8000 linhas manualmente para
   descobrir que não havia `key=ctrl+c`. Hoje o analyzer faria isso
   em 2 segundos com VERDICT explícito.

A versão final é simples: **uma mudança em `app.py`** + tracer guardado
para a próxima vez. Ferramenta sobrevive ao bug.
