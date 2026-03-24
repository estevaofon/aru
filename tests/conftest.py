"""Pytest configuration and shared fixtures."""

import os
import sys
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

# Prevent arc.cli from wrapping sys.stdout/stderr on Windows during tests,
# which would break pytest's capture mechanism.
sys._called_from_test = True


@pytest.fixture
def temp_dir() -> Iterator[Path]:
    """Create a temporary directory for testing.
    
    Yields:
        Path to temporary directory that is cleaned up after the test.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def project_dir(temp_dir: Path) -> Path:
    """Create a mock project directory with common structure.
    
    Creates:
        - README.md
        - AGENTS.md
        - .agents/commands/
        - .agents/skills/
        - .gitignore
        - src/ directory with sample files
    """
    # Create README.md
    readme = temp_dir / "README.md"
    readme.write_text("# Test Project\n\nA sample project for testing.")
    
    # Create AGENTS.md
    agents_md = temp_dir / "AGENTS.md"
    agents_md.write_text("# Project Instructions\n\nCustom instructions for agents.")
    
    # Create .agents structure
    agents_dir = temp_dir / ".agents"
    agents_dir.mkdir()
    
    commands_dir = agents_dir / "commands"
    commands_dir.mkdir()
    
    skills_dir = agents_dir / "skills"
    skills_dir.mkdir()
    
    # Create sample command
    deploy_cmd = commands_dir / "deploy.md"
    deploy_cmd.write_text(
        "---\n"
        "description: Deploy the application\n"
        "---\n"
        "Deploy with args: $INPUT"
    )
    
    # Create sample skill
    review_skill = skills_dir / "review.md"
    review_skill.write_text(
        "---\n"
        "description: Code review assistant\n"
        "---\n"
        "Review code with best practices."
    )
    
    # Create .gitignore
    gitignore = temp_dir / ".gitignore"
    gitignore.write_text(
        "__pycache__/\n"
        "*.pyc\n"
        ".venv/\n"
        "build/\n"
        "dist/\n"
    )
    
    # Create src directory with sample files
    src_dir = temp_dir / "src"
    src_dir.mkdir()
    
    (src_dir / "main.py").write_text(
        "def main():\n"
        "    print('Hello, world!')\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )
    
    (src_dir / "utils.py").write_text(
        "import os\n"
        "from typing import Optional\n"
        "\n"
        "class Helper:\n"
        "    def __init__(self, name: str):\n"
        "        self.name = name\n"
        "\n"
        "    def greet(self) -> str:\n"
        "        return f'Hello, {self.name}'\n"
        "\n"
        "def get_env(key: str, default: Optional[str] = None) -> Optional[str]:\n"
        "    return os.getenv(key, default)\n"
    )
    
    # Create ignored directories
    (temp_dir / "__pycache__").mkdir()
    (temp_dir / "__pycache__" / "test.pyc").write_text("binary")
    
    (temp_dir / ".venv").mkdir()
    (temp_dir / ".venv" / "lib").mkdir()
    
    return temp_dir


@pytest.fixture
def sample_python_file(temp_dir: Path) -> Path:
    """Create a sample Python file for AST testing.
    
    Returns:
        Path to a Python file with various code structures.
    """
    content = '''
import os
import sys
from typing import List, Optional
from pathlib import Path

CONSTANT = 42

class BaseClass:
    """A base class."""
    
    def __init__(self):
        self.value = 0

class DerivedClass(BaseClass):
    """A derived class."""
    
    def __init__(self, name: str):
        super().__init__()
        self.name = name
    
    def get_name(self) -> str:
        return self.name
    
    @property
    def display_name(self) -> str:
        return f"Name: {self.name}"

def simple_function():
    """A simple function."""
    pass

def function_with_args(x: int, y: int = 10) -> int:
    """Function with arguments."""
    return x + y

async def async_function(data: List[str]) -> Optional[str]:
    """An async function."""
    return data[0] if data else None

if __name__ == "__main__":
    print("Running")
'''
    filepath = temp_dir / "sample.py"
    filepath.write_text(content)
    return filepath


@pytest.fixture
def git_repo_dir(temp_dir: Path) -> Path:
    """Create a temporary directory with .git to simulate a git repository.
    
    Returns:
        Path to directory with .git folder.
    """
    git_dir = temp_dir / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n")
    return temp_dir


@pytest.fixture
def sample_files_for_ranking(temp_dir: Path) -> dict[str, Path]:
    """Create multiple files for testing file ranking.
    
    Returns:
        Dictionary mapping file descriptions to their paths.
    """
    files = {}
    
    # Authentication related file
    auth_file = temp_dir / "auth.py"
    auth_file.write_text(
        "import jwt\n"
        "\n"
        "def authenticate_user(token: str) -> bool:\n"
        "    return jwt.decode(token)\n"
    )
    files["auth"] = auth_file
    
    # Database file
    db_file = temp_dir / "database.py"
    db_file.write_text(
        "import sqlalchemy\n"
        "\n"
        "def connect_db():\n"
        "    pass\n"
    )
    files["database"] = db_file
    
    # Config file
    config_file = temp_dir / "config.py"
    config_file.write_text(
        "APP_NAME = 'test'\n"
        "DEBUG = True\n"
    )
    files["config"] = config_file
    
    # Nested directory structure
    api_dir = temp_dir / "api"
    api_dir.mkdir()
    
    routes_file = api_dir / "routes.py"
    routes_file.write_text(
        "from fastapi import APIRouter\n"
        "\n"
        "router = APIRouter()\n"
    )
    files["routes"] = routes_file
    
    return files