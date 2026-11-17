#!/usr/bin/env python3
"""One-off: move a lab's Ingest Lag Profiler history from lagprof-* to rulescope-lag-*.

Before the restructure the lag module wrote lagprof-results, lagprof-stream-stats,
lagprof-host-lag and lagprof-runs, with fields under lagprof.*. This copies each into
its rulescope-lag-* data stream, renaming the fields, so the dashboard keeps its
trend lines. Document IDs are kept, so running it twice copies nothing new.

    migrate_lagprof.py --target-file targets/docker.env            copy history
    migrate_lagprof.py ... --remove-old                            also remove the old
        workflows, dashboards, alert rules, roles, templates and pipelines
    migrate_lagprof.py ... --remove-old --delete-old-data          and the lagprof-* data

Install rulescope first (python3 -m rulescope install), so the rulescope-lag-* index
templates exist. Field changes:
    lagprof.run / window / space / rule   ->  rulescope.run / window / space / rule
    lagprof.mode                          ->  rulescope.scope (global, or space:<id>)
    lagprof.wrote_shared_stats            ->  wrote_shared_stats
Results have no mode; --scope says which scope wrote them (default global; per-space
uses each result's space). Stream stats and host lag get no scope, as in rulescope.

Nothing outside the lab needs this: there was no release before the rename.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rulescope.client import Client  # noqa: E402
from rulescope.config import load_config  # noqa: E402
from rulescope.core import WORKFLOWS_API, space_prefix  # noqa: E402

STREAMS = ("results", "stream-stats", "host-lag", "runs")
SCOPED = ("results", "runs")

RENAME = """
Map lp = ctx._source.remove('lagprof');
if (lp == null) { return; }
Map rs = new HashMap();
for (String k : ['run', 'window', 'space', 'rule']) { if (lp.containsKey(k)) { rs.put(k, lp.get(k)); } }
if (lp.containsKey('wrote_shared_stats')) { ctx._source.wrote_shared_stats = lp.get('wrote_shared_stats'); }
if (params.scoped) {
  def mode = lp.get('mode');
  def space = lp.get('space') instanceof Map ? lp.get('space').get('id') : null;
  if (mode == 'all-spaces') { rs.scope = 'global'; }
  else if (mode == 'per-space' && space != null) { rs.scope = 'space:' + space; }
  else if (params.scope == 'per-space' && space != null) { rs.scope = 'space:' + space; }
  else { rs.scope = 'global'; }
}
ctx._source.rulescope = rs;
"""

OLD_ES = {
    "/_index_template/": ("lagprof-results", "lagprof-stream-stats", "lagprof-host-lag", "lagprof-runs",
                          "lagprof-rules", "lagprof-rule-streams"),
    "/_component_template/": ("lagprof-common",),
    "/_ingest/pipeline/": ("lagprof-rules-parse", "lagprof-streams-parse", "lagprof-stamp"),
}


def copy_history(c: Client, scope: str) -> int:
    failures = 0
    for name in STREAMS:
        src, dest = f"lagprof-{name}", f"rulescope-lag-{name}"
        code, _ = c.es("GET", f"/_data_stream/{src}", allow=(404,))
        if code == 404:
            print(f"skip    {src}: not found")
            continue
        code, _ = c.es("GET", f"/_index_template/{dest}", allow=(404,))
        if code == 404:
            print(f"FAILED  {dest}: index template missing; run python3 -m rulescope install first")
            failures += 1
            continue
        body = {"conflicts": "proceed", "source": {"index": src},
                "dest": {"index": dest, "op_type": "create"},
                "script": {"lang": "painless", "source": RENAME,
                           "params": {"scoped": name in SCOPED, "scope": scope}}}
        _, out = c.es("POST", "/_reindex?refresh=true&wait_for_completion=true", body)
        bad = out.get("failures") or []
        print(f"{'FAILED ' if bad else 'copied '} {src} -> {dest}: {out.get('created', 0)} created, "
              f"{out.get('version_conflicts', 0)} already there, of {out.get('total', 0)}")
        for f in bad[:3]:
            print(f"        {f.get('cause', f)}")
        failures += bool(bad)
    return failures


def remove_old(c: Client, delete_data: bool) -> None:
    _, spaces = c.kb("GET", "/api/spaces/space")
    for sid in [s["id"] for s in spaces]:
        prefix = space_prefix(sid)
        code, out = c.kb("GET", f"{prefix}{WORKFLOWS_API}?size=1000", allow=(400, 404))
        for w in (out or {}).get("results", []) if code == 200 else []:
            if str(w.get("name", "")).startswith("lagprof-hourly"):
                c.kb("DELETE", f"{prefix}{WORKFLOWS_API}/workflow/{w['id']}")
                print(f"deleted workflow {w['name']} ({sid})")
        suffix = "" if sid == "default" else f"-{sid}"
        for rule in (f"lagprof-miss-rate{suffix}", f"lagprof-stale-profiler{suffix}",
                     "lagprof-miss-rate", "lagprof-stale-profiler"):
            code, _ = c.kb("DELETE", f"{prefix}/api/alerting/rule/{rule}", allow=(404,))
            if code < 300:
                print(f"deleted alert rule {rule} ({sid})")
        for dash in {f"lagprof-overview{suffix}", "lagprof-overview", f"lagprof-overview-{sid}"}:
            code, _ = c.kb("DELETE", f"{prefix}/api/dashboards/{dash}", allow=(404,))
            if code < 300:
                print(f"deleted dashboard {dash} ({sid})")
    _, roles = c.kb("GET", "/api/security/role")
    for role in roles or []:
        if role.get("name", "").startswith("lagprof-workflow"):
            c.kb("DELETE", f"/api/security/role/{role['name']}")
            print(f"deleted role {role['name']}")
    if delete_data:
        c.es("DELETE", "/_data_stream/lagprof-*", allow=(404,))
        c.es("DELETE", "/lagprof-rules,lagprof-rule-streams?ignore_unavailable=true", allow=(404,))
        print("deleted lagprof-* data streams and indices")
    for path, names in OLD_ES.items():
        for name in names:
            code, _ = c.es("DELETE", path + name, allow=(400, 404))
            if code == 400:
                print(f"kept    {path}{name}: still in use (add --delete-old-data)")
            elif code < 300:
                print(f"deleted {path}{name}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target-file", default="targets/docker.env")
    ap.add_argument("--scope", choices=("global", "per-space"), default="global",
                    help="the scope that wrote the old results")
    ap.add_argument("--remove-old", action="store_true", help="remove the old lagprof Kibana and Elasticsearch objects")
    ap.add_argument("--delete-old-data", action="store_true", help="with --remove-old: delete lagprof-* data too")
    args = ap.parse_args()
    c = Client(load_config(args.target_file))
    failures = copy_history(c, args.scope)
    if failures:
        print("Not removing anything: fix the failures above first.")
        return 1
    if args.remove_old:
        remove_old(c, args.delete_old_data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
