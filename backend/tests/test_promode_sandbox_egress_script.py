"""setup_promode_sandbox.sh egress allowlist: idempotent for BOTH families.

Runs the script's real purge/install functions (extracted verbatim) against a
stateful fake netfilter command, so the mechanism is exercised without root:
re-runs and pre-existing hand-added tagged rules never duplicate, untagged
rules are untouched, IPv6 gets the same uid-scoped allowlist as IPv4, a long
chain listing cannot end the purge early, and a family whose netfilter tool is
missing or unusable is skipped with a warning instead of aborting.
"""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup_promode_sandbox.sh"

HARNESS = r'''
set -euo pipefail
STATE="$1"; shift
TAG="promode-egress"; PUID=996
fake_ipt() {
    case "$1" in
        -S) echo "-P OUTPUT ACCEPT"; cat "$STATE" ;;
        -A) shift; echo "-A $*" >> "$STATE" ;;
        -D) shift; local want="-A $*" done=0 tmp; tmp="$(mktemp)"
            while IFS= read -r l; do
                if [ "$done" = 0 ] && [ "$l" = "$want" ]; then done=1; continue; fi
                printf '%s\n' "$l"
            done < "$STATE" > "$tmp"; mv "$tmp" "$STATE"
            [ "$done" = 1 ] ;;
    esac
}
'''


def _functions() -> str:
    text = SCRIPT.read_text()
    out = []
    for name in ("purge_tagged_rules", "install_egress_allowlist", "provision_family"):
        start = text.index(f"{name}() {{")
        end = text.index("\n}\n", start) + 3
        out.append(text[start:end])
    return "\n".join(out)


def _run_raw(state: Path, *calls: str) -> subprocess.CompletedProcess:
    script = HARNESS + _functions() + "\n" + "\n".join(calls) + "\n"
    return subprocess.run(
        ["bash", "-c", script, "harness", str(state)],
        check=True, timeout=60, capture_output=True, text=True,
    )


def _run(state: Path, *calls: str) -> list:
    _run_raw(state, *calls)
    return [l for l in state.read_text().splitlines() if l]


def _tagged(rules):
    return [r for r in rules if "--comment promode-egress" in r]


def test_syntax_is_valid():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_ipv6_allowlist_shape_and_order(tmp_path):
    state = tmp_path / "v6"
    state.write_text("")
    rules = _tagged(_run(state, "install_egress_allowlist fake_ipt"))
    owner = "-m owner --uid-owner 996"
    assert rules == [
        f"-A OUTPUT {owner} -o lo -j REJECT -m comment --comment promode-egress",
        f"-A OUTPUT {owner} -p udp --dport 53 -j ACCEPT -m comment --comment promode-egress",
        f"-A OUTPUT {owner} -p tcp --dport 53 -j ACCEPT -m comment --comment promode-egress",
        f"-A OUTPUT {owner} -p tcp --dport 443 -j ACCEPT -m comment --comment promode-egress",
        f"-A OUTPUT {owner} -j REJECT -m comment --comment promode-egress",
    ]


def test_ipv4_keeps_metadata_reject_before_accepts(tmp_path):
    state = tmp_path / "v4"
    state.write_text("")
    rules = _tagged(_run(state, "install_egress_allowlist fake_ipt 169.254.169.254/32"))
    assert len(rules) == 6
    assert "-d 169.254.169.254/32 -j REJECT" in rules[1]
    assert rules[-1].endswith("-j REJECT -m comment --comment promode-egress")


@pytest.mark.parametrize("extra", ["", "169.254.169.254/32"])
def test_rerun_and_hand_added_rules_never_duplicate(tmp_path, extra):
    state = tmp_path / "rules"
    state.write_text("-A OUTPUT -p tcp --dport 22 -j ACCEPT\n")
    call = f"install_egress_allowlist fake_ipt {extra}".strip()
    first = _run(state, call)
    # Simulate the hand-applied identical rules already being present, then
    # run provisioning twice more (deploy + service restart).
    state.write_text("\n".join(first + _tagged(first)) + "\n")
    final = _run(state, call, call)
    assert _tagged(final) == _tagged(first)
    assert "-A OUTPUT -p tcp --dport 22 -j ACCEPT" in final


def test_purge_survives_listing_larger_than_a_pipe_buffer(tmp_path):
    """Tagged rules listed FIRST in a chain listing far larger than a pipe
    buffer: an early-exit reader in a pipefail pipe would SIGPIPE the lister
    and end the purge early, leaving stale copies to be duplicated."""
    state = tmp_path / "big"
    tagged = _tagged(_run(state, "install_egress_allowlist fake_ipt"))
    filler = [f"-A OUTPUT -p tcp --dport {1024 + i} -j ACCEPT -m comment --comment filler-{i}"
              for i in range(3000)]
    state.write_text("\n".join(tagged + filler) + "\n")
    assert len(state.read_bytes()) > 2 * 65536

    final = _run(state, "install_egress_allowlist fake_ipt")

    assert _tagged(final) == tagged
    assert [r for r in final if "filler-" in r] == filler


@pytest.mark.parametrize("probe", ["missing", "broken"])
def test_unusable_family_is_skipped_with_warning_not_fatal(tmp_path, probe):
    """A missing binary, or one whose `-S OUTPUT` probe fails (no netfilter
    for that family), warns and returns success without touching rules."""
    state = tmp_path / "rules"
    state.write_text("-A OUTPUT -p tcp --dport 22 -j ACCEPT\n")
    calls = []
    if probe == "missing":
        tool = "promode_no_such_ip6tables_binary"
    else:
        calls.append("broken_ipt() { [ \"$1\" = -S ] && return 3; fake_ipt \"$@\"; }")
        tool = "broken_ipt"
    calls.append(f"provision_family {tool} IPv6")
    calls.append("echo provision-continued")

    proc = _run_raw(state, *calls)

    assert "provision-continued" in proc.stdout
    assert f"WARNING: {tool} unavailable; IPv6 egress allowlist NOT applied" in proc.stderr
    assert state.read_text() == "-A OUTPUT -p tcp --dport 22 -j ACCEPT\n"


def test_usable_family_is_provisioned(tmp_path):
    state = tmp_path / "rules"
    state.write_text("")
    proc = _run_raw(state, "provision_family fake_ipt IPv6")
    assert len(_tagged(state.read_text().splitlines())) == 5
    assert "fake_ipt egress allowlist installed for uid 996" in proc.stdout


def test_script_provisions_both_families():
    """Top-level wiring: IPv4 (with the metadata REJECT) and the IPv6 mirror."""
    lines = [l.strip() for l in SCRIPT.read_text().splitlines()]
    assert "provision_family iptables IPv4 169.254.169.254/32" in lines
    assert "provision_family ip6tables IPv6" in lines
