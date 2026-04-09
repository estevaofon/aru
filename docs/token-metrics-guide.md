# Aru — Guia de Metricas de Tokens e Custo

## As duas linhas de metricas

Apos cada resposta, o Aru exibe duas linhas:

```
tokens: 184,909 (in: 182,072 / out: 2,837 / cached: 36,257) | cost: $0.08 | calls: 2
context: 23,913 (in: 20,268 / out: 856 / cache_read: 2,789)
```

### Linha 1: `tokens` — Total cumulativo do run

Soma de **todos os API calls internos** feitos durante o run do agente.

- **in**: total de input tokens enviados ao modelo (soma de todos os calls)
- **out**: total de output tokens gerados pelo modelo
- **cached**: total de tokens lidos do cache (soma de todos os calls)
- **cost**: custo estimado em USD, levando em conta precos diferenciados para cache read/write
- **calls**: numero de runs do agente na sessao (nao confundir com API calls internos)

Quando o modelo faz tool calls (grep, read_file, etc.), cada tool call gera um novo API call interno. Um unico run com 5 tool calls pode gerar 6 API calls internos (1 inicial + 5 apos cada tool result). O `tokens` soma todos eles.

### Linha 2: `context` — Janela de contexto do ultimo API call

Tokens do **ultimo API call interno** — a janela real que o modelo viu quando gerou a resposta final.

- **in**: input tokens do ultimo call (system prompt + historico + mensagem + tool results)
- **out**: output tokens do ultimo call
- **cache_read**: tokens lidos do cache nesse call (tipicamente o system prompt)
- **cache_write**: tokens escritos no cache nesse call (aparece quando o cache e criado)

**Esta e a metrica comparavel com OpenCode e Claude Code.** Todas usam a mesma fonte: o campo `usage` da resposta da API (Anthropic/OpenAI). Nao e estimativa — e a contagem real que o provider fez ao tokenizar.

## Por que os valores diferem entre as duas linhas

Exemplo real:

```
tokens: 14,331 (in: 13,846 / out: 485)
context: 12,053 (in: 8,886 / out: 378)
```

O run fez 1 tool call (Grep), gerando 2 API calls internos:

1. **Call 1** (~5K input): system prompt + mensagem do usuario → modelo pede Grep
2. **Call 2** (~8.9K input): system prompt + mensagem + resultado do Grep → resposta final

- `tokens.in` = 5K + 8.9K ≈ 13.8K (soma)
- `context.in` = 8.9K (so o ultimo call)

## O que e o `context` exatamente

O `context` e o **tamanho real da janela enviada ao LLM** no ultimo API call. Inclui:

- System prompt (instrucoes do agente, AGENTS.md, contexto de ambiente)
- Historico de conversa (mensagens anteriores, prunadas conforme necessario)
- Mensagem atual do usuario
- Resultados de tool calls acumulados durante o run
- Cache markers (para prompt caching da Anthropic)

Esse valor e reportado pela API do provider — nao e estimativa do Aru. E exatamente o que a Anthropic/OpenAI contou ao tokenizar tudo que recebeu, e e o que eles cobram.

## Comparacao com OpenCode

O OpenCode exibe a mesma metrica. No `processor.ts`:

```typescript
ctx.assistantMessage.cost += usage.cost    // custo: acumula (+=)
ctx.assistantMessage.tokens = usage.tokens // tokens: sobrescreve (=)
```

E no display:

```typescript
tokenTotal = input + output + reasoning + cache.read + cache.write
```

O Aru faz o equivalente:

```python
ctx_total = last_input_tokens + last_output_tokens + last_cache_read + last_cache_write
```

Mesma fonte (`usage` da API), mesma formula. Diretamente comparavel.

## Prompt caching

O `cache_read` constante (~2.8K) que aparece em todo call e o system prompt sendo lido do cache da Anthropic. Isso funciona porque:

1. O system prompt (instrucoes + AGENTS.md) e estavel entre turns
2. A Anthropic cacheia automaticamente com `cache_control: ephemeral`
3. Tokens cacheados custam 10% do preco normal de input

O `cached` na linha cumulativa cresce mais rapido porque soma o cache_read de **todos** os API calls internos.

## Pruning de tool results

O Aru usa pruning budget-based (alinhado com OpenCode):

- **Protege 160K chars** (~40K tokens) de tool results recentes — tudo dentro desse budget fica intacto
- **So prune se tem 80K+ chars** (~20K tokens) de conteudo antigo a liberar
- Tool results antigos viram `[Old tool result cleared]` (placeholder)

Em conversas normais (< 40K tokens de tools), nada e prunado — o modelo ve tudo.

## Custo estimado

O custo usa precos por modelo com rates diferenciados:

| Tipo | Sonnet 4.5/4.6 (por 1M tokens) |
|------|-------------------------------|
| Input | $3.00 |
| Output | $15.00 |
| Cache read | $0.30 (10% do input) |
| Cache write | $3.75 (125% do input) |

Formula: `base_input * price_in + output * price_out + cache_read * price_cache_read + cache_write * price_cache_write`

Onde `base_input = total_input - cache_read - cache_write` (tokens que nao foram cacheados).

## Descoberta importante: metricas cumulativas vs context

A comparacao original "Aru 51K vs OpenCode 18K" era injusta porque:

- **Aru reportava tokens cumulativos** (soma de todos os API calls)
- **OpenCode reportava o ultimo step** (ultimo API call)

O custo real era $0.05 vs $0.04 — praticamente igual. Agora ambas as metricas sao visiveis e comparaveis.
