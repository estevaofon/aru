# Providers & LLMs

Aru supports multiple LLM providers through a unified `provider/model` format. You can switch between cloud APIs and local models without changing your workflow.

## Quick Start

```bash
# Switch models in the REPL
/model anthropic/claude-sonnet-4-5
/model ollama/llama3.1
/model openai/gpt-4o
/model groq/llama-3.3-70b-versatile

# Legacy aliases still work
/model sonnet
/model opus
/model haiku
```

## Built-in Providers

| Provider | Key | API Key Env Var | Default Model | Install |
|---|---|---|---|---|
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-4-5` | included |
| OpenAI | `openai` | `OPENAI_API_KEY` | `gpt-4o` | `pip install aru-code[openai]` |
| Ollama | `ollama` | - | `llama3.1` | `pip install aru-code[ollama]` |
| Groq | `groq` | `GROQ_API_KEY` | `llama-3.3-70b-versatile` | `pip install aru-code[groq]` |
| OpenRouter | `openrouter` | `OPENROUTER_API_KEY` | `anthropic/claude-sonnet-4-5` | included |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-chat` | included |

To install all provider dependencies at once:

```bash
pip install -e ".[all-providers]"
```

---

## Provider Setup

### Anthropic (default)

Funciona out of the box. Basta ter a API key configurada.

```env
# .env
ANTHROPIC_API_KEY=sk-ant-...
```

```bash
/model anthropic/claude-sonnet-4-5
/model anthropic/claude-opus-4
/model anthropic/claude-haiku-4-5
/model sonnet   # alias
```

**Models disponíveis:**

| Short Name | Full ID |
|---|---|
| `claude-sonnet-4-5` | `claude-sonnet-4-5-20250929` |
| `claude-opus-4` | `claude-opus-4-20250514` |
| `claude-haiku-3-5` | `claude-haiku-3-5-20241022` |
| `claude-haiku-4-5` | `claude-haiku-4-5-20251001` |

---

### OpenAI

```env
# .env
OPENAI_API_KEY=sk-...
```

```bash
pip install aru-code[openai]

/model openai/gpt-4o
/model openai/gpt-4o-mini
/model openai/gpt-4.1
/model openai/gpt-4.1-mini
/model openai/gpt-4.1-nano
/model openai/o3-mini
```

---

### Ollama (local)

Rode modelos localmente sem API key. Requer o [Ollama](https://ollama.com) instalado e rodando.

```bash
# 1. Instale o Ollama e baixe um modelo
ollama pull llama3.1

# 2. Instale a dependência
pip install aru-code[ollama]

# 3. Use no aru
/model ollama/llama3.1
```

Qualquer modelo instalado no Ollama funciona automaticamente:

```bash
ollama pull deepseek-coder-v2
/model ollama/deepseek-coder-v2

ollama pull codellama
/model ollama/codellama

ollama pull phi3
/model ollama/phi3

ollama pull qwen2.5-coder
/model ollama/qwen2.5-coder
```

Se o Ollama estiver rodando em outra porta ou máquina, configure no `aru.json`:

```json
{
  "providers": {
    "ollama": {
      "base_url": "http://192.168.1.100:11434"
    }
  }
}
```

> **Dica:** Se tool calls não estiverem funcionando corretamente com modelos locais, aumente o `num_ctx` no Ollama para 16k-32k.

---

### Groq

Inferência ultra-rápida na nuvem.

```env
# .env
GROQ_API_KEY=gsk_...
```

```bash
pip install aru-code[groq]

/model groq/llama-3.3-70b-versatile
/model groq/llama-3.1-8b-instant
/model groq/mixtral-8x7b-32768
```

---

### OpenRouter

Acesso a centenas de modelos via uma única API. Os models são dinâmicos — qualquer modelo listado no [OpenRouter](https://openrouter.ai/models) funciona.

```env
# .env
OPENROUTER_API_KEY=sk-or-...
```

```bash
/model openrouter/anthropic/claude-sonnet-4-5
/model openrouter/google/gemini-2.5-pro
/model openrouter/meta-llama/llama-3.3-70b
```

---

### DeepSeek

```env
# .env
DEEPSEEK_API_KEY=sk-...
```

```bash
/model deepseek/deepseek-chat
/model deepseek/deepseek-reasoner
```

---

## Configuration (`aru.json`)

Toda a configuração de providers pode ser feita no `aru.json` na raiz do projeto.

### Definir model default

```json
{
  "models": {
    "default": "ollama/llama3.1",
    "small": "ollama/phi3"
  }
}
```

- **`default`** — Model usado ao iniciar o aru (main agent, planner, executor)
- **`small`** — Model usado por sub-agents (`delegate_task`). Se não definido, aru escolhe automaticamente um model pequeno/rápido do mesmo provider.

### Customizar providers existentes

Override de configuração sobre os defaults built-in:

```json
{
  "providers": {
    "ollama": {
      "base_url": "http://my-gpu-server:11434",
      "default_model": "deepseek-coder-v2",
      "models": {
        "deepseek-coder-v2": { "id": "deepseek-coder-v2:latest" }
      }
    },
    "openai": {
      "base_url": "https://my-proxy.example.com/v1"
    }
  }
}
```

### Adicionar provider custom

Qualquer API compatível com OpenAI funciona via `"type": "openai"`:

```json
{
  "providers": {
    "together": {
      "type": "openai",
      "name": "Together AI",
      "api_key_env": "TOGETHER_API_KEY",
      "base_url": "https://api.together.xyz/v1",
      "default_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
      "models": {
        "llama-3.3-70b": { "id": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "max_tokens": 4096 },
        "qwen-2.5-coder": { "id": "Qwen/Qwen2.5-Coder-32B-Instruct", "max_tokens": 4096 }
      }
    }
  }
}
```

```bash
/model together/llama-3.3-70b
```

### Exemplo completo

```json
{
  "permission": {
    "allow": ["git *", "pytest *", "python *"]
  },
  "models": {
    "default": "anthropic/claude-sonnet-4-5",
    "small": "anthropic/claude-haiku-4-5"
  },
  "providers": {
    "ollama": {
      "base_url": "http://localhost:11434"
    },
    "my-company": {
      "type": "openai",
      "name": "Internal LLM",
      "api_key_env": "INTERNAL_LLM_KEY",
      "base_url": "https://llm.internal.company.com/v1",
      "default_model": "company-coder-v2"
    }
  }
}
```

---

## Sub-agents

Por padrão o sub-agent criado via `delegate_task` (e o explorer) herda o **mesmo model do main agent** — mesmo design do Codex (`build_agent_shared_config` em `multi_agents_common.rs`). Mantém o sub-agent no mesmo provider/credencial, preserva o cache lineage e evita falhas tipo "modelo X não suportado" quando o backend do main agent (ex.: ChatGPT Plus OAuth, que só aceita `gpt-5*`) rejeita um small model hard-coded.

Para usar um model menor/mais barato nos sub-agents, defina a alias `"small"` em `model_aliases`:

```json
{
  "model_aliases": {
    "small": "anthropic/claude-haiku-4-5"
  }
}
```

---

## Environment Variables

| Variable | Provider | Obrigatória |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic | Sim (se usar Anthropic) |
| `OPENAI_API_KEY` | OpenAI | Sim (se usar OpenAI) |
| `GROQ_API_KEY` | Groq | Sim (se usar Groq) |
| `OPENROUTER_API_KEY` | OpenRouter | Sim (se usar OpenRouter) |
| `DEEPSEEK_API_KEY` | DeepSeek | Sim (se usar DeepSeek) |

Coloque no `.env` na raiz do projeto:

```env
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GROQ_API_KEY=gsk_...
```

---

## `/model` Command Reference

```bash
/model                          # Mostra model atual + providers disponíveis
/model sonnet                   # Alias legado
/model anthropic/claude-opus-4  # Provider/model explícito
/model ollama/llama3.1          # Model local via Ollama
/model openai/gpt-4o            # OpenAI
/model groq/llama-3.3-70b-versatile  # Groq
```

A troca de model é instantânea e vale para a sessão atual. O model escolhido é salvo na sessão e restaurado ao usar `aru --resume`.
