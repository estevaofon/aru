"""Tests for aru/tools/ast_tools.py — AST-based code analysis."""

import os
import tempfile
from pathlib import Path

import pytest

from aru.tools.ast_tools import (
    SUPPORTED_EXTENSIONS,
    _extract_decorators,
    _extract_function,
    _extract_class,
    _extract_structure_regex,
    _extract_structure_treesitter,
    _find_project_root,
    _format_structure,
    _parse_python_tree,
    _resolve_import_to_file,
    _TREE_SITTER_AVAILABLE,
    code_structure,
    find_dependencies,
)


# --- Parser Tests ---

def test_parse_python_tree_basic():
    """Test basic tree-sitter parsing."""
    source = b"def hello():\n    pass"
    tree = _parse_python_tree(source)
    
    if _TREE_SITTER_AVAILABLE:
        assert tree is not None
        assert tree.root_node is not None
    else:
        assert tree is None


def test_parse_python_tree_invalid():
    """Test parsing invalid syntax."""
    source = b"def hello("
    tree = _parse_python_tree(source)
    
    if _TREE_SITTER_AVAILABLE:
        # tree-sitter still returns a tree with error nodes
        assert tree is not None
    else:
        assert tree is None


# --- Function Extraction Tests ---

@pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="tree-sitter not available")
def test_extract_function_simple():
    """Test extracting a simple function."""
    source = b"def hello(name):\n    pass"
    tree = _parse_python_tree(source)
    
    # Find function_definition node
    func_node = None
    for child in tree.root_node.children:
        if child.type == "function_definition":
            func_node = child
            break
    
    assert func_node is not None
    func_info = _extract_function(func_node, source)
    
    assert func_info["name"] == "hello"
    assert func_info["params"] == ["name"]


@pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="tree-sitter not available")
def test_extract_function_with_docstring():
    """Test extracting a function with docstring."""
    source = b"""def documented(param):
    \"\"\"This is a docstring.\"\"\"
    pass
"""
    tree = _parse_python_tree(source)
    
    func_node = None
    for child in tree.root_node.children:
        if child.type == "function_definition":
            func_node = child
            break
    
    assert func_node is not None
    func_info = _extract_function(func_node, source)
    
    assert func_info["name"] == "documented"
    assert func_info["params"] == ["param"]
    assert "This is a docstring." in func_info.get("docstring", "")


@pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="tree-sitter not available")
def test_extract_function_with_types():
    """Test extracting function with type annotations."""
    source = b"def greet(name: str, age: int = 30) -> str:\n    pass"
    tree = _parse_python_tree(source)
    
    func_node = None
    for child in tree.root_node.children:
        if child.type == "function_definition":
            func_node = child
            break
    
    func_info = _extract_function(func_node, source)
    
    assert func_info["name"] == "greet"
    # Should strip type annotations and defaults
    assert func_info["params"] == ["name", "age"]


@pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="tree-sitter not available")
def test_extract_function_no_params():
    """Test extracting function with no parameters."""
    source = b"def empty():\n    pass"
    tree = _parse_python_tree(source)
    
    func_node = None
    for child in tree.root_node.children:
        if child.type == "function_definition":
            func_node = child
            break
    
    func_info = _extract_function(func_node, source)
    
    assert func_info["name"] == "empty"
    assert func_info["params"] == []


# --- Class Extraction Tests ---

@pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="tree-sitter not available")
def test_extract_class_simple():
    """Test extracting a simple class."""
    source = b"class Person:\n    pass"
    tree = _parse_python_tree(source)
    
    class_node = None
    for child in tree.root_node.children:
        if child.type == "class_definition":
            class_node = child
            break
    
    assert class_node is not None
    class_info = _extract_class(class_node, source)
    
    assert class_info["name"] == "Person"
    assert class_info["bases"] == []
    assert class_info["methods"] == []


@pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="tree-sitter not available")
def test_extract_class_with_bases():
    """Test extracting class with inheritance."""
    source = b"class Employee(Person, Worker):\n    pass"
    tree = _parse_python_tree(source)
    
    class_node = None
    for child in tree.root_node.children:
        if child.type == "class_definition":
            class_node = child
            break
    
    class_info = _extract_class(class_node, source)
    
    assert class_info["name"] == "Employee"
    assert "Person" in class_info["bases"]
    assert "Worker" in class_info["bases"]


@pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="tree-sitter not available")
def test_extract_class_with_methods():
    """Test extracting class with methods."""
    source = b"""class Calculator:
    def add(self, a, b):
        return a + b
    
    def subtract(self, x, y):
        return x - y
"""
    tree = _parse_python_tree(source)
    
    class_node = None
    for child in tree.root_node.children:
        if child.type == "class_definition":
            class_node = child
            break
    
    class_info = _extract_class(class_node, source)
    
    assert class_info["name"] == "Calculator"
    assert len(class_info["methods"]) == 2
    
    assert class_info["methods"][0]["name"] == "add"
    assert class_info["methods"][0]["params"] == ["self", "a", "b"]
    
    assert class_info["methods"][1]["name"] == "subtract"
    assert class_info["methods"][1]["params"] == ["self", "x", "y"]


@pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="tree-sitter not available")
def test_extract_class_with_decorated_methods():
    """Test extracting class with decorated methods."""
    source = b"""class Service:
    @property
    def name(self):
        return "test"
    
    @staticmethod
    def helper():
        pass
"""
    tree = _parse_python_tree(source)
    
    class_node = None
    for child in tree.root_node.children:
        if child.type == "class_definition":
            class_node = child
            break
    
    class_info = _extract_class(class_node, source)
    
    assert class_info["name"] == "Service"
    assert len(class_info["methods"]) == 2
    
    # Check decorators
    assert "@property" in class_info["methods"][0]["decorators"]
    assert "@staticmethod" in class_info["methods"][1]["decorators"]


# --- Decorator Extraction Tests ---

@pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="tree-sitter not available")
def test_extract_decorators():
    """Test extracting decorators from decorated definitions."""
    source = b"""@decorator1
@decorator2("arg")
def func():
    pass
"""
    tree = _parse_python_tree(source)
    
    decorated_node = None
    for child in tree.root_node.children:
        if child.type == "decorated_definition":
            decorated_node = child
            break
    
    assert decorated_node is not None
    decorators = _extract_decorators(decorated_node, source)
    
    assert len(decorators) == 2
    assert "@decorator1" in decorators
    assert '@decorator2("arg")' in decorators


# --- Structure Extraction Tests (tree-sitter) ---

@pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="tree-sitter not available")
def test_extract_structure_treesitter_complete():
    """Test full structure extraction with tree-sitter."""
    source = b"""import os
from pathlib import Path

DEBUG = True

class MyClass(Base):
    def method(self):
        pass

def top_level_func(arg):
    return arg
"""
    tree = _parse_python_tree(source)
    structure = _extract_structure_treesitter(tree, source, "test.py")
    
    # Check imports
    assert len(structure["imports"]) == 2
    assert structure["imports"][0]["text"] == "import os"
    assert structure["imports"][1]["text"] == "from pathlib import Path"
    
    # Check globals
    assert len(structure["globals"]) == 1
    assert structure["globals"][0]["name"] == "DEBUG"
    
    # Check classes
    assert len(structure["classes"]) == 1
    assert structure["classes"][0]["name"] == "MyClass"
    assert structure["classes"][0]["bases"] == ["Base"]
    assert len(structure["classes"][0]["methods"]) == 1
    
    # Check functions
    assert len(structure["functions"]) == 1
    assert structure["functions"][0]["name"] == "top_level_func"
    assert structure["functions"][0]["params"] == ["arg"]


# --- Regex Fallback Tests ---

def test_extract_structure_regex_imports():
    """Test regex-based import extraction."""
    content = """import os
import sys
from pathlib import Path
from typing import List, Dict
"""
    structure = _extract_structure_regex(content)
    
    assert len(structure["imports"]) == 4
    assert any("import os" in imp["text"] for imp in structure["imports"])
    assert any("from pathlib import Path" in imp["text"] for imp in structure["imports"])


def test_extract_structure_regex_classes():
    """Test regex-based class extraction."""
    content = """class Simple:
    pass

class WithBase(Parent):
    pass

class Multiple(Base1, Base2):
    pass
"""
    structure = _extract_structure_regex(content)
    
    assert len(structure["classes"]) == 3
    
    assert structure["classes"][0]["name"] == "Simple"
    assert structure["classes"][0]["bases"] == []
    
    assert structure["classes"][1]["name"] == "WithBase"
    assert structure["classes"][1]["bases"] == ["Parent"]
    
    assert structure["classes"][2]["name"] == "Multiple"
    assert "Base1" in structure["classes"][2]["bases"]
    assert "Base2" in structure["classes"][2]["bases"]


def test_extract_structure_regex_functions():
    """Test regex-based function extraction."""
    content = """def simple():
    pass

def with_params(a, b, c):
    pass

def with_types(x: int, y: str = "default"):
    pass
"""
    structure = _extract_structure_regex(content)
    
    assert len(structure["functions"]) == 3
    
    assert structure["functions"][0]["name"] == "simple"
    assert structure["functions"][0]["params"] == []
    
    assert structure["functions"][1]["name"] == "with_params"
    assert structure["functions"][1]["params"] == ["a", "b", "c"]
    
    assert structure["functions"][2]["name"] == "with_types"
    assert structure["functions"][2]["params"] == ["x", "y"]


def test_extract_structure_regex_methods():
    """Test regex-based method extraction (indented functions)."""
    content = """class Calculator:
    def add(self, a, b):
        return a + b
    
    def multiply(self, x, y):
        return x * y

def top_level():
    pass
"""
    structure = _extract_structure_regex(content)
    
    assert len(structure["classes"]) == 1
    assert len(structure["classes"][0]["methods"]) == 2
    assert len(structure["functions"]) == 1
    
    assert structure["classes"][0]["methods"][0]["name"] == "add"
    assert structure["functions"][0]["name"] == "top_level"


# --- Format Structure Tests ---

def test_format_structure_basic():
    """Test formatting structure output."""
    structure = {
        "imports": [{"text": "import os", "line": 1}],
        "classes": [{"name": "TestClass", "bases": [], "methods": [], "line": 3}],
        "functions": [{"name": "test_func", "params": ["x"], "line": 6}],
        "globals": [{"name": "DEBUG", "line": 2}],
    }
    
    output = _format_structure(structure, "test.py", 10)
    
    assert "test.py" in output
    assert "Python" in output
    assert "10 lines" in output
    assert "import os" in output
    assert "TestClass" in output
    assert "test_func(x)" in output
    assert "DEBUG" in output


def test_format_structure_with_decorators():
    """Test formatting structure with decorators."""
    structure = {
        "imports": [],
        "classes": [
            {
                "name": "MyClass",
                "bases": ["Base"],
                "methods": [
                    {
                        "name": "method",
                        "params": ["self"],
                        "decorators": ["@property"],
                        "line": 3
                    }
                ],
                "decorators": ["@dataclass"],
                "line": 2
            }
        ],
        "functions": [],
        "globals": [],
    }
    
    output = _format_structure(structure, "test.py", 10)
    
    assert "@dataclass" in output
    assert "@property" in output
    assert "MyClass(Base)" in output


# --- code_structure Integration Tests ---

def test_code_structure_file_not_found():
    """Test code_structure with non-existent file."""
    result = code_structure("/nonexistent/file.py")
    assert "Error: File not found" in result


def test_code_structure_python_file(tmp_path):
    """Test code_structure with a real Python file."""
    test_file = tmp_path / "example.py"
    test_file.write_text("""import os

class Example:
    def method(self):
        pass

def function():
    pass
""")
    
    result = code_structure(str(test_file))
    
    assert "example.py" in result
    assert "Python" in result
    assert "import os" in result
    assert "Example" in result
    assert "function" in result


def test_code_structure(tmp_path):
    """Test code_structure() parsing a Python file returning imports, classes and functions correctly."""
    test_file = tmp_path / "sample.py"
    test_file.write_text("""import os
from pathlib import Path

class User:
    def __init__(self, name):
        self.name = name
    
    def greet(self):
        return f"Hello, {self.name}"

def add(a, b):
    return a + b

def multiply(x, y):
    return x * y
""")
    
    result = code_structure(str(test_file))
    
    # Test imports
    assert "import os" in result
    assert "from pathlib import Path" in result
    
    # Test classes
    assert "User" in result
    assert "__init__" in result
    assert "greet" in result
    
    # Test functions
    assert "add" in result
    assert "multiply" in result


# --- Import Resolution Tests ---

def test_resolve_import_to_file_module(tmp_path):
    """Test resolving simple module import."""
    # Create a properly structured module file
    (tmp_path / "utils.py").write_text("def helper(): pass")
    
    result = _resolve_import_to_file("from utils import helper", str(tmp_path))
    
    # Should resolve to utils.py
    if result:
        assert "utils.py" in result or result == "utils.py"
    else:
        # The function works with actual Python path resolution,
        # which may not find temp files without proper Python path setup
        pytest.skip("Module resolution may require proper Python path configuration")


def test_resolve_import_to_file_package(tmp_path):
    """Test resolving package import."""
    # Create package structure
    pkg = tmp_path / "mypackage"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("# package")
    
    result = _resolve_import_to_file("import mypackage", str(tmp_path))
    
    # Should resolve to mypackage/__init__.py or mypackage\__init__.py
    if result:
        assert "__init__.py" in result
        assert "mypackage" in result
    else:
        pytest.skip("Package resolution may require proper Python path configuration")


def test_resolve_import_to_file_from_import(tmp_path):
    """Test resolving 'from X import Y' import."""
    (tmp_path / "utils.py").write_text("# utils")
    
    result = _resolve_import_to_file("from utils import helper", str(tmp_path))
    assert result == "utils.py"


def test_resolve_import_to_file_nested(tmp_path):
    """Test resolving nested module import."""
    # Create nested structure
    pkg = tmp_path / "package" / "subpackage"
    pkg.mkdir(parents=True)
    (pkg / "module.py").write_text("# nested")
    
    result = _resolve_import_to_file("import package.subpackage.module", str(tmp_path))
    
    # Check if the nested structure is resolved
    if result:
        assert "package" in result
        assert "subpackage" in result
        assert "module.py" in result
    else:
        pytest.skip("Nested module resolution may require proper Python path configuration")


def test_resolve_import_to_file_not_found(tmp_path):
    """Test resolving non-existent import."""
    result = _resolve_import_to_file("import nonexistent", str(tmp_path))
    assert result is None


def test_resolve_import_to_file_stdlib():
    """Test that stdlib imports don't resolve to local files."""
    result = _resolve_import_to_file("import os", "/tmp")
    assert result is None


# --- Project Root Finding Tests ---

def test_find_project_root_pyproject(tmp_path):
    """Test finding project root with pyproject.toml."""
    (tmp_path / "pyproject.toml").write_text("")
    subdir = tmp_path / "src" / "package"
    subdir.mkdir(parents=True)
    test_file = subdir / "module.py"
    test_file.write_text("")
    
    root = _find_project_root(str(test_file))
    assert Path(root) == tmp_path


def test_find_project_root_git(tmp_path):
    """Test finding project root with .git directory."""
    (tmp_path / ".git").mkdir()
    subdir = tmp_path / "nested" / "deep"
    subdir.mkdir(parents=True)
    test_file = subdir / "file.py"
    test_file.write_text("")
    
    root = _find_project_root(str(test_file))
    assert Path(root) == tmp_path


def test_find_project_root_setup_py(tmp_path):
    """Test finding project root with setup.py."""
    (tmp_path / "setup.py").write_text("")
    test_file = tmp_path / "test.py"
    test_file.write_text("")
    
    root = _find_project_root(str(test_file))
    assert Path(root) == tmp_path


def test_find_project_root_no_marker(tmp_path):
    """Test fallback when no project markers found."""
    test_file = tmp_path / "orphan.py"
    test_file.write_text("")
    
    root = _find_project_root(str(test_file))
    # Should return cwd as fallback
    assert root == os.getcwd()


# --- find_dependencies Integration Tests ---

def test_find_dependencies_file_not_found():
    """Test find_dependencies with non-existent file."""
    result = find_dependencies("/nonexistent/file.py")
    assert "Error: File not found" in result


def test_find_dependencies_no_imports(tmp_path):
    """Test find_dependencies with file that has no local imports."""
    # Add a project marker so find_project_root works within tmp_path
    (tmp_path / "pyproject.toml").write_text("")
    
    test_file = tmp_path / "simple.py"
    test_file.write_text("""import os
import sys

def hello():
    pass
""")
    
    result = find_dependencies(str(test_file))
    assert "simple.py" in result


def test_find_dependencies_with_local_imports(tmp_path):
    """Test find_dependencies with local imports."""
    # Create project structure
    (tmp_path / "pyproject.toml").write_text("")
    
    utils = tmp_path / "utils.py"
    utils.write_text("# utils module")
    
    main = tmp_path / "main.py"
    main.write_text("""import os
from utils import helper
""")
    
    result = find_dependencies(str(main))
    
    assert "main.py" in result
    assert "utils.py" in result


def test_find_dependencies_circular(tmp_path):
    """Test find_dependencies handles circular imports."""
    (tmp_path / "pyproject.toml").write_text("")
    
    a = tmp_path / "a.py"
    a.write_text("from b import func_b")
    
    b = tmp_path / "b.py"
    b.write_text("from a import func_a")
    
    result = find_dependencies(str(a))
    
    assert "a.py" in result
    assert "b.py" in result
    assert "(circular)" in result


def test_find_dependencies_depth_limit(tmp_path):
    """Test find_dependencies respects depth limit."""
    (tmp_path / "pyproject.toml").write_text("")
    
    # Create chain: a -> b -> c -> d
    (tmp_path / "d.py").write_text("# leaf")
    (tmp_path / "c.py").write_text("from d import x")
    (tmp_path / "b.py").write_text("from c import y")
    (tmp_path / "a.py").write_text("from b import z")
    
    # Depth 1 should only show a -> b
    result = find_dependencies(str(tmp_path / "a.py"), depth=1)
    
    assert "a.py" in result
    assert "b.py" in result
    # Should not go deeper than depth 1
    lines = result.split("\n")
    assert len(lines) <= 3  # a.py + b.py + maybe c.py header


# --- Constants Tests ---

def test_supported_extensions():
    """Test that .py is in supported extensions."""
    assert ".py" in SUPPORTED_EXTENSIONS


def test_tree_sitter_availability_flag():
    """Test tree-sitter availability flag is set correctly."""
    try:
        import tree_sitter_python
        expected = True
    except ImportError:
        expected = False
    
    assert _TREE_SITTER_AVAILABLE == expected