---
title: Aru
description: Assistente de programação inteligente no terminal, com múltiplos agentes Claude
hide:
  - navigation
---

# Aru

**Aru** é um assistente de programação inteligente que roda no terminal. Você descreve a tarefa em linguagem natural e agentes especializados planejam, exploram o código e aplicam mudanças usando Claude.

![Aru demo](https://github.com/user-attachments/assets/e84d5139-ebaa-4d12-bbae-628fae7dbc7a)

## Destaques

<div class="grid cards" markdown>

-   :material-brain:{ .lg .middle } __Arquitetura Multi-Agente__

    ---

    Agentes especializados para planejamento, execução, exploração e conversação — cada um com seu próprio conjunto de ferramentas e prompt.

-   :material-console:{ .lg .middle } __CLI Interativo__

    ---

    REPL com respostas em streaming, suporte a multi-linha, histórico de sessões e mentions de arquivos com `@`.

-   :material-puzzle:{ .lg .middle } __11 Ferramentas Integradas__

    ---

    Leitura e edição de arquivos, busca em código, shell, web search e delegação de tarefas a sub-agentes.

-   :material-swap-horizontal:{ .lg .middle } __Multi-Provider__

    ---

    Anthropic, OpenAI, Ollama, Groq, OpenRouter, DeepSeek e outros providers customizados via `aru.json`.

-   :material-image-multiple:{ .lg .middle } __Suporte a Imagens__

    ---

    Anexe imagens com `@arquivo.png` para análise multimodal (Claude, GPT-4o, Gemini).

-   :material-cog-outline:{ .lg .middle } __Extensível__

    ---

    Comandos, skills, agentes, ferramentas Python e plugins com hooks compatíveis com OpenCode.

</div>

## Instalação

```bash
pip install aru-code
```

Configure sua chave da Anthropic em um `.env`:

```env
ANTHROPIC_API_KEY=sk-ant-sua-chave-aqui
```

E execute:

```bash
aru
```

Pronto. Veja o [Início Rápido](comecando/inicio-rapido.md) para mais detalhes.

## Como funciona

```text
main.py → cli.run_cli() → REPL
                           ├─ General Agent   (conversa + ferramentas)
                           ├─ /plan → Planner (plano passo a passo)
                           └─ Executor        (implementa cada passo)
```

O agente geral resolve tarefas diretas. Quando você pede `/plan`, o Planner gera um plano em Markdown, e o Executor implementa cada passo com acesso a todas as ferramentas.

## Configuração

A configuração é o coração do Aru. É nela que você define **qual modelo usar**, **quais providers conectar**, **o que cada ferramenta pode fazer** e **quais regras o agente deve seguir**. O Aru lê dois níveis de configuração, com o projeto sobrescrevendo o global via merge profundo:

| Nível | Caminho | Propósito |
|-------|---------|-----------|
| **Global** | `~/.aru/config.json` | Padrões para todos os projetos (modelo, aliases, permissões, providers) |
| **Projeto** | `aru.json` ou `.aru/config.json` | Overrides específicos do projeto (modelo, permissões, MCP, plugins) |

### Exemplo mínimo de `aru.json`

```json
{
  "default_model": "anthropic/claude-sonnet-4-6",
  "model_aliases": {
    "sonnet": "anthropic/claude-sonnet-4-6",
    "opus": "anthropic/claude-opus-4-6"
  },
  "permission": {
    "read": "allow",
    "grep": "allow",
    "glob": "allow",
    "edit": "ask",
    "write": "ask",
    "bash": { "pytest *": "allow", "rm *": "deny" }
  },
  "instructions": ["AGENTS.md"],
  "plugins": ["./plugins/meu_plugin.py"]
}
```

### Campos principais

| Campo | Descrição |
|-------|-----------|
| `default_model` | Modelo padrão no formato `provider/model` |
| `model_aliases` | Atalhos para `/model <alias>` |
| `providers` | Providers customizados (tokens, endpoints, headers) |
| `permission` | Regras granulares allow/ask/deny por ferramenta e padrão |
| `instructions` | Arquivos ou URLs anexados ao system prompt (ex: `AGENTS.md`) |
| `mcp` | Servidores Model Context Protocol a conectar |
| `plugins` | Lista explícita de plugins a carregar |
| `agent` | Overrides por agente customizado (modelo, prompt, ferramentas) |

### Áreas de configuração

<div class="grid cards" markdown>

-   :material-file-cog-outline:{ .lg .middle } __Visão Geral__

    ---

    Como os arquivos `~/.aru/config.json` e `aru.json` são carregados, mesclados e aplicados em runtime. Ponto de partida para entender toda a configuração.

    [:octicons-arrow-right-24: Visão geral](configuracao/index.md)

-   :material-swap-horizontal-variant:{ .lg .middle } __Modelos e Providers__

    ---

    Configure Anthropic, OpenAI, Ollama, Groq, OpenRouter, DeepSeek ou providers OpenAI-compatíveis customizados. Aliases, tokens e endpoints.

    [:octicons-arrow-right-24: Modelos e Providers](configuracao/modelos.md)

-   :material-shield-key-outline:{ .lg .middle } __Permissões__

    ---

    Sistema granular **allow / ask / deny** por ferramenta, com regras por padrão (ex: `bash: { "pytest *": "allow" }`). Controle fino do que o agente pode executar.

    [:octicons-arrow-right-24: Permissões](configuracao/permissoes.md)

-   :material-file-document-outline:{ .lg .middle } __Regras (AGENTS.md)__

    ---

    Instruções de projeto anexadas automaticamente ao system prompt. Use para convenções de código, arquitetura, padrões de commit e contexto do repositório.

    [:octicons-arrow-right-24: AGENTS.md](configuracao/regras.md)

-   :material-server-network:{ .lg .middle } __MCP Servers__

    ---

    Integre servidores Model Context Protocol (GitHub, Slack, Postgres, filesystem…) declarados no `aru.json` para expor ferramentas externas ao Aru.

    [:octicons-arrow-right-24: MCP Servers](configuracao/mcp.md)

-   :material-cog-sync-outline:{ .lg .middle } __Overrides por Agente__

    ---

    Use o campo `agent` para trocar modelo, prompt ou conjunto de ferramentas de um sub-agente específico sem afetar o agente geral.

    [:octicons-arrow-right-24: Agentes customizados](agentes/customizados.md)

</div>

## Extensibilidade

Aru foi projetado para ser extensível em todas as camadas. Cada tipo de extensão tem sua própria seção na documentação:

<div class="grid cards" markdown>

-   :material-script-text-outline:{ .lg .middle } __Skills__

    ---

    Capacidades plugáveis carregadas sob demanda a partir de `.aru/skills/`. Defina prompts, ferramentas permitidas e arquivos auxiliares em uma pasta por skill.

    [:octicons-arrow-right-24: Ver Skills](comandos/skills.md)

-   :material-server-network:{ .lg .middle } __MCP Servers__

    ---

    Conecte servidores Model Context Protocol para expor ferramentas externas (GitHub, Slack, bancos de dados) ao Aru via `aru.json`.

    [:octicons-arrow-right-24: Configurar MCP](configuracao/mcp.md)

-   :material-tools:{ .lg .middle } __Tools Integradas__

    ---

    11 ferramentas nativas prontas para uso: read, edit, write, bash, grep, glob, web search, delegate e mais — cobrindo o fluxo completo de edição de código.

    [:octicons-arrow-right-24: Ferramentas integradas](ferramentas/index.md)

-   :material-wrench-outline:{ .lg .middle } __Tools Customizadas__

    ---

    Crie suas próprias ferramentas em Python e registre-as no Aru. Ideal para integrar APIs internas, automações específicas do projeto ou fluxos proprietários.

    [:octicons-arrow-right-24: Ferramentas customizadas](ferramentas/customizadas.md)

-   :material-account-group-outline:{ .lg .middle } __Custom Agents__

    ---

    Crie sub-agentes especializados com prompt, modelo e conjunto de ferramentas próprios. Delegue tarefas isoladas sem poluir o contexto do agente principal.

    [:octicons-arrow-right-24: Agentes customizados](agentes/customizados.md)

-   :material-puzzle-outline:{ .lg .middle } __Plugins__

    ---

    Sistema de hooks compatível com OpenCode para interceptar eventos do ciclo de vida (tool calls, mensagens, sessões) e estender o comportamento do Aru.

    [:octicons-arrow-right-24: Sistema de Plugins](plugins/index.md) · [Hooks](plugins/hooks.md)

</div>

## Próximos passos

- [Instalação](comecando/instalacao.md)
- [Início Rápido](comecando/inicio-rapido.md)
- [CLI](comecando/cli.md)
- [Configuração](configuracao/index.md)
- [Agentes](agentes/index.md)
- [Ferramentas](ferramentas/index.md)
- [Plugins](plugins/index.md)
