"""setup_promode_sandbox.sh egress allowlist: idempotent for BOTH families.

Runs the script's real purge/install functions (extracted verbatim) against a
stateful fake netfilter command, so the mechanism is exercised without root:
re-runs and pre-existing hand-added tagged rules never duplicate, untagged
rules are untouched, IPv6 gets the same uid-scoped allowlist as IPv4, and the
IPv6 step is skipped (not fatal) when ip6tables is unusable.
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
    for name in ("purge_tagged_rules", "install_egress_allowlist"):
        start = text.index(f"{name}() {{")
        end = text.index("\n}\n", start) + 3
        out.append(text[start:end])
    return "\n".join(out)


def _run(state: Path, *calls: str) -> list:
    script = HARNESS + _functions() + "\n" + "\n".join(calls) + "\n"
    subprocess.run(["bash", "-c", script, "harness", str(state)], check=True, timeout=30)
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


def test_ipv6_step_is_guarded_and_non_fatal():
    text = SCRIPT.read_text()
    assert "command -v ip6tables >/dev/null 2>&1 && ip6tables -S OUTPUT >/dev/null 2>&1" in text
    assert "install_egress_allowlist ip6tables" in text
    assert "ip6tables unavailable; IPv6 egress allowlist NOT applied" in text
