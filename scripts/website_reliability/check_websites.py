#!/usr/bin/env python3
"""Check this server's public HTTP/TLS routes over both IP families."""

import argparse
import concurrent.futures
import datetime
import json
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

HOSTS = (
    "luhan.io", "www.luhan.io", "luhanecon.com", "www.luhanecon.com",
    "jiaqili.io", "www.jiaqili.io", "hansearch.com", "www.hansearch.com",
    "app.hansearch.com", "agent.hansearch.com", "n8n.hansearch.com",
    "jp.hansearch.com", "api.hansearch.com", "crypto.hansearch.com",
    "data.openecon.ai", "deepecon.ai", "www.deepecon.ai", "openecon.ai",
    "www.openecon.ai", "trade.hansearch.com",
)
KNOWN_DOWN = {"app.hansearch.com", "agent.hansearch.com"}
CANONICAL = {
    "deepecon.ai": "openecon.ai",
    "www.deepecon.ai": "openecon.ai",
    "www.openecon.ai": "openecon.ai",
}
JOBS = {(host, scheme, family) for host in HOSTS for scheme in ("http", "https") for family in (4, 6)}


def probe(job):
    host, scheme, family = job
    result = subprocess.run([
        "curl", "-q", "--noproxy", "*", "--proto", "=http,https",
        "--proto-redir", "=https", "-sS", "-L", "--max-redirs", "5",
        "--connect-timeout", "4", "--max-time", "12", f"-{family}",
        "-o", "/dev/null", "-w", "%{json}", f"{scheme}://{host}/",
    ], capture_output=True, text=True, timeout=15)
    try:
        metrics = json.loads(result.stdout)
        final = urlsplit(metrics.get("url_effective", ""))
        valid_target = (
            final.scheme == "https" and final.hostname == CANONICAL.get(host, host)
            and final.port in (None, 443) and final.username is None and final.password is None
        )
    except (ValueError, AttributeError):
        metrics = {}
        valid_target = False
    code = metrics.get("http_code", 0)
    tls_verified = metrics.get("ssl_verify_result") == 0
    degraded = host in KNOWN_DOWN and code == 503
    return {
        "host": host, "scheme": scheme, "ip_family": family, "http_status": code,
        "curl_exit_code": result.returncode, "tls_verified": tls_verified,
        "canonical_target_verified": valid_target,
        "elapsed_seconds": metrics.get("time_total"),
        "known_degraded": degraded,
        "ok": result.returncode == 0 and tls_verified and valid_target and (code == 200 or degraded),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(probe, sorted(JOBS)))
    report = {
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "results": rows,
    }
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps({"checked": len(rows), "passed": sum(row["ok"] for row in rows)}))
    return 0 if all(row["ok"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
