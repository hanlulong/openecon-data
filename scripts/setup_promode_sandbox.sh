#!/bin/bash
###############################################################################
# setup_promode_sandbox.sh — LAYER 2 provisioning for the Pro Mode sandbox.
#
# Idempotent. Run as root (or via sudo). Safe to re-run on every deploy.
#
# It provisions the "defense in depth beyond mount isolation" layer:
#   1. A dedicated, unprivileged system user `promode` (no shell, no home, no
#      sudo) that the jail runs AS. Isolates the jailed code from the backend
#      uid (hanlulong) entirely — a kernel-namespace bug can't reach hanlulong's
#      files/processes because the process simply isn't hanlulong.
#   2. A shared group `promode-share` so the backend (hanlulong) can still
#      collect output files written by promode (setgid on the output tree).
#   3. A TIGHTLY-scoped sudoers drop-in letting ONLY hanlulong run ONLY
#      /usr/bin/unshare as promode (NOT NOPASSWD:ALL, NOT for promode itself).
#   4. A uid-scoped iptables egress allowlist for promode: block loopback
#      (SSRF to backend:3001 / redis), block cloud metadata (169.254.169.254),
#      allow DNS(53)+HTTPS(443), reject the rest. This is what stops the
#      Layer-1 residual: under Layer-1-only, jailed httpx CAN still reach
#      127.0.0.1:3001. With this, it cannot.
#
# After success it writes /etc/sudoers.d/promode (the marker the executor probes
# to decide whether to run the jail as the promode uid).
#
# REVERSAL (manual):
#   sudo userdel promode 2>/dev/null
#   sudo groupdel promode-share 2>/dev/null
#   sudo rm -f /etc/sudoers.d/promode
#   sudo iptables -L OUTPUT -n --line-numbers   # then delete the promode rules
###############################################################################
set -euo pipefail

# systemd ExecStartPre runs with a minimal PATH that omits /usr/sbin and even
# /usr/bin, so bare `id`/`getent`/`groupadd`/`useradd`/`iptables` resolve to
# "command not found" and the whole re-provision silently no-ops (the unit uses
# `ExecStartPre=-` so the failure is ignored). That would leave the ephemeral
# iptables egress rules un-applied after a reboot. Pin a full PATH so this
# script behaves identically whether run from a deploy shell or from systemd.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

PROMODE_USER="promode"
SHARE_GROUP="promode-share"
BACKEND_USER="${BACKEND_USER:-hanlulong}"
PUBLIC_DIR="${PROMODE_PUBLIC_DIR:-/home/hanlulong/OpenEcon/public_media/promode}"
SESSION_DIR="${PROMODE_SESSION_DIR:-/tmp/promode_sessions}"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must run as root (use sudo)." >&2
    exit 1
fi

echo "[promode-setup] backend_user=$BACKEND_USER public_dir=$PUBLIC_DIR"

# ---------------------------------------------------------------------------
# 1. Shared group + dedicated user (idempotent).
# ---------------------------------------------------------------------------
if ! getent group "$SHARE_GROUP" >/dev/null; then
    groupadd --system "$SHARE_GROUP"
    echo "[promode-setup] created group $SHARE_GROUP"
fi

if ! id -u "$PROMODE_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin \
            --gid "$SHARE_GROUP" "$PROMODE_USER"
    echo "[promode-setup] created user $PROMODE_USER"
fi

# Backend user must be in the shared group to read/move promode's output files.
if ! id -nG "$BACKEND_USER" | tr ' ' '\n' | grep -qx "$SHARE_GROUP"; then
    usermod -aG "$SHARE_GROUP" "$BACKEND_USER"
    echo "[promode-setup] added $BACKEND_USER to $SHARE_GROUP (re-login or restart service to take effect)"
fi

# promode must have NO sudo rights of its own.
gpasswd -d "$PROMODE_USER" sudo 2>/dev/null || true

# ---------------------------------------------------------------------------
# 2. Output/session dirs: setgid + group-shared so files written by promode are
#    group-owned by promode-share and collectable by hanlulong.
#    NOTE: the per-exec output dir is created by the backend (hanlulong) under
#    SESSION_DIR/work; we set the group + setgid on the parents so children
#    inherit the group. The jail runs as promode and writes files there; they
#    inherit group=promode-share and (with umask) are group-writable, so
#    hanlulong (also in the group) can shutil.move them.
# ---------------------------------------------------------------------------
for d in "$PUBLIC_DIR" "$SESSION_DIR" "$SESSION_DIR/work"; do
    mkdir -p "$d"
    chgrp "$SHARE_GROUP" "$d" 2>/dev/null || true
    # rwx for owner+group, setgid so new files/dirs inherit the group.
    chmod 2770 "$d" 2>/dev/null || true
done
echo "[promode-setup] dirs prepared with setgid + group $SHARE_GROUP"

# ---------------------------------------------------------------------------
# 3. Scoped sudoers drop-in. ONLY hanlulong, ONLY to run unshare AS promode.
#    Validate with visudo -c before installing (a broken sudoers locks out sudo).
# ---------------------------------------------------------------------------
SUDOERS_FILE="/etc/sudoers.d/promode"
SUDOERS_TMP="$(mktemp)"
cat > "$SUDOERS_TMP" <<EOF
# Managed by scripts/setup_promode_sandbox.sh — do not edit by hand.
# Allow the backend user to launch the Pro Mode jail AS the promode uid.
# Scoped to exactly /usr/bin/unshare; promode has no rights of its own.
$BACKEND_USER ALL=($PROMODE_USER) NOPASSWD: /usr/bin/unshare
EOF
chmod 0440 "$SUDOERS_TMP"
if visudo -c -f "$SUDOERS_TMP" >/dev/null 2>&1; then
    install -m 0440 -o root -g root "$SUDOERS_TMP" "$SUDOERS_FILE"
    echo "[promode-setup] installed $SUDOERS_FILE"
else
    echo "ERROR: generated sudoers failed visudo -c; NOT installing." >&2
    rm -f "$SUDOERS_TMP"
    exit 1
fi
rm -f "$SUDOERS_TMP"

# ---------------------------------------------------------------------------
# 4. uid-scoped egress allowlist for promode. Idempotent: we tag rules with a
#    comment and remove any prior copies before re-adding.
#    Order matters (first match wins): REJECT loopback + metadata BEFORE the
#    ACCEPT for 53/443, then a final REJECT catch-all for promode.
# ---------------------------------------------------------------------------
TAG="promode-egress"
PUID="$(id -u "$PROMODE_USER")"

if command -v iptables >/dev/null 2>&1; then
    # Remove any existing promode-egress rules (idempotency). iptables -S prints
    # the comment WITHOUT quotes, so we match the bare tag. We round-trip each
    # matching -A line into a -D delete until none remain (guard against loops).
    _guard=0
    while iptables -S OUTPUT 2>/dev/null | grep -q -- "--comment $TAG"; do
        line="$(iptables -S OUTPUT | grep -- "--comment $TAG" | head -1 | sed 's/^-A/-D/')"
        # shellcheck disable=SC2086
        iptables $line 2>/dev/null || break
        _guard=$((_guard + 1))
        [ "$_guard" -gt 50 ] && { echo "WARN: too many promode-egress rules, aborting cleanup" >&2; break; }
    done

    add() { iptables -A OUTPUT -m owner --uid-owner "$PUID" "$@" -m comment --comment "$TAG"; }

    # Block loopback egress (SSRF to backend:3001, redis, etc.). DNS/HTTPS go out
    # the real interface, not lo, so this does not break legitimate fetches.
    add -o lo -j REJECT
    # Block cloud metadata service.
    add -d 169.254.169.254/32 -j REJECT
    # Allow DNS + HTTPS only.
    add -p udp --dport 53 -j ACCEPT
    add -p tcp --dport 53 -j ACCEPT
    add -p tcp --dport 443 -j ACCEPT
    # Reject everything else from promode.
    add -j REJECT

    echo "[promode-setup] iptables egress allowlist installed for uid $PUID"
    echo "[promode-setup] NOTE: persist across reboot via netfilter-persistent/iptables-save or systemd ExecStartPre."
else
    echo "[promode-setup] WARNING: iptables not found; egress allowlist NOT applied." >&2
    echo "[promode-setup] Layer-1 mount isolation still hides all secrets; SSRF to loopback remains possible." >&2
fi

echo "[promode-setup] DONE. Pro Mode jail will now run as '$PROMODE_USER' (executor auto-detects the sudoers marker)."
