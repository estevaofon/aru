# aru

Um assistente de codificação inteligente powered by Claude e Agno agents.

## Destaques

- **Arquitetura Multi-Agente** — Agentes especializados para planejamento, execução e conversação
- **CLI Interativa** — Respostas em streaming, paste multi-linha, gerenciamento de sessões
- **16 Ferramentas Integradas** — Operações de arquivo, busca de código, shell, busca web, busca semântica, delegação de tarefas
- **Planejamento de Tarefas** — Quebra de tarefas complexas em etapas com execução automática
- **Flexibilidade de Modelos** — Anthropic (Sonnet, Opus, Haiku), Ollama (modelos locais), OpenAI, Groq e OpenRouter
- **Busca Semântica** — Busca de código baseada em embeddings com chromadb
- **Comandos e Skills Personalizados** — Estenda aru via diretório `.agents/`
- **Suporte MCP** — Integração com Model Context Protocol servers

## Início Rápido

```bash
# Instalar
pip install -e .

# Configurar
cp .env.example .env
# Edite .env e adicione: ANTHROPIC_API_KEY=sk-ant-sua-chave-aqui

# Executar
aru
```

**Requisitos:** Python 3.13+ e uma [chave de API da Anthropic](https://console.anthropic.com/)

### Instalação Global (rodar `aru` de qualquer lugar)

A forma mais simples de usar o aru globalmente é criar um ambiente virtual dedicado e um script wrapper:

**Windows (.bat):**

1. Crie o ambiente virtual e instale o aru:
```bash
python -m venv C:\aru-env
C:\aru-env\Scripts\pip install -e /caminho/para/aru
```

2. Crie um arquivo `aru.bat` em uma pasta que esteja no seu `PATH` (ex: `C:\Users\<user>\bin\aru.bat`):
```bat
@echo off
C:\aru-env\Scripts\python -m aru.cli %*
```

**Linux/macOS (.sh):**

1. Crie o ambiente virtual e instale o aru:
```bash
python3 -m venv ~/.aru-env
~/.aru-env/bin/pip install -e /caminho/para/aru
```

2. Crie um script `aru` em um diretório no seu `PATH` (ex: `~/.local/bin/aru`):
```bash
#!/bin/bash
~/.aru-env/bin/python -m aru.cli "$@"
```

3. Torne executável:
```bash
chmod +x ~/.local/bin/aru
```

Agora você pode rodar `aru` de qualquer diretório.

## Uso

### Comandos

| Comando | Descrição |
|---------|-----------|
| Linguagem natural | Apenas digite — aru cuida do resto |
| `/plan <tarefa>` | Cria plano de implementação detalhado |
| `/model [nome]` | Alterna modelos (sonnet/opus/haiku) |
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

aru> /model opus
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

## Configuração

Aru suporta configuração a nível de projeto através de:

### Modelos e Providers

Por padrão, aru utiliza o **Claude Sonnet 4.5** da Anthropic. Você pode alternar modelos e providers de LLM a qualquer momento:

```bash
# Alternar modelos Anthropic
/model anthropic/claude-opus-4
/model anthropic/claude-haiku-4-5

# Usar Ollama (modelos locais)
pip install -e ".[ollama]"
/model ollama/llama3.1
/model ollama/codellama

# Usar OpenAI
pip install -e ".[openai]"
/model openai/gpt-4o

# Usar Groq
pip install -e ".[groq]"
/model groq/llama-3.3-70b-versatile

# Instalar todos os providers
pip install -e ".[all-providers]"
```

Para usar **Ollama**, basta ter o [Ollama](https://ollama.com/) rodando localmente (`ollama serve`) e instalar a dependência extra. Qualquer modelo instalado no Ollama pode ser utilizado.

Você também pode configurar providers e modelos padrão no `aru.json`:

```json
{
  "models": {
    "default": "ollama/codellama",
    "small": "ollama/llama3.1"
  }
}
```

### Permissões (aru.json)

O arquivo `aru.json` na raiz do projeto permite configurar permissões para comandos que o aru pode executar sem pedir confirmação:

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

Cada entrada é um padrão glob. Comandos que não se encaixam em nenhum padrão pedirão confirmação antes de executar. Use `--dangerously-skip-permissions` na CLI para pular todas as confirmações (não recomendado para produção).

O `aru.json` também pode ser colocado em `.aru/config.json` como alternativa.

### AGENTS.md
Coloque um arquivo `AGENTS.md` na raiz do seu projeto com instruções personalizadas que serão anexadas a todos os prompts do sistema dos agentes.

### Diretório .agents/

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

## Arquitetura

```
aru-code/
├── aru/
│   ├── cli.py              # CLI interativa com display em streaming (1306 LOC)
│   ├── config.py           # Carregador de configuração (AGENTS.md, .agents/)
│   ├── agents/
│   │   ├── planner.py      # Agente de planejamento (60 LOC)
│   │   └── executor.py     # Agente de execução (57 LOC)
│   └── tools/
│       ├── codebase.py     # 16 ferramentas principais (1043 LOC)
│       ├── ast_tools.py    # Análise de código tree-sitter (402 LOC)
│       ├── indexer.py      # Indexação semântica chromadb (332 LOC)
│       ├── ranker.py       # Ranking de relevância de arquivos (280 LOC)
│       ├── mcp_client.py   # Cliente MCP (145 LOC)
│       └── gitignore.py    # Filtro gitignore-aware (104 LOC)
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
