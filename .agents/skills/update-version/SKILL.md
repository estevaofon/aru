---
name: update-version
description: Bump the aru package version (major, minor, or patch). Default is minor.
argument-hint: "[major|minor|patch]"
user-invocable: true
allowed-tools: Read, Write, Edit
---

Bump the aru package version. Accepts an optional argument: `major`, `minor`, or `patch`. **Default is `minor`** if no argument is provided.

## Steps

1. **Parse argument**: Read the argument (if any). Accept `major`, `minor`, or `patch` (case-insensitive). If no argument is given, default to `minor`. If the argument is not one of these three, report an error and stop.

2. **Read current version** from both files:
   - `pyproject.toml` — look for `version = "X.Y.Z"` under `[project]`
   - `aru/__init__.py` — look for `__version__ = "X.Y.Z"`
   - Both must match. If they differ, report the conflict and stop.

3. **Calculate new version** based on the bump type:
   - `major`: increment MAJOR by 1, reset MINOR and PATCH to 0. Example: `0.5.1` → `1.0.0`
   - `minor`: increment MINOR by 1, reset PATCH to 0. Example: `0.5.1` → `0.6.0`
   - `patch`: increment PATCH by 1. Example: `0.5.1` → `0.5.2`

4. **Update `pyproject.toml`**: Replace the `version` value under `[project]` with the new version.

5. **Update `aru/__init__.py`**: Replace the `__version__` assignment with the new version.

6. **Confirm**: Report the bump type, old version, and new version to the user (e.g. `minor: v0.5.0 → v0.6.0`).

## Rules

- Both files must always have the same version number
- Use semantic versioning format: `MAJOR.MINOR.PATCH`
- Do not modify any other lines in either file
- Never ask the user for the version — derive it automatically from the files