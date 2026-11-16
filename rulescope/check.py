"""Pre-install checks: reachability, version, license, Workflows and data."""

from __future__ import annotations

from dataclasses import dataclass, field

from .client import Client, HttpError
from .core import MIN_VERSION, TESTED_VERSION, WORKFLOWS_API, version_tuple
from .report import Reporter


@dataclass
class CheckResult:
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: dict = field(default_factory=dict)


def run_check(client: Client, rep: Reporter, fix_license: bool = False) -> CheckResult:
    res = CheckResult()
    cfg = client.cfg

    # Elasticsearch reachable, version and flavor
    try:
        _, root = client.es("GET", "/")
    except HttpError as err:
        res.blocking.append(f"Elasticsearch not reachable: {err}")
        rep.line("BLOCKED", "elasticsearch", cfg.es_url, str(err))
        return res
    ver = root.get("version", {})
    number, flavor = ver.get("number", "?"), ver.get("build_flavor", "default")
    res.info.update(version=number, flavor=flavor)
    detected = "serverless" if flavor == "serverless" else "stateful"
    if (cfg.target == "serverless") != (detected == "serverless"):
        msg = f"target file says {cfg.target}, but the cluster looks {detected}"
        res.warnings.append(msg)
        rep.line("WARN", "target", cfg.target, msg)
    client.serverless = detected == "serverless"
    rep.line("ok", "elasticsearch", cfg.es_url, f"version {number}, flavor {flavor}")

    if detected != "serverless":
        if version_tuple(number) < MIN_VERSION:
            msg = f"version {number} is older than {TESTED_VERSION}"
            res.blocking.append(msg)
            rep.line("BLOCKED", "version", number, msg)
        elif cfg.target == "docker" and number != TESTED_VERSION:
            msg = f"lab should run {TESTED_VERSION}, found {number}"
            res.warnings.append(msg)
            rep.line("WARN", "version", number, msg)

        # License
        _, lic = client.es("GET", "/_license")
        ltype = (lic or {}).get("license", {}).get("type", "unknown")
        lstatus = (lic or {}).get("license", {}).get("status", "unknown")
        if ltype in ("enterprise", "trial") and lstatus == "active":
            rep.line("ok", "license", ltype)
        elif ltype == "basic" and cfg.target == "docker" and fix_license:
            client.es("POST", "/_license/start_trial?acknowledge=true")
            rep.line("fixed", "license", "trial", "started a 30-day trial in the lab")
        else:
            msg = f"Workflows needs an Enterprise or trial license (found {ltype}, {lstatus})"
            res.blocking.append(msg)
            rep.line("BLOCKED", "license", ltype, msg)
    else:
        rep.line("skipped", "license", "serverless", "no license on Serverless")

    # Kibana reachable
    try:
        _, status = client.kb("GET", "/api/status")
        level = ((status or {}).get("status", {}).get("overall", {}) or {}).get("level", "?")
        rep.line("ok", "kibana", cfg.kibana_url, f"status {level}")
    except HttpError as err:
        res.blocking.append(f"Kibana not reachable: {err}")
        rep.line("BLOCKED", "kibana", cfg.kibana_url, str(err))
        return res

    # Workflows API: without it there is nothing to run the profiler.
    try:
        code, _ = client.kb("GET", WORKFLOWS_API, allow=(400, 403, 404))
        if code < 400:
            rep.line("ok", "workflows", WORKFLOWS_API)
            res.info["workflows_api"] = True
        else:
            msg = (f"Workflows isn't available here (HTTP {code}). It needs 9.4 or later with an "
                   "Enterprise or trial license, and a user with Workflows privileges.")
            res.blocking.append(msg)
            rep.line("BLOCKED", "workflows", WORKFLOWS_API, msg)
    except HttpError as err:
        res.blocking.append(str(err))
        rep.line("BLOCKED", "workflows", WORKFLOWS_API, str(err))

    # event.ingested coverage in the last hour
    query = ("FROM logs-* | WHERE @timestamp >= NOW() - 1 hour "
             "| STATS total = COUNT(*), measured = COUNT(event.ingested)")
    try:
        code, out = client.es("POST", "/_query", {"query": query}, allow=(400, 404))
        if code >= 400:
            rep.line("WARN", "data", "logs-*", "no logs-* data found yet")
            res.warnings.append("no logs-* data found yet")
        else:
            total, measured = (out.get("values") or [[0, 0]])[0]
            pct = (measured / total * 100) if total else 0.0
            status_word = "ok" if total and pct >= 95 else "WARN"
            detail = f"{total} events in the last hour, {pct:.1f}% have event.ingested"
            rep.line(status_word, "data", "logs-*", detail)
            if status_word == "WARN":
                res.warnings.append(detail)
    except HttpError as err:
        rep.line("WARN", "data", "logs-*", str(err))
        res.warnings.append(str(err))
    return res
