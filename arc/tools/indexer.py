"""Semantic search indexer using chromadb for codebase exploration."""

import hashlib
import json
import os
from typing import Any

from arc.tools.gitignore import walk_filtered

ARC_DIR = ".arc"
CHROMA_DIR = os.path.join(ARC_DIR, "chroma")
META_FILE = os.path.join(ARC_DIR, "index_meta.json")
CHUNK_SIZE = 1500
CHUNK_OVERLAP = 200
MAX_FILE_SIZE = 500_000

# Text file extensions to index
_TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".rb",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".swift", ".kt", ".scala",
    ".php", ".lua", ".sh", ".bash", ".zsh", ".fish",
    ".html", ".css", ".scss", ".less", ".vue", ".svelte",
    ".json", ".yaml", ".yml", ".toml", ".xml", ".ini", ".cfg",
    ".md", ".rst", ".txt",
    ".sql", ".graphql", ".proto",
    ".dockerfile", ".env.example", ".gitignore",
    ".r", ".m", ".jl",
}

_FILENAMES_TO_INDEX = {
    "Dockerfile", "Makefile", "Rakefile", "Gemfile",
    "Pipfile", "Procfile", "Vagrantfile",
}

# Lazy-initialized state
_client: Any = None
_collection: Any = None
_index_meta: dict[str, float] = {}


def _get_arc_dir() -> str:
    """Get or create the .arc directory relative to cwd."""
    arc_dir = os.path.join(os.getcwd(), ARC_DIR)
    os.makedirs(arc_dir, exist_ok=True)
    return arc_dir


def _is_text_file(filepath: str) -> bool:
    """Check if a file should be indexed based on extension or name."""
    _, ext = os.path.splitext(filepath)
    basename = os.path.basename(filepath)
    if basename in _FILENAMES_TO_INDEX:
        return True
    if ext.lower() in _TEXT_EXTENSIONS:
        return True
    # Fallback: check for null bytes
    if not ext:
        try:
            with open(filepath, "rb") as f:
                sample = f.read(512)
            return b"\x00" not in sample
        except OSError:
            return False
    return False


def _init_client():
    """Lazy-initialize the chromadb client and collection."""
    global _client, _collection

    if _collection is not None:
        return

    import chromadb

    chroma_path = os.path.join(os.getcwd(), CHROMA_DIR)
    os.makedirs(chroma_path, exist_ok=True)
    _client = chromadb.PersistentClient(path=chroma_path)
    _collection = _client.get_or_create_collection(
        name="codebase",
        metadata={"hnsw:space": "cosine"},
    )


def _load_meta() -> dict[str, float]:
    """Load index metadata (file mtimes) from disk."""
    global _index_meta
    meta_path = os.path.join(os.getcwd(), META_FILE)
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                _index_meta = json.load(f)
        except (json.JSONDecodeError, OSError):
            _index_meta = {}
    else:
        _index_meta = {}
    return _index_meta


def _save_meta():
    """Save index metadata to disk."""
    meta_path = os.path.join(os.getcwd(), META_FILE)
    os.makedirs(os.path.dirname(meta_path), exist_ok=True)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(_index_meta, f, indent=2)


def _chunk_file(content: str, file_path: str) -> list[dict]:
    """Split file content into overlapping chunks with metadata."""
    lines = content.split("\n")
    chunks = []
    current_chars = 0
    chunk_start_line = 0
    chunk_lines: list[str] = []

    for i, line in enumerate(lines):
        chunk_lines.append(line)
        current_chars += len(line) + 1  # +1 for newline

        if current_chars >= CHUNK_SIZE or i == len(lines) - 1:
            chunk_text = "\n".join(chunk_lines)
            chunk_id = hashlib.md5(f"{file_path}:{chunk_start_line}".encode()).hexdigest()

            _, ext = os.path.splitext(file_path)
            chunks.append({
                "id": chunk_id,
                "document": chunk_text,
                "metadata": {
                    "file_path": file_path.replace("\\", "/"),
                    "start_line": chunk_start_line + 1,
                    "end_line": i + 1,
                    "language": ext.lstrip(".") if ext else "unknown",
                },
            })

            # Overlap: keep last few lines for context continuity
            overlap_chars = 0
            overlap_start = len(chunk_lines)
            for j in range(len(chunk_lines) - 1, -1, -1):
                overlap_chars += len(chunk_lines[j]) + 1
                if overlap_chars >= CHUNK_OVERLAP:
                    overlap_start = j
                    break

            chunk_lines = chunk_lines[overlap_start:]
            chunk_start_line = i - len(chunk_lines) + 1
            current_chars = sum(len(l) + 1 for l in chunk_lines)

    return chunks


def _get_indexable_files(root_dir: str) -> list[str]:
    """Get all indexable text files using gitignore-aware walk."""
    files = []
    for dirpath, _, filenames in walk_filtered(root_dir):
        for filename in filenames:
            filepath = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(filepath, root_dir)
            try:
                size = os.path.getsize(filepath)
            except OSError:
                continue
            if size > MAX_FILE_SIZE or size == 0:
                continue
            if _is_text_file(filepath):
                files.append(rel_path)
    return files


def _update_index(root_dir: str):
    """Incrementally update the codebase index. Only re-indexes changed/new files."""
    global _index_meta

    _init_client()
    _load_meta()

    current_files = _get_indexable_files(root_dir)
    current_files_set = set(current_files)

    # Find files to add/update
    to_index = []
    for rel_path in current_files:
        filepath = os.path.join(root_dir, rel_path)
        try:
            mtime = os.path.getmtime(filepath)
        except OSError:
            continue
        if rel_path not in _index_meta or _index_meta[rel_path] != mtime:
            to_index.append((rel_path, mtime))

    # Find files to remove (deleted from disk)
    to_remove = [p for p in _index_meta if p not in current_files_set]

    # Remove deleted files from collection
    if to_remove:
        for path in to_remove:
            # Remove all chunks for this file
            try:
                _collection.delete(where={"file_path": path.replace("\\", "/")})
            except Exception:
                pass
            del _index_meta[path]

    # Index new/changed files in batches
    if to_index:
        batch_ids = []
        batch_docs = []
        batch_metas = []

        for rel_path, mtime in to_index:
            filepath = os.path.join(root_dir, rel_path)
            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except OSError:
                continue

            # Remove old chunks for this file before re-indexing
            try:
                _collection.delete(where={"file_path": rel_path.replace("\\", "/")})
            except Exception:
                pass

            chunks = _chunk_file(content, rel_path)
            for chunk in chunks:
                batch_ids.append(chunk["id"])
                batch_docs.append(chunk["document"])
                batch_metas.append(chunk["metadata"])

            _index_meta[rel_path] = mtime

            # Upsert in batches of 100
            if len(batch_ids) >= 100:
                _collection.upsert(ids=batch_ids, documents=batch_docs, metadatas=batch_metas)
                batch_ids, batch_docs, batch_metas = [], [], []

        # Flush remaining
        if batch_ids:
            _collection.upsert(ids=batch_ids, documents=batch_docs, metadatas=batch_metas)

    _save_meta()


def semantic_search(query: str, top_k: int = 10, file_glob: str = "") -> str:
    """Search the codebase using natural language. Finds code semantically related to your query,
    even if the exact words don't appear in the code.

    Use this when grep_search won't work because you're looking for concepts, not exact text.
    Examples: "authentication logic", "database connection setup", "error handling patterns"

    The codebase is indexed automatically on first use and updated incrementally.

    Args:
        query: Natural language description of what you're looking for.
        top_k: Maximum number of results to return. Defaults to 10.
        file_glob: Optional glob to filter files (e.g. '*.py'). Empty means all files.
    """
    try:
        import chromadb  # noqa: F401
    except ImportError:
        return (
            "Error: Semantic search unavailable. Install chromadb: pip install chromadb>=0.5\n"
            "Use grep_search as an alternative for text-based search."
        )

    try:
        root_dir = os.getcwd()
        _update_index(root_dir)

        # Build query parameters
        query_params: dict[str, Any] = {
            "query_texts": [query],
            "n_results": min(top_k, _collection.count()) if _collection.count() > 0 else 1,
        }

        # Apply file glob filter via metadata
        if file_glob:
            import fnmatch as _fnmatch
            # chromadb where filters don't support glob, so we filter post-query
            query_params["n_results"] = min(top_k * 3, _collection.count()) if _collection.count() > 0 else 1

        if _collection.count() == 0:
            return "No files indexed yet. The codebase may be empty or all files are ignored."

        results = _collection.query(**query_params)

        if not results or not results["ids"] or not results["ids"][0]:
            return f"No semantic matches found for: {query}"

        # Format results
        output_lines = []
        seen_files: dict[str, float] = {}  # Track best score per file

        for i, (doc_id, doc, metadata, distance) in enumerate(zip(
            results["ids"][0],
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        )):
            file_path = metadata["file_path"]

            # Apply file glob filter if specified
            if file_glob and not _fnmatch.fnmatch(file_path, file_glob):
                continue

            score = max(0, 1 - distance)  # Convert distance to similarity score
            start_line = metadata.get("start_line", "?")
            end_line = metadata.get("end_line", "?")

            # Show best chunk per file, with snippet
            if file_path not in seen_files or score > seen_files[file_path]:
                seen_files[file_path] = score

            # Truncate snippet preview
            preview = doc[:200].replace("\n", " ").strip()
            if len(doc) > 200:
                preview += "..."

            output_lines.append(
                f"{file_path}:{start_line}-{end_line} (score: {score:.2f})\n  {preview}"
            )

            if len(output_lines) >= top_k:
                break

        if not output_lines:
            return f"No matches found for: {query}" + (f" (filtered by {file_glob})" if file_glob else "")

        return f"Found {len(output_lines)} semantic matches for '{query}':\n\n" + "\n\n".join(output_lines)

    except Exception as e:
        return f"Error during semantic search: {e}"
