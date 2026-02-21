# Public Release Checklist (`econ-data-mcp`)

Last updated: 2026-02-21

## Current Status

- `origin` URL updated to `https://github.com/hanlulong/econ-data-mcp.git`.
- Current tracked files scan does not show live API keys.
- `.gitignore` now excludes `.claude/` and `packages/frontend/dist-data/`.
- Critical blocker: a historical OpenRouter key appears in git history (commit `cc9872c`, legacy docs path), even though it is not present in current files.

## Blocker Before Making Repo Public

1. Rotate exposed credentials now:
   - OpenRouter API key
   - Any key that has ever been present in old docs or env examples
2. Remove sensitive history before flipping visibility.

## Recommended History Strategy (Fastest + Safest)

Use a clean public history (single sanitized initial commit):

```bash
cd /home/hanlulong/OpenEcon

# Ensure sensitive local files remain ignored
git status --short

# Create a fresh history from current sanitized working tree
git checkout --orphan public-main
git add -A
git commit -m "Public release: sanitized initial history"

# Replace default branch history
git branch -M main
git push --force-with-lease origin main
```

Notes:
- This intentionally drops old commit history, which is appropriate when credentials were exposed historically.
- If you must preserve history, use `git filter-repo` and force-push rewritten history instead.

## Pre-Public Verification

```bash
cd /home/hanlulong/OpenEcon
bash scripts/public_release_audit.sh
```

The script checks:
- secrets in tracked files
- dangerous patterns in git history
- risky untracked files

## GitHub Settings Before Visibility Change

1. Confirm default branch is correct (`main`).
2. Enable branch protection on `main`.
3. Keep secret scanning enabled (GitHub Advanced Security if available).
4. Verify no pending PRs contain secrets in diffs.
5. Confirm README, LICENSE, and SECURITY docs are final.
6. Then switch visibility to `Public`.

## Immediate Follow-Up After Public

1. Run a fresh key rotation one more time if any alert appears.
2. Add a lightweight CI check that runs `scripts/public_release_audit.sh`.
3. Create a `v0.1.0` tag/release note with a minimal getting-started path.
