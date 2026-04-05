---
name: open-pr
description: Create a GitHub pull request with structured summary, components, and test plan
argument-hint: [base-branch]
user-invocable: true
allowed-tools: Bash, Read, Grep, Glob
---

Create a GitHub pull request for the current branch using `gh pr create`.

## Steps

1. **Determine base branch**: Use `$ARGUMENTS` for the base branch.

2. **Gather context** — run these in parallel:
   - `git status` to check for uncommitted changes
   - `git log <base>..HEAD --oneline` to see all commits being merged
   - `git diff <base>...HEAD --stat` to see changed files

3. **Analyze changes**: Read the changed files and commit messages to understand what was done.

4. **Build the PR description** by generating:
   - Concise title under 70 chars
   - Summary bullets (focus on **why**, not just what)
   - Components list with affected files/modules
   - Test Plan with checkboxes

5. **Create the PR** using one of these approaches:
   
   Option A - Pass body via stdin (recommended):
   ```
   gh pr create --base <base-branch> --title "<title>" --body-file /dev/stdin <<'EOF'
   ## Summary
   - ...
   
   ## Components
   - ...
   
   ## Test Plan
   - [ ] ...
   EOF
   ```
   
   Option B - Use a temp file:
   ```
   echo "## Summary..." > /tmp/pr_body.md
   gh pr create --base <base-branch> --title "<title>" --body-file /tmp/pr_body.md
   ```
```

## Rules

- Title must be under 70 characters
- Summary bullets should focus on **why**, not just what
- Components should list affected files/modules with brief technical context
- Test Plan checkboxes go in the **body** (not comments) so GitHub tracks progress
- If there are uncommitted changes, warn the user before proceeding
- Push the branch with `git push -u origin HEAD` if not already pushed
- Return the PR URL when done
