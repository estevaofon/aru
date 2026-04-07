---
name: open-pr
description: Create a GitHub pull request with structured summary, components, and test plan
argument-hint: [base-branch]
user-invocable: true
allowed-tools: Bash, Read, Grep, Glob, Write
---

Create a GitHub pull request for the current branch using `gh pr create`.

## Steps

1. **Determine base branch**: Use `$ARGUMENTS` as the base branch. If empty, default to `develop`.

2. **Check prerequisites**:
   - Run `git status` — if there are uncommitted changes, warn the user and ask whether to continue.
   - Run `git log <base>..HEAD --oneline` to see all commits being merged.
   - Run `git diff <base>...HEAD --stat` to see changed files summary.
   - Run `git diff <base>...HEAD` to read the actual code changes (truncate if too large).

3. **Analyze changes deeply**: Read the diff output and commit messages carefully. Understand:
   - **What** changed (files, functions, classes)
   - **Why** it changed (purpose, motivation, problem being solved)
   - **How** it changed (approach, technique, patterns used)

4. **Write the PR body to a temp file**: Create a file at `.aru/tmp_pr_body.md` with this exact structure:

   ```markdown
   ## Summary
   - First bullet explaining the main change and WHY it was needed
   - Additional bullets for other notable changes

   ## Components
   - `path/to/file.py` — Brief description of what changed in this file
   - `path/to/other.py` — Brief description

   ## Test Plan
   - [ ] Describe how to verify the change works
   - [ ] Additional verification steps
   ```

   **Rules for the PR body content:**
   - Write in plain English (not Portuguese, unless the commits are in Portuguese)
   - Summary bullets must explain **why**, not just restate the commit message
   - Components must reference actual changed files with backtick formatting
   - Test Plan must have actionable verification steps as checkboxes
   - Do NOT wrap lines in quotes or escape characters — write raw markdown

5. **Generate a concise PR title**: Under 72 characters, in imperative mood (e.g., "Add retry logic to API client", "Fix token count in context pruning"). Do NOT just repeat a commit message — synthesize the overall change.

6. **Push the branch** if not already pushed:
   ```
   git push -u origin HEAD
   ```

7. **Create the PR** using `--body-file` pointing to the temp file:
   ```
   gh pr create --base <base-branch> --title "<title>" --body-file .aru/tmp_pr_body.md
   ```

   **CRITICAL**: Always use `--body-file` with the temp file. NEVER use `--body` with inline text — it causes quoting/escaping issues that corrupt the PR description.

8. **Clean up**: Delete `.aru/tmp_pr_body.md` after the PR is created.

9. **Return the PR URL** shown in the `gh` output.

## Rules

- ALWAYS use `--body-file` with a temp file. NEVER use `--body` with inline text or heredocs.
- NEVER wrap markdown content in quotes when writing to the temp file — write raw markdown directly.
- NEVER use `echo` with quotes to build the body — use `write_file` tool to create the temp file instead.
- Title must be under 72 characters, imperative mood, and synthesize the overall change.
- Summary must focus on **why** and **impact**, not just restate file names or commit subjects.
- If the diff is large, focus the summary on the most important changes.
- If there are uncommitted changes, warn the user before proceeding.
