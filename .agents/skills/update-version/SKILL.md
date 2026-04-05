---
name: update-version
description: Read the current aru package version and bump the minor version (e.g. v0.5.0 → v0.6.0)
argument-hint: (no arguments needed)
user-invocable: true
allowed-tools: Read, Write, Edit
---

Bump the aru package minor version automatically — no arguments required.

## Steps

1. **Read current version** from both files:
   - `pyproject.toml` — look for `version = "X.Y.Z"` under `[project]`
   - `aru/__init__.py` — look for `__version__ = "X.Y.Z"`
   - Both must match. If they differ, report the conflict and stop.

2. **Calculate new version**: increment the MINOR component by 1 and reset PATCH to 0.
   - Example: `0.5.0` → `0.6.0`, `1.3.2` → `1.4.0`

3. **Update `pyproject.toml`**: Replace the `version` value under `[project]` with the new version.

4. **Update `aru/__init__.py`**: Replace the `__version__` assignment with the new version.

5. **Confirm**: Report the old version and new version to the user (e.g. `v0.5.0 → v0.6.0`).

## Rules

- Both files must always have the same version number
- Use semantic versioning format: `MAJOR.MINOR.PATCH`
- Do not modify any other lines in either file
- Never ask the user for the version — derive it automatically from the files