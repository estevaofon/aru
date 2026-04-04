---
name: update-version
description: Update the aru package version in pyproject.toml and aru/__init__.py
argument-hint: <version>
user-invocable: true
allowed-tools: Read, Write, Edit
---

Update the aru package version to the version specified in `$ARGUMENTS`.

## Steps

1. **Validate argument**: Ensure `$ARGUMENTS` is a valid semantic version (e.g., `0.5.0`). If missing or invalid, ask the user.

2. **Read current versions** from both files to confirm current state:
   - `pyproject.toml` — look for `version = "X.Y.Z"` under `[project]`
   - `aru/__init__.py` — look for `__version__ = "X.Y.Z"`

3. **Update `pyproject.toml`**: Replace the `version` value under `[project]` with the new version.

4. **Update `aru/__init__.py`**: Replace the `__version__` assignment with the new version.

5. **Confirm**: Report the old version and new version to the user.

## Rules

- Both files must always have the same version number
- Use semantic versioning format: `MAJOR.MINOR.PATCH`
- Do not modify any other lines in either file