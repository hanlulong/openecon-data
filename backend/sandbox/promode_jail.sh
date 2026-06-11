#!/bin/bash
###############################################################################
# promode_jail.sh — Layer-1 mount-isolation jail for Pro Mode code execution.
#
# SECURITY INVARIANT (the whole point of this file):
#   The LLM-generated Python script runs with a *fabricated* root filesystem
#   that contains ONLY: a read-only system runtime (/usr,/lib,...), the read-only
#   venv, and three writable scratch dirs (/work,/sess,/tmp). The real /home,
#   the real /etc (passwd/shadow/secrets), the repo source tree, and every .env
#   file are NOT bind-mounted, so they simply do not exist inside the jail.
#   The proven exploit
#       from pathlib import Path; Path('/home/hanlulong/OpenEcon/.env').read_text()
#   becomes a FileNotFoundError because /home is not in the mount namespace.
#
# This script is STATIC and checked into the repo. It is NEVER written by, or
# derived from, LLM output. The executor passes only positional path arguments.
# It is invoked under:
#   unshare --user --map-root-user --mount --pid --fork -- /bin/bash <this> ARGS
# so "root" here is a uid-mapped fake root inside an unprivileged user namespace
# (no real privilege on the host). --mount gives us a private mount namespace;
# --pid --fork lets us mount a fresh /proc that only shows jail processes.
#
# Args (all absolute host paths, positional):
#   $1 VENV_PY     - venv python to exec (e.g. /home/.../backend/.venv/bin/python)
#   $2 SCRIPT      - the wrapped user script to run (lives inside WORK_DIR)
#   $3 WORK_DIR    - per-exec scratch  -> bound writable at /work (also HOME)
#   $4 SESS_DIR    - persistent session dir -> bound writable at /sess
#   $5 TMP_OUT_DIR - per-exec output dir -> bound writable at /tmp (plots land here)
#   $6 FONT_CACHE  - prebaked matplotlib cache dir -> bound read-only at /mpl
#   $7 VENV_DIR    - the venv root (carries pyvenv.cfg + numpy.libs OpenBLAS/etc.)
#   $8 TIMEOUT_SECS- hard wall-clock kill applied INSIDE the jail via timeout(1)
#   $9 MEM_BYTES   - RLIMIT_AS cap applied INSIDE the jail via prlimit(1)
#
# On ANY error before exec we must exit non-zero so the parent fails closed.
###############################################################################
set -euo pipefail

# Pin a full PATH FIRST. This script's early commands (mktemp, mount, mkdir, cp,
# pivot_root, umount, timeout, prlimit) run before the locked-down PATH export
# below, using whatever env we were launched with. Under systemd the service env
# is minimal, and under Layer 2 `sudo` strips the env entirely — so a bare
# `mktemp` resolved to "command not found" and the jail (hence the canary) failed
# closed. Set the PATH up front so the jail behaves identically in every launch
# context. (A separate, narrower PATH is re-exported just before the final python
# exec so the jailed code itself sees only /usr/bin:/bin.)
export PATH="/usr/sbin:/usr/bin:/sbin:/bin"

VENV_PY="$1"
SCRIPT="$2"
WORK_DIR="$3"
SESS_DIR="$4"
TMP_OUT_DIR="$5"
FONT_CACHE="$6"
VENV_DIR="$7"
TIMEOUT_SECS="${8:-30}"
MEM_BYTES="${9:-1073741824}"   # RLIMIT_AS cap (default 1 GiB)

# ---------------------------------------------------------------------------
# 1. Build a fresh tmpfs rootfs. We place the mountpoint dir INSIDE the per-exec
#    work dir (not global /tmp) so that even after pivot_root detaches it, the
#    leftover empty mountpoint is removed when the backend rmtree's the work dir.
#    (Putting it in /tmp leaked one promode-owned empty dir per execution.)
#    We mount a tmpfs over it so the new root is wholly in memory and disappears
#    on namespace teardown. NOSUID/NODEV: no setuid escalation, no device nodes.
# ---------------------------------------------------------------------------
NEWROOT="$(mktemp -d "$WORK_DIR/.jailroot.XXXXXX")"
mount -t tmpfs -o nosuid,nodev,size=256m tmpfs "$NEWROOT"

mkdir -p "$NEWROOT"/{proc,dev,etc,work,sess,tmp,mpl,mplcache,old_root}

# ---------------------------------------------------------------------------
# 2. Read-only bind-mount the system runtime. These are the ONLY host paths the
#    jailed code can read. Everything not listed here is invisible.
#    - /usr /lib /lib64 /bin : the Python interpreter + shared libraries.
#    - /etc/alternatives     : CRITICAL. The python3 symlink chain
#                              (venv/bin/python -> python3 -> /usr/bin/python3.12)
#                              and many tool symlinks resolve through here.
#                              Omitting it yields "python: No such file or directory".
#    - /etc/ssl              : CA bundle so httpx TLS to FRED/WorldBank works.
#    - /etc/fonts            : fontconfig config for matplotlib Agg rendering.
#    - $VENV_DIR             : venv site-packages (numpy/pandas/matplotlib/httpx)
#                              + numpy.libs (OpenBLAS/gfortran/quadmath via RPATH)
#                              + pyvenv.cfg (so venv python finds its site-packages).
# We mount each at the SAME absolute path inside NEWROOT so RPATHs / sys.path
# that embed absolute host paths keep resolving after pivot_root.
# ---------------------------------------------------------------------------
ro_bind() {
    # ro_bind <host_path>  -> bind it read-only at the identical path under NEWROOT
    local src="$1"
    [ -e "$src" ] || return 0   # silently skip non-existent optional paths
    local dst="$NEWROOT$src"
    if [ -d "$src" ]; then
        mkdir -p "$dst"
    else
        mkdir -p "$(dirname "$dst")"
        : > "$dst"
    fi
    mount --bind "$src" "$dst"
    # Re-mount read-only. A plain --bind is rw; this second call enforces RO so
    # jailed code cannot tamper with the system runtime or the venv.
    mount -o remount,ro,bind "$dst"
}

ro_bind /usr
ro_bind /lib
ro_bind /lib64
ro_bind /bin
ro_bind /etc/alternatives
ro_bind /etc/ssl
ro_bind /etc/fonts
ro_bind "$VENV_DIR"

# ---------------------------------------------------------------------------
# 3. Writable scratch binds. The /tmp bind is how generated plots ESCAPE the
#    jail: code writes /tmp/promode_*.png inside, which is TMP_OUT_DIR outside,
#    which the executor's file-collection then publishes.
# ---------------------------------------------------------------------------
mount --bind "$WORK_DIR"   "$NEWROOT/work"
mount --bind "$SESS_DIR"   "$NEWROOT/sess"
mount --bind "$TMP_OUT_DIR" "$NEWROOT/tmp"
# Prebaked matplotlib font cache, read-only (avoids 1-2s cold-cache rebuild).
mount --bind "$FONT_CACHE" "$NEWROOT/mpl"
mount -o remount,ro,bind "$NEWROOT/mpl"

# ---------------------------------------------------------------------------
# 4. Fresh /proc (only jail PIDs visible; needs --pid --fork from the caller).
#    Minimal /dev: just the four character devices numeric code legitimately
#    needs. No /dev/mem, no block devices, no /dev/kmsg.
# ---------------------------------------------------------------------------
mount -t proc -o nosuid,nodev,noexec proc "$NEWROOT/proc"

for dev in null urandom random zero; do
    : > "$NEWROOT/dev/$dev"
    mount --bind "/dev/$dev" "$NEWROOT/dev/$dev"
done

# ---------------------------------------------------------------------------
# 5. Minimal /etc. We deliberately do NOT bind the real /etc — that is what
#    hides /etc/passwd real entries, /etc/shadow, and any stray secrets.
#    We synthesize a root-only passwd/group, and copy ONLY the *resolved*
#    resolv.conf + hosts so DNS works for the allowed outbound HTTPS.
#    (resolv.conf on the host is a symlink into /run; cp -L follows it so we
#    capture the actual nameserver lines, not a dangling link.)
# ---------------------------------------------------------------------------
printf 'root:x:0:0:root:/work:/bin/bash\n' > "$NEWROOT/etc/passwd"
printf 'root:x:0:\n'                       > "$NEWROOT/etc/group"
cp -L /etc/resolv.conf "$NEWROOT/etc/resolv.conf" 2>/dev/null || true
cp -L /etc/hosts       "$NEWROOT/etc/hosts"       2>/dev/null || true
# nsswitch so glibc resolves hosts via files+dns only (no nis/ldap surprises).
printf 'hosts: files dns\n' > "$NEWROOT/etc/nsswitch.conf"

# ---------------------------------------------------------------------------
# 6. pivot_root into the new rootfs, then DETACH the old root.
#    The lazy umount of /old_root is ESSENTIAL: without it the entire host
#    filesystem remains reachable under /old_root and the isolation is void.
# ---------------------------------------------------------------------------
cd "$NEWROOT"
pivot_root . old_root
cd /
# Lazy-detach the old host root. After this the host fs is gone from the ns.
umount -l /old_root
rmdir /old_root 2>/dev/null || true

# ---------------------------------------------------------------------------
# 6b. Seed a WRITABLE matplotlib config dir from the read-only prebaked cache.
#     matplotlib needs MPLCONFIGDIR to be writable (it takes a lock file and may
#     touch the cache); a read-only /mpl makes it fall back to building a fresh
#     fontlist (the 1-2s cold-start we are trying to avoid).
#     CRITICAL (Layer 2): we must NOT write this into /work or /tmp — those are
#     host-bound dirs the backend uid later cleans with rmtree, and files written
#     here by the (promode) jail uid would be un-removable by the backend uid
#     ("Permission denied" on cross-uid unlink). Instead we mount a private,
#     in-namespace-only tmpfs at /mplcache (NOT bound to any host path) and seed
#     it from the read-only /mpl. It evaporates with the mount namespace, so it
#     never leaves promode-owned droppings on the host.
# ---------------------------------------------------------------------------
mount -t tmpfs -o nosuid,nodev,size=32m tmpfs /mplcache 2>/dev/null || mkdir -p /mplcache
cp -a /mpl/. /mplcache/ 2>/dev/null || true

# ---------------------------------------------------------------------------
# 7. Locked-down environment, then exec the venv python on the user script.
#    HOME=/work keeps any stray dotfile writes inside scratch.
#    MPLCONFIGDIR=/mplcache: writable in-namespace tmpfs, pre-seeded with the
#    prebaked fontlist so matplotlib import is fast and never rebuilds, and
#    leaves nothing on the host.
#    *_NUM_THREADS=1 keeps OpenBLAS/MKL single-threaded (predictable, no fork
#    storms) — note we rely on RLIMIT_* (set by the parent's preexec_fn), NOT
#    RLIMIT_NPROC, so httpx/numpy threads still spawn.
#
#    TIMEOUT — CRITICAL: we exec `timeout` (which becomes PID 1 of this pid ns)
#    rather than python directly. timeout(1) SIGKILLs python after TIMEOUT_SECS
#    FROM INSIDE the jail, at the SAME uid as python. This is the authoritative
#    wall-clock kill: it does not depend on the host parent being able to signal
#    across the uid boundary (under Layer 2 the backend uid hanlulong CANNOT
#    SIGKILL promode's processes, so an externally-driven kill is unreliable —
#    self-termination is). `-s KILL` sends SIGKILL; `-k 5` adds a 5s grace
#    SIGKILL escalation. Because timeout is PID 1 here, killing it (or it exiting)
#    tears the namespace down. We add a small margin so the parent's asyncio
#    timeout (which collects output) usually fires slightly after this hard kill.
# ---------------------------------------------------------------------------
export HOME=/work
export MPLCONFIGDIR=/mplcache
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1
export PATH=/usr/bin:/bin
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export OMP_NUM_THREADS=1

cd /work
# RLIMIT enforcement INSIDE the jail, via prlimit(1), applied to the python it
# execs. CRITICAL: under Layer 2 the parent spawns us through `sudo`, and sudo's
# PAM (pam_limits) RESETS the rlimits the parent set via preexec_fn — so the
# host-side RLIMIT_AS does NOT reach python. Re-applying here, at the jail uid,
# guarantees the memory cap regardless of sudo. We cap:
#   --as       address space (virtual memory): bounds bytearray/np.zeros DoS.
#   --cpu      CPU-seconds: backstop for CPU-bound loops (timeout(1) covers wall).
#   --fsize    100 MiB max written file.
#   --nofile   256 open fds.
#   --core     0 (no core dumps -> no in-memory data spilled to disk).
# timeout(1) then wraps it for the authoritative wall-clock SIGKILL (see above).
CPU_SECS=$(( TIMEOUT_SECS + 5 ))
exec timeout -s KILL -k 5 "$TIMEOUT_SECS" \
     prlimit \
       --as="$MEM_BYTES" \
       --cpu="$CPU_SECS" \
       --fsize=104857600 \
       --nofile=256 \
       --core=0 \
       -- "$VENV_PY" "$SCRIPT"
