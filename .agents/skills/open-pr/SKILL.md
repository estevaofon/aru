---
name: open-pr
description: Create a GitHub pull request with structured summary, components, and test plan
argument-hint: [base-branch]
user-invocable: true
allowed-tools: Bash, Read, Grep, Glob
---

Create a GitHub pull request for the current branch using `gh pr create`.

## Steps

1. **Determine base branch**: Use `$ARGUMENTS` if provided, otherwise default to `main`.

2. **Gather context** — run these in parallel:
   - `git status` to check for uncommitted changes
   - `git log <base>..HEAD --oneline` to see all commits being merged
   - `git diff <base>...HEAD --stat` to see changed files

3. **Analyze changes**: Read the changed files and commit messages to understand what was done.

4. **Build the PR** using `gh pr create` with this exact template:

```
gh pr create --base <base-branch> --title "<concise title under 70 chars>" --body "$(cat <<'EOF'
## Summary
- <bullet points describing what was done, focus on the why>

## Components
- <list affected components with paths and technical details>

## Test Plan
- [ ] Testes unitários passando
- [ ] Testado localmente
EOF
)"
```

## Rules

- Title must be under 70 characters
- Summary bullets should focus on **why**, not just what
- Components should list affected files/modules with brief technical context
- Test Plan checkboxes go in the **body** (not comments) so GitHub tracks progress
- If there are uncommitted changes, warn the user before proceeding
- Push the branch with `git push -u origin HEAD` if not already pushed
- Return the PR URL when done
