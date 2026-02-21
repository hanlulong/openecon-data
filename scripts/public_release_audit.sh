#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "== econ-data-mcp public release audit =="

SECRET_PATTERNS='(sk-[A-Za-z0-9]{20,}|-----BEGIN (RSA |EC |DSA )?PRIVATE KEY-----|BEGIN OPENSSH PRIVATE KEY|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,})'
UNTRACKED_PATTERNS='^\?\? (\.env|.*\.pem|.*\.key|\.claude/|.*credentials|.*secret)'

tmp_tracked="$(mktemp /tmp/public_audit_tracked_hits.XXXXXX)"
tmp_hist="$(mktemp /tmp/public_audit_hist_hits.XXXXXX)"
tmp_untracked="$(mktemp /tmp/public_audit_untracked_hits.XXXXXX)"
trap 'rm -f "$tmp_tracked" "$tmp_hist" "$tmp_untracked"' EXIT

fail=0

echo
echo "[1/4] Scanning tracked files for secret-like patterns..."
if command -v rg >/dev/null 2>&1; then
  tracked_scan_cmd='git ls-files | rg -v "^scripts/public_release_audit\\.sh$" | xargs rg -n "$SECRET_PATTERNS" >"$tmp_tracked" 2>/dev/null'
else
  tracked_scan_cmd='git ls-files | grep -Ev "^scripts/public_release_audit\\.sh$" | xargs grep -nE "$SECRET_PATTERNS" >"$tmp_tracked" 2>/dev/null'
fi

if eval "$tracked_scan_cmd"; then
  echo "FAIL: secret-like matches found in tracked files:"
  cat "$tmp_tracked"
  fail=1
else
  echo "PASS: no secret-like matches in tracked files"
fi

echo
echo "[2/4] Checking git history for known high-risk key patterns..."
hist_hits=0
for pat in "OPENROUTER_API_KEY=sk-or-v1-[a-f0-9]{32,}" "SUPABASE_SERVICE_ROLE_KEY=eyJ[0-9A-Za-z._-]{20,}" "BEGIN (RSA |EC |DSA )?PRIVATE KEY"; do
  if git log --all --oneline -G "$pat" -- | head -n 5 >"$tmp_hist" && [ -s "$tmp_hist" ]; then
    echo "WARN: history matches pattern '$pat':"
    cat "$tmp_hist"
    hist_hits=1
  fi
done
if [ "$hist_hits" -eq 0 ]; then
  echo "PASS: no high-risk history patterns detected"
else
  echo "WARN: history contains high-risk patterns; sanitize history before public release."
fi

echo
echo "[3/4] Checking remote origin URL..."
origin_url="$(git remote get-url origin 2>/dev/null || true)"
echo "origin: ${origin_url:-<missing>}"
if [[ "${origin_url:-}" != *"github.com/hanlulong/econ-data-mcp.git"* ]]; then
  echo "WARN: origin does not match expected public repo URL."
fi

echo
echo "[4/4] Checking risky untracked files..."
if command -v rg >/dev/null 2>&1; then
  untracked_scan_cmd='git status --short | rg -n "$UNTRACKED_PATTERNS" >"$tmp_untracked"'
else
  untracked_scan_cmd='git status --short | grep -nE "$UNTRACKED_PATTERNS" >"$tmp_untracked"'
fi

if eval "$untracked_scan_cmd"; then
  echo "WARN: risky untracked files detected:"
  cat "$tmp_untracked"
else
  echo "PASS: no obvious risky untracked files"
fi

echo
if [ "$fail" -ne 0 ]; then
  echo "RESULT: FAIL (tracked file secrets detected)"
  exit 1
fi

echo "RESULT: PASS for tracked files; review WARN items before making repo public."
