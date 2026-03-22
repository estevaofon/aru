# arc 🤖

Um assistente de codificação inteligente inspirado no Claude Code, construído com agentes Agno e Claude da Anthropic.

## 📋 Sobre

**arc** é um clone do Claude Code que utiliza agentes de IA para auxiliar em tarefas de engenharia de software. O projeto implementa uma arquitetura multi-agente onde diferentes agentes especializados trabalham juntos:

- **Planner** 🧠: Analisa o código e cria planos de implementação detalhados
- **Executor** ⚡: Executa mudanças no código baseado em planos ou instruções diretas
- **Arc (General)** 💬: Agente de propósito geral que decide entre planejar ou executar

## ✨ Funcionalidades

- 📁 Leitura, escrita e edição de arquivos
- 🔍 Busca no código (glob patterns e regex)
- 🏃 Execução de comandos shell
- 🗂️ Navegação e análise de estrutura de diretórios
- 💭 Conversação contextual com histórico
- 📝 Planejamento de implementações
- ⚡ Execução automatizada de tarefas

## 🚀 Instalação

### Pré-requisitos

- Python 3.13 ou superior
- Conta na Anthropic com acesso à API

### Passos

1. Clone o repositório:
```bash
git clone <url-do-repositorio>
cd arc
```

2. Crie e ative um ambiente virtual (recomendado):
```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/Mac
source .venv/bin/activate
```

3. Instale as dependências:
```bash
pip install -e .
```

4. Configure a chave da API da Anthropic:
```bash
cp .env.example .env
```

Edite o arquivo `.env` e adicione sua chave:
```
ANTHROPIC_API_KEY=sk-ant-sua-chave-aqui
```

## 🎮 Uso

### Modo Interativo (CLI)

Execute o arc no modo interativo:

```bash
arc
```

ou

```bash
python main.py
```

### Comandos Disponíveis

- **Conversa natural**: Digite normalmente e o arc decidirá como ajudar
- **`/plan <tarefa>`**: Cria um plano de implementação detalhado
- **`/exec [tarefa]`**: Executa o plano atual ou uma tarefa específica
- **`! <comando>`**: Executa um comando shell diretamente
- **`/quit`** ou **`/exit`**: Sai do arc

### Exemplos de Uso

```
arc> /plan criar uma API REST com FastAPI para gerenciar usuários
```

O agente Planner irá:
1. Analisar a estrutura do projeto
2. Buscar por padrões relevantes no código
3. Criar um plano passo-a-passo

```
arc> /exec
```

O agente Executor irá implementar o plano criado.

```
arc> ! pytest tests/
```

Executa os testes diretamente no shell.

```
arc> adicione validação de email na função create_user
```

O agente general irá executar diretamente a tarefa.

## 🏗️ Estrutura do Projeto

```
arc/
├── arc/
│   ├── agents/          # Agentes especializados
│   │   ├── planner.py   # Agente de planejamento
│   │   └── executor.py  # Agente de execução
│   ├── tools/           # Ferramentas disponíveis para os agentes
│   │   └── codebase.py  # Ferramentas de manipulação de código
│   └── cli.py           # Interface de linha de comando
├── main.py              # Ponto de entrada
├── pyproject.toml       # Configuração do projeto
└── README.md
```

## 🛠️ Tecnologias

- **[Agno](https://github.com/agno-agi/agno)**: Framework para construção de agentes de IA
- **[Anthropic Claude](https://www.anthropic.com/)**: Modelo de linguagem (claude-sonnet-4-5)
- **[Rich](https://rich.readthedocs.io/)**: Interface de terminal bonita e interativa
- **SQLAlchemy**: ORM para persistência de dados
- **python-dotenv**: Gerenciamento de variáveis de ambiente

## 🤝 Contribuindo

Contribuições são bem-vindas! Sinta-se à vontade para:

1. Fazer fork do projeto
2. Criar uma branch para sua feature (`git checkout -b feature/MinhaFeature`)
3. Commit suas mudanças (`git commit -m 'Adiciona MinhaFeature'`)
4. Push para a branch (`git push origin feature/MinhaFeature`)
5. Abrir um Pull Request

## 📝 Licença

Este projeto é um clone educacional do Claude Code para fins de aprendizado.

## 🔒 Segurança

- Nunca compartilhe sua chave da API da Anthropic
- O arquivo `.env` está no `.gitignore` para evitar commits acidentais
- Revise os comandos shell antes de executá-los via arc

## 📚 Recursos Adicionais

- [Documentação do Agno](https://docs.agno.dev/)
- [API da Anthropic](https://docs.anthropic.com/)
- [Claude Sonnet 4.5 Documentation](https://www.anthropic.com/claude)

## 🐛 Problemas Conhecidos

- O histórico da conversa é limitado a 40 mensagens (últimas 10 exibidas no contexto)
- Comandos shell são executados no diretório de trabalho atual

## 📧 Contato

Para dúvidas, sugestões ou problemas, abra uma issue no repositório.

---

Desenvolvido com ❤️ usando Claude e Agno