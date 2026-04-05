## Running Tests

**Do NOT use `source .venv/bin/activate` in bash tool** — it hangs in subprocesses.

```bash
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest --cov=aru --cov-report=term-missing
```

**Always use `--cov-report=term-missing`**, not `--cov-report=html` (HTML causes OOM in WSL2).