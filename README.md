# aru

Um assistente de codificação inteligente para o terminal, powered by LLMs e [Agno](https://github.com/agno-agi/agno) agents.
</br></br>
<img width="600" alt="image" src="https://github.com/user-attachments/assets/164298dc-8ac5-46c4-80ed-6c782373f61e" />


## Destaques

- **Arquitetura Multi-Agente** — Agentes especializados para planejamento, execução e conversação
- **CLI Interativa** — Respostas em streaming, paste multi-linha, gerenciamento de sessões
- **16 Ferramentas Integradas** — Operações de arquivo, busca de código, shell, busca web, busca semântica, delegação de tarefas
- **Planejamento de Tarefas** — Quebra de tarefas complexas em etapas com execução automática
- **Multi-Provider** — Anthropic (Sonnet, Opus, Haiku), Ollama (modelos locais), OpenAI, Groq e OpenRouter
- **Busca Semântica** — Busca de código baseada em embeddings com chromadb
- **Comandos e Skills Personalizados** — Estenda aru via diretório `.agents/`
- **Suporte MCP** — Integração com Model Context Protocol servers

## Início Rápido

### 1. Instalar

```bash
pip install -e .
```

> **Requisitos:** Python 3.13+

### 2. Configurar a API Key

Aru usa o **Claude Sonnet 4.5** da Anthropic como modelo padrão. Você precisa de uma [chave de API da Anthropic](https://console.anthropic.com/) para começar.

Crie um arquivo `.env` na raiz do projeto:

```bash
cp .env.example .env
```

Edite o `.env` com sua chave:

```env
ANTHROPIC_API_KEY=sk-ant-sua-chave-aqui
```

> Usando outro provider? Veja a seção [Modelos e Providers](#modelos-e-providers) para configurar OpenAI, Ollama, Groq, etc.

### 3. Executar

```bash
aru
```

### Instalação Global (rodar `aru` de qualquer lugar)

Para usar o aru como comando global no terminal, crie um ambiente virtual dedicado e um script wrapper:

<details>
<summary><strong>Windows</strong></summary>

1. Crie o ambiente virtual e instale:
```bash
python -m venv C:\aru-env
C:\aru-env\Scripts\pip install -e C:\caminho\para\aru
```

2. Crie `aru.bat` em uma pasta no seu `PATH` (ex: `C:\Users\<user>\bin\`):
```bat
@echo off
C:\aru-env\Scripts\python -m aru.cli %*
```

</details>

<details>
<summary><strong>Linux / macOS</strong></summary>

1. Crie o ambiente virtual e instale:
```bash
python3 -m venv ~/.aru-env
~/.aru-env/bin/pip install -e /caminho/para/aru
```

2. Crie o script `~/.local/bin/aru`:
```bash
#!/bin/bash
~/.aru-env/bin/python -m aru.cli "$@"
```

3. Torne executável:
```bash
chmod +x ~/.local/bin/aru
```

</details>

Pronto — agora `aru` funciona de qualquer diretório.

## Uso

### Comandos

| Comando | Descrição |
|---------|-----------|
| Linguagem natural | Apenas digite — aru cuida do resto |
| `/plan <tarefa>` | Cria plano de implementação detalhado |
| `/model [provider/modelo]` | Alterna modelos e providers |
| `/mcp` | Lista servidores e ferramentas MCP disponíveis |
| `/commands` | Lista comandos personalizados |
| `/skills` | Lista skills disponíveis |
| `/sessions` | Lista sessões recentes |
| `/help` | Mostra todos os comandos |
| `! <comando>` | Executa comandos shell |
| `/quit` ou `/exit` | Sai do aru |

### Opções CLI

```bash
aru                                    # Inicia nova sessão
aru --resume <id>                      # Retoma sessão
aru --resume last                      # Retoma última sessão
aru --list                             # Lista sessões
aru --dangerously-skip-permissions     # Pula prompts de permissão
```

### Exemplos

```
aru> /plan criar uma REST API com FastAPI para gerenciar usuários

aru> refatorar o módulo de autenticação para usar tokens JWT

aru> ! pytest tests/ -v

aru> /model ollama/codellama
```

## Configuração

### Modelos e Providers

Por padrão, aru utiliza o **Claude Sonnet 4.5** (Anthropic). Você pode alternar para qualquer provider suportado durante a sessão com `/model`:

| Provider | Comando | API Key (`.env`) | Instalação extra |
|----------|---------|-------------------|------------------|
| **Anthropic** | `/model anthropic/claude-sonnet-4-5` | `ANTHROPIC_API_KEY` | — (incluído) |
| **Ollama** | `/model ollama/llama3.1` | — (local) | `pip install -e ".[ollama]"` |
| **OpenAI** | `/model openai/gpt-4o` | `OPENAI_API_KEY` | `pip install -e ".[openai]"` |
| **Groq** | `/model groq/llama-3.3-70b-versatile` | `GROQ_API_KEY` | `pip install -e ".[groq]"` |
| **OpenRouter** | `/model openrouter/anthropic/claude-sonnet-4-5` | `OPENROUTER_API_KEY` | `pip install -e ".[openai]"` |

Para instalar todos os providers de uma vez:

```bash
pip install -e ".[all-providers]"
```

#### Ollama (modelos locais)

Para rodar modelos localmente sem API key, instale o [Ollama](https://ollama.com/), inicie o servidor e use qualquer modelo instalado:

```bash
ollama serve                    # Inicia o servidor Ollama
ollama pull codellama           # Baixa um modelo
aru                             # Inicia aru
# Dentro do aru:
/model ollama/codellama
```

#### Configurando o modelo padrão

Você pode definir o provider/modelo padrão no `aru.json` para não precisar trocar manualmente toda sessão:

```json
{
  "models": {
    "default": "ollama/codellama",
    "small": "ollama/llama3.1"
  }
}
```

O campo `small` define o modelo usado para sub-agentes e tarefas delegadas.

### Permissões (`aru.json`)

O arquivo `aru.json` na raiz do projeto controla quais comandos shell o aru pode executar **sem pedir confirmação**:

```json
{
  "permission": {
    "allow": [
      "git *",
      "npm *",
      "pytest *",
      "python *"
    ]
  }
}
```

Cada entrada é um padrão glob. Qualquer comando que não se encaixe em um padrão listado pedirá confirmação antes de executar.

> O `aru.json` também pode ser colocado em `.aru/config.json`.

#### Exemplo completo de `aru.json`

```json
{
  "models": {
    "default": "anthropic/claude-sonnet-4-5",
    "small": "anthropic/claude-haiku-4-5"
  },
  "permission": {
    "allow": [
      "git *",
      "npm *",
      "pytest *",
      "python *"
    ]
  }
}
```

### AGENTS.md

Coloque um arquivo `AGENTS.md` na raiz do seu projeto com instruções personalizadas que serão anexadas a todos os prompts do sistema dos agentes.

### Diretório `.agents/`

```
.agents/
├── commands/       # Comandos slash personalizados (nome do arquivo = nome do comando)
│   └── deploy.md   # Uso: /deploy <args>
└── skills/         # Skills/personas personalizadas
    └── review.md   # Carregado como instruções adicionais do agente
```

Arquivos de comando suportam frontmatter com `description` e a variável template `$INPUT` para argumentos.

### Suporte MCP (Model Context Protocol)

Aru pode carregar ferramentas de servidores MCP. Configure em `.aru/mcp_config.json`:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/caminho/permitido"]
    }
  }
}
```

## Agentes

| Agente | Papel | Ferramentas |
|--------|-------|-------------|
| **Planner** | Analisa codebase, cria planos de implementação estruturados | Ferramentas somente leitura, busca, web |
| **Executor** | Implementa mudanças de código baseadas em planos ou instruções | Todas as ferramentas incluindo delegação |
| **General** | Lida com conversação e operações simples | Todas as ferramentas incluindo delegação |

## Ferramentas

### Operações de Arquivo
- `read_file` — Lê arquivos com suporte a range de linhas e detecção binária
- `write_file` / `write_files` — Escreve arquivos únicos ou em lote
- `edit_file` / `edit_files` — Edições find-replace em múltiplos arquivos

### Busca & Descoberta
- `glob_search` — Encontra arquivos por padrão (respeita .gitignore)
- `grep_search` — Busca de conteúdo com regex e filtro de arquivos
- `list_directory` — Listagem de diretório com filtro gitignore
- `semantic_search` — Busca conceitual em linguagem natural via embeddings chromadb
- `rank_files` — Ranking de relevância de arquivos multi-fator (semântico, nome, estrutura, recência)

### Análise de Código
- `code_structure` — Extrai classes, funções, imports via AST tree-sitter
- `find_dependencies` — Analisa relacionamentos de imports entre arquivos

### Shell & Web
- `bash` — Executa comandos shell com gates de permissão
- `web_search` — Busca na web via DuckDuckGo
- `web_fetch` — Busca URLs e converte HTML para texto legível

### Avançado
- `delegate_task` — Gera sub-agentes autônomos para execução paralela de tarefas

## Arquitetura

```
aru-code/
├── aru/
│   ├── cli.py              # CLI interativa com display em streaming
│   ├── config.py           # Carregador de configuração (AGENTS.md, .agents/)
│   ├── providers.py        # Abstração multi-provider de LLMs
│   ├── agents/
│   │   ├── planner.py      # Agente de planejamento
│   │   └── executor.py     # Agente de execução
│   └── tools/
│       ├── codebase.py     # 16 ferramentas principais
│       ├── ast_tools.py    # Análise de código tree-sitter
│       ├── indexer.py      # Indexação semântica chromadb
│       ├── ranker.py       # Ranking de relevância de arquivos
│       ├── mcp_client.py   # Cliente MCP
│       └── gitignore.py    # Filtro gitignore-aware
├── aru.json                # Permissões e configuração de modelos
├── .env                    # API keys (não commitado)
├── .aru/                   # Dados locais (sessões, índice, embeddings)
└── pyproject.toml
```

## Construído Com

- **[Agno](https://github.com/agno-agi/agno)** — Framework de agentes com orquestração de ferramentas
- **[Anthropic Claude](https://www.anthropic.com/)** — Sonnet 4.5, Opus 4, Haiku 3.5
- **[chromadb](https://www.trychroma.com/)** — Embeddings de busca semântica
- **[tree-sitter](https://tree-sitter.github.io/)** — Análise de código baseada em AST
- **[Rich](https://rich.readthedocs.io/)** — UI de terminal
- **[prompt-toolkit](https://python-prompt-toolkit.readthedocs.io/)** — Manipulação avançada de input

## Desenvolvimento

```bash
# Instalar com dependências de desenvolvimento
pip install -e ".[dev]"

# Executar testes
pytest

# Executar testes com cobertura
pytest --cov=aru --cov-report=html
```

---

Construído com Claude e Agno
