"""AST-based code analysis tools using tree-sitter."""

from __future__ import annotations

import os
import re
from typing import Any

# Tree-sitter availability flag
_TREE_SITTER_AVAILABLE = False
_parser: Any = None

try:
    import tree_sitter_python as tspython
    from tree_sitter import Language, Parser

    _TREE_SITTER_AVAILABLE = True

    PY_LANGUAGE = Language(tspython.language())
    _parser = Parser(PY_LANGUAGE)
except ImportError:
    pass

# Language registry for future extension
SUPPORTED_EXTENSIONS = {".py"}


def _parse_python_tree(source: bytes) -> Any | None:
    """Parse Python source code with tree-sitter."""
    if not _TREE_SITTER_AVAILABLE or _parser is None:
        return None
    return _parser.parse(source)


def _extract_structure_treesitter(tree: Any, source: bytes, file_path: str) -> dict:
    """Extract code structure from a tree-sitter AST."""
    root = tree.root_node
    source_text = source.decode("utf-8", errors="ignore")
    lines = source_text.split("\n")

    structure: dict[str, list] = {
        "imports": [],
        "classes": [],
        "functions": [],
        "globals": [],
    }

    for child in root.children:
        node_type = child.type
        start_line = child.start_point[0] + 1  # 1-indexed

        if node_type == "import_statement":
            text = source[child.start_byte:child.end_byte].decode("utf-8", errors="ignore").strip()
            structure["imports"].append({"text": text, "line": start_line})

        elif node_type == "import_from_statement":
            text = source[child.start_byte:child.end_byte].decode("utf-8", errors="ignore").strip()
            structure["imports"].append({"text": text, "line": start_line})

        elif node_type == "class_definition":
            class_info = _extract_class(child, source)
            class_info["line"] = start_line
            structure["classes"].append(class_info)

        elif node_type == "function_definition":
            func_info = _extract_function(child, source)
            func_info["line"] = start_line
            structure["functions"].append(func_info)

        elif node_type == "decorated_definition":
            # Handle decorated classes/functions
            for sub in child.children:
                if sub.type == "class_definition":
                    class_info = _extract_class(sub, source)
                    class_info["line"] = sub.start_point[0] + 1
                    decorators = _extract_decorators(child, source)
                    class_info["decorators"] = decorators
                    structure["classes"].append(class_info)
                elif sub.type == "function_definition":
                    func_info = _extract_function(sub, source)
                    func_info["line"] = sub.start_point[0] + 1
                    decorators = _extract_decorators(child, source)
                    func_info["decorators"] = decorators
                    structure["functions"].append(func_info)

        elif node_type == "expression_statement":
            # Top-level assignments (globals)
            for sub in child.children:
                if sub.type == "assignment":
                    text = source[sub.start_byte:sub.end_byte].decode("utf-8", errors="ignore").strip()
                    name = text.split("=")[0].strip().split(":")[0].strip()
                    if name and not name.startswith("_"):
                        structure["globals"].append({"name": name, "line": start_line})

    return structure


def _extract_class(node: Any, source: bytes) -> dict:
    """Extract class info from a class_definition node."""
    name = ""
    bases = []
    methods = []

    for child in node.children:
        if child.type == "identifier":
            name = source[child.start_byte:child.end_byte].decode("utf-8", errors="ignore")
        elif child.type == "argument_list":
            bases_text = source[child.start_byte:child.end_byte].decode("utf-8", errors="ignore")
            bases = [b.strip() for b in bases_text.strip("()").split(",") if b.strip()]
        elif child.type == "block":
            for block_child in child.children:
                if block_child.type == "function_definition":
                    method_info = _extract_function(block_child, source)
                    method_info["line"] = block_child.start_point[0] + 1
                    methods.append(method_info)
                elif block_child.type == "decorated_definition":
                    for sub in block_child.children:
                        if sub.type == "function_definition":
                            method_info = _extract_function(sub, source)
                            method_info["line"] = sub.start_point[0] + 1
                            method_info["decorators"] = _extract_decorators(block_child, source)
                            methods.append(method_info)

    return {"name": name, "bases": bases, "methods": methods}


def _extract_function(node: Any, source: bytes) -> dict:
    """Extract function info from a function_definition node."""
    name = ""
    params = []

    for child in node.children:
        if child.type == "identifier":
            name = source[child.start_byte:child.end_byte].decode("utf-8", errors="ignore")
        elif child.type == "parameters":
            params_text = source[child.start_byte:child.end_byte].decode("utf-8", errors="ignore")
            raw_params = params_text.strip("()").split(",")
            params = [p.strip().split(":")[0].strip().split("=")[0].strip()
                      for p in raw_params if p.strip()]

    # Extract docstring if present (first expression_statement with a string child)
    docstring = ""
    body = None
    for child in node.children:
        if child.type == "block":
            body = child
            break
    if body and body.children:
        first_stmt = body.children[0]
        if first_stmt.type == "expression_statement":
            for sc in first_stmt.children:
                if sc.type == "string":
                    docstring = source[sc.start_byte:sc.end_byte].decode("utf-8", errors="ignore")
                    # Strip triple quotes
                    for q in ('"""', "'''"):
                        if docstring.startswith(q) and docstring.endswith(q):
                            docstring = docstring[3:-3].strip()
                            break
                    break

    return {"name": name, "params": params, "docstring": docstring}


def _extract_decorators(node: Any, source: bytes) -> list[str]:
    """Extract decorator names from a decorated_definition node."""
    decorators = []
    for child in node.children:
        if child.type == "decorator":
            text = source[child.start_byte:child.end_byte].decode("utf-8", errors="ignore").strip()
            decorators.append(text)
    return decorators


def _extract_structure_regex(content: str) -> dict:
    """Fallback: extract code structure using regex (when tree-sitter is unavailable)."""
    structure: dict[str, list] = {
        "imports": [],
        "classes": [],
        "functions": [],
        "globals": [],
    }

    for i, line in enumerate(content.split("\n"), 1):
        stripped = line.strip()

        if stripped.startswith("import ") or stripped.startswith("from "):
            structure["imports"].append({"text": stripped, "line": i})

        elif stripped.startswith("class "):
            match = re.match(r"class\s+(\w+)(?:\((.*?)\))?:", stripped)
            if match:
                name = match.group(1)
                bases = [b.strip() for b in (match.group(2) or "").split(",") if b.strip()]
                structure["classes"].append({"name": name, "bases": bases, "methods": [], "line": i})

        elif stripped.startswith("def "):
            match = re.match(r"def\s+(\w+)\((.*?)\)", stripped)
            if match:
                name = match.group(1)
                params = [p.strip().split(":")[0].split("=")[0].strip()
                          for p in match.group(2).split(",") if p.strip()]
                # Check if it's a method (indented) or top-level function
                if line.startswith("    ") or line.startswith("\t"):
                    # Method - add to last class
                    if structure["classes"]:
                        structure["classes"][-1]["methods"].append({
                            "name": name, "params": params, "line": i
                        })
                else:
                    structure["functions"].append({"name": name, "params": params, "line": i})

    return structure


def _format_structure(structure: dict, file_path: str, total_lines: int) -> str:
    """Format extracted structure as readable text."""
    _, ext = os.path.splitext(file_path)
    lang = {"py": "Python", "js": "JavaScript", "ts": "TypeScript"}.get(ext.lstrip("."), ext.lstrip(".").upper() or "Unknown")

    parts = [f"## {file_path} ({lang}, {total_lines} lines)\n"]

    if structure["imports"]:
        parts.append("### Imports")
        for imp in structure["imports"]:
            parts.append(f"  - {imp['text']} (line {imp['line']})")
        parts.append("")

    if structure["classes"]:
        parts.append("### Classes")
        for cls in structure["classes"]:
            bases_str = f"({', '.join(cls['bases'])})" if cls.get("bases") else ""
            decorators = cls.get("decorators", [])
            dec_str = " ".join(decorators) + " " if decorators else ""
            parts.append(f"  - {dec_str}{cls['name']}{bases_str} (line {cls['line']})")
            for method in cls.get("methods", []):
                params_str = ", ".join(method["params"])
                dec_str = " ".join(method.get("decorators", []))
                prefix = f"    {dec_str} " if dec_str else "    "
                parts.append(f"{prefix}- {method['name']}({params_str}) - line {method['line']}")
        parts.append("")

    if structure["functions"]:
        parts.append("### Functions")
        for func in structure["functions"]:
            params_str = ", ".join(func["params"])
            decorators = func.get("decorators", [])
            dec_str = " ".join(decorators) + " " if decorators else ""
            parts.append(f"  - {dec_str}{func['name']}({params_str}) - line {func['line']}")
        parts.append("")

    if structure["globals"]:
        parts.append("### Globals")
        for g in structure["globals"]:
            parts.append(f"  - {g['name']} (line {g['line']})")
        parts.append("")

    return "\n".join(parts)


def code_structure(file_path: str) -> str:
    """Analyze a file and return its structural overview: imports, classes, functions, and globals.

    Useful for quickly understanding what a file contains without reading its full content.
    Works best with Python files (using tree-sitter AST parsing), but falls back to
    regex-based extraction for other languages.

    Args:
        file_path: Path to the file to analyze.
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except FileNotFoundError:
        return f"Error: File not found: {file_path}"
    except Exception as e:
        return f"Error reading file: {e}"

    total_lines = content.count("\n") + 1
    _, ext = os.path.splitext(file_path)

    # Try tree-sitter for supported languages
    if ext in SUPPORTED_EXTENSIONS and _TREE_SITTER_AVAILABLE:
        source = content.encode("utf-8")
        tree = _parse_python_tree(source)
        if tree:
            structure = _extract_structure_treesitter(tree, source, file_path)
            return _format_structure(structure, file_path, total_lines)

    # Fallback to regex
    structure = _extract_structure_regex(content)
    return _format_structure(structure, file_path, total_lines)


def _resolve_import_to_file(import_text: str, project_root: str) -> str | None:
    """Try to resolve an import statement to a file path within the project."""
    # Handle "from X import Y" and "import X"
    match = re.match(r"(?:from\s+)?([\w.]+)", import_text)
    if not match:
        return None

    module_path = match.group(1)
    parts = module_path.split(".")

    # Try as package (directory/__init__.py) and module (.py file)
    candidates = [
        os.path.join(*parts, "__init__.py"),
        os.path.join(*parts) + ".py",
    ]

    # Also try relative to common src directories
    for candidate in candidates:
        full_path = os.path.join(project_root, candidate)
        if os.path.isfile(full_path):
            return candidate

    return None


def _find_project_root(file_path: str) -> str:
    """Find the project root by looking for pyproject.toml, setup.py, or .git."""
    current = os.path.abspath(os.path.dirname(file_path))
    markers = ("pyproject.toml", "setup.py", "setup.cfg", "package.json", ".git")

    while True:
        for marker in markers:
            if os.path.exists(os.path.join(current, marker)):
                return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.getcwd()
        current = parent


def find_dependencies(file_path: str, depth: int = 3) -> str:
    """Trace the import dependency tree of a file within the project.

    Resolves local imports (within the project) and shows which files depend on which.
    Skips stdlib and third-party packages. Useful for understanding how files are connected.

    Args:
        file_path: Path to the file to analyze.
        depth: Maximum recursion depth for tracing imports. Defaults to 3.
    """
    if not os.path.isfile(file_path):
        return f"Error: File not found: {file_path}"

    project_root = _find_project_root(file_path)
    rel_start = os.path.relpath(file_path, project_root).replace("\\", "/")

    visited: set[str] = set()
    tree_lines: list[str] = []

    def _trace(rel_path: str, current_depth: int, prefix: str = "", is_last: bool = True):
        if rel_path in visited or current_depth > depth:
            if rel_path in visited:
                connector = "└── " if is_last else "├── "
                tree_lines.append(f"{prefix}{connector}{rel_path} (circular)")
            return

        visited.add(rel_path)
        connector = "└── " if is_last else "├── "

        if current_depth == 0:
            tree_lines.append(rel_path)
        else:
            tree_lines.append(f"{prefix}{connector}{rel_path}")

        # Read file and extract imports
        full_path = os.path.join(project_root, rel_path)
        if not os.path.isfile(full_path):
            return

        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except OSError:
            return

        # Extract imports (tree-sitter or regex)
        _, ext = os.path.splitext(rel_path)
        imports = []

        if ext == ".py" and _TREE_SITTER_AVAILABLE:
            source = content.encode("utf-8")
            tree = _parse_python_tree(source)
            if tree:
                for child in tree.root_node.children:
                    if child.type in ("import_statement", "import_from_statement"):
                        text = source[child.start_byte:child.end_byte].decode("utf-8", errors="ignore").strip()
                        imports.append(text)
        else:
            for line in content.split("\n"):
                stripped = line.strip()
                if stripped.startswith("import ") or stripped.startswith("from "):
                    imports.append(stripped)

        # Resolve imports to local files
        local_deps = []
        for imp in imports:
            resolved = _resolve_import_to_file(imp, project_root)
            if resolved and resolved != rel_path:
                local_deps.append(resolved)

        # Remove duplicates while preserving order
        seen = set()
        unique_deps = []
        for dep in local_deps:
            normalized = dep.replace("\\", "/")
            if normalized not in seen:
                seen.add(normalized)
                unique_deps.append(normalized)

        # Recurse into dependencies
        child_prefix = prefix + ("    " if is_last else "│   ")
        for i, dep in enumerate(unique_deps):
            is_dep_last = (i == len(unique_deps) - 1)
            _trace(dep, current_depth + 1, child_prefix if current_depth > 0 else "", is_dep_last)

    _trace(rel_start, 0)

    if not tree_lines:
        return f"No dependencies found for: {file_path}"

    return "\n".join(tree_lines)
