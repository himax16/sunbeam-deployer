"""Verify embedded COS observability features inside the bootstrap VM.

Runs standalone on the primary Sunbeam node (where ``juju`` is available)
using only the Python standard library. It checks Grafana health, the
documented dashboards, Prometheus/loki datasources, and that metrics and
logs are flowing through Grafana's datasource proxy. The Grafana admin
password is never printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

_EXPECTED_DASHBOARDS = (
    "Service Overview",
    "Cloud Usage",
    "Compute Overview",
    "Capacity",
    "Project Overview",
    "Logging",
)


def _grafana_action() -> tuple[str, str]:
    """Return ``(admin-password, url)`` from the grafana leader action."""
    proc = subprocess.run(
        [
            "juju",
            "run",
            "-m",
            "observability",
            "grafana/leader",
            "get-admin-password",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout)
    first_unit = next(iter(data.values()))
    results = first_unit["results"]
    return (results["admin-password"], results["url"])


def _get(
    url: str,
    password: str,
    path: str,
    params: dict | None = None,
) -> tuple[int, str]:
    """GET *path* on *url* with basic auth; return ``(status, body)``."""
    target = url.rstrip("/") + path
    if params:
        target += "?" + urllib.parse.urlencode(params)
    token = base64.b64encode(f"admin:{password}".encode()).decode()
    req = urllib.request.Request(target)
    req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return (resp.status, resp.read().decode())
    except urllib.error.HTTPError as exc:
        return (exc.code, exc.read().decode())
    except urllib.error.URLError as exc:
        return (0, str(exc))


def _find_missing_dashboards(
    titles: list[str], expected: tuple[str, ...]
) -> list[str]:
    """Return the expected dashboard substrings absent from *titles*."""
    lowered = [t.lower() for t in titles]
    return [e for e in expected if not any(e.lower() in t for t in lowered)]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify embedded COS observability features."
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Per-poll deadline in seconds (default: 300)",
    )
    return parser.parse_args()


def _record(name: str, ok: bool, detail: str, failures: list[str]) -> None:
    if ok:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name}: {detail}")
        failures.append(name)


def main() -> int:
    args = _parse_args()
    deadline = time.time() + args.timeout

    try:
        password, url = _grafana_action()
    except Exception as exc:
        print(f"FAIL grafana-admin-password: {exc}")
        return 1
    print(f"Grafana URL: {url}")

    failures: list[str] = []

    # Health
    status, body = _get(url, password, "/api/health")
    _record("grafana-health", status == 200, f"HTTP {status}", failures)

    # Dashboards
    status, body = _get(url, password, "/api/search")
    titles: list[str] = []
    missing: list[str] = []
    if status == 200:
        try:
            dashboards = json.loads(body)
        except ValueError:
            dashboards = []
        titles = [d.get("title", "") for d in dashboards]
        missing = _find_missing_dashboards(titles, _EXPECTED_DASHBOARDS)
    _record(
        "dashboards",
        status == 200 and not missing,
        f"missing={missing} found={titles} HTTP {status}",
        failures,
    )

    # Datasources
    prom_uid: str | None = None
    loki_uid: str | None = None
    status, body = _get(url, password, "/api/datasources")
    if status == 200:
        try:
            datasources = json.loads(body)
        except ValueError:
            datasources = []
        for ds in datasources:
            if ds.get("type") == "prometheus":
                prom_uid = ds.get("uid")
            elif ds.get("type") == "loki":
                loki_uid = ds.get("uid")
    datasources_ok = bool(prom_uid and loki_uid)
    _record(
        "datasources",
        datasources_ok,
        f"prometheus_uid={prom_uid} loki_uid={loki_uid}",
        failures,
    )

    if not datasources_ok:
        print(f"FAILED: {len(failures)} check(s) failed")
        return 1

    # Metrics (retry until deadline)
    metrics_ok = False
    metrics_detail = "timeout"
    while time.time() < deadline:
        status, body = _get(
            url,
            password,
            f"/api/datasources/proxy/uid/{prom_uid}/api/v1/query",
            {"query": 'count({__name__=~"openstack.+"})'},
        )
        if status == 200:
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = {}
            result = parsed.get("data", {}).get("result", [])
            if parsed.get("status") == "success" and result:
                try:
                    if int(result[0]["value"][1]) > 0:
                        metrics_ok = True
                        break
                except (KeyError, ValueError, IndexError):
                    pass
        metrics_detail = f"HTTP {status}"
        time.sleep(15)
    _record("metrics", metrics_ok, metrics_detail, failures)

    # Logs (retry until deadline)
    logs_ok = False
    logs_detail = "timeout"
    while time.time() < deadline:
        status, body = _get(
            url,
            password,
            f"/api/datasources/proxy/uid/{loki_uid}/loki/api/v1/labels",
        )
        if status == 200:
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = {}
            if parsed.get("status") == "success" and parsed.get("data"):
                logs_ok = True
                break
        logs_detail = f"HTTP {status}"
        time.sleep(15)
    _record("logs", logs_ok, logs_detail, failures)

    if failures:
        print(f"FAILED: {len(failures)} check(s) failed")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
