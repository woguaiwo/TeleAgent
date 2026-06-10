# Repository Setup

Use this checklist when publishing TeleAgent to GitHub.

## 1. Review Local Files

Before the first commit, confirm that generated or private files are ignored:

```bash
git status --ignored
```

Do not publish files matching these patterns:

- `.teleagent/`
- `teleagent-history.log`
- `teleagent-raw.log`
- `teleagent-debug-*`
- `teleagent-debug-runs/`
- `error.txt`
- `*.egg-info/`
- `__pycache__/`

## 2. Initialize Git

If this checkout is not already a valid Git repository:

```bash
git init
git add .
git status
git commit -m "Initial open-source release"
```

If `git status` says `fatal: not a git repository` but a `.git/` directory is
already present, inspect that directory first. An empty or incomplete `.git/`
directory must be moved away or removed before `git init` can create a real
repository.

## 3. Create GitHub Repository

Create an empty repository on GitHub, then connect it:

```bash
git branch -M main
git remote add origin git@github.com:woguaiwo/TeleAgent.git
git push -u origin main
```

If you publish under a different account or organization, update:

- `pyproject.toml` project URLs
- README clone URL
- GitHub remote URL

## 4. Verify CI

After pushing, confirm the GitHub Actions workflow passes on Python 3.11, 3.12,
and 3.13.
