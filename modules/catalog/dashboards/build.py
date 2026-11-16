#!/usr/bin/env python3
"""Build modules/catalog/dashboards/rulescope-catalog-health.json, the Rule Catalog dashboard.

The JSON is a Kibana Dashboards API body (PUT /api/dashboards/rulescope-catalog-health).
Every panel reads the rulescope-catalog-* indices with ES|QL. A per-space install
filters each panel to its own scope (see resources.scope_query).

    build.py                                            write the JSON
    build.py --check --target-file targets/docker.env   also run every query first
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from rulescope.panels import Panels, check_queries, g, markdown  # noqa: E402

OUT = HERE / "rulescope-catalog-health.json"

INTRO = """**Rule Catalog.** Every detection rule in every space the catalog covers, enabled or not, synced \
hourly by the rulescope-catalog-sync workflow, with a daily snapshot kept for 400 days. Report modules read the \
catalog, and a module run refuses to use a catalog more than 2 hours old. *Catalog health* shows the last sync \
per scope."""

Q = {
    "health": """FROM rulescope-catalog-syncs
| WHERE kind == "sync" OR kind == "snapshot"
| INLINE STATS latest = MAX(@timestamp) BY rulescope.scope, kind
| EVAL is_last = @timestamp == latest
| STATS last_run = MAX(@timestamp),
        last_success = MAX(CASE(status == "success", @timestamp, null)),
        runs = COUNT(*),
        failed = SUM(CASE(status == "failed", 1, 0)),
        last_status = MAX(CASE(is_last, status, null)),
        rules_seen = MAX(CASE(is_last, rules_seen, null)),
        pairs = MAX(CASE(is_last, pairs_written, null)),
        capped_spaces = MAX(CASE(is_last, rule_cap_spaces, null))
     BY scope = rulescope.scope, workflow = kind
| EVAL minutes_since_success = DATE_DIFF("minutes", last_success, NOW()),
       state = CASE(last_success IS NULL, "never succeeded",
                    workflow == "sync" AND minutes_since_success > 120, "STALE: modules won't use it",
                    workflow == "snapshot" AND minutes_since_success > 1500, "no snapshot today",
                    "ok")
| KEEP scope, workflow, state, last_success, last_status, minutes_since_success, runs, failed, rules_seen, pairs,
       capped_spaces
| SORT scope, workflow""",

    "rules_by_space": """FROM rulescope-catalog-rules
| STATS rules = COUNT(*),
        enabled = SUM(CASE(rulescope.rule.enabled, 1, 0)),
        prebuilt = SUM(CASE(rulescope.rule.prebuilt, 1, 0)),
        customized = SUM(CASE(rulescope.rule.source.is_customized, 1, 0)),
        building_block = SUM(CASE(rulescope.rule.building_block, 1, 0))
     BY space = rulescope.space.id
| EVAL disabled = rules - enabled, custom = rules - prebuilt
| KEEP space, rules, enabled, disabled, prebuilt, customized, custom, building_block
| SORT rules DESC""",

    "data_source": """FROM rulescope-catalog-rules
| WHERE rulescope.rule.enabled
| EVAL source = CASE(rulescope.rule.index_patterns_csv IS NOT NULL, "Index patterns",
                     rulescope.rule.data_view_id IS NOT NULL, "Data view (not resolved yet)",
                     rulescope.rule.index_patterns_remote IS NOT NULL, "Remote clusters only",
                     rulescope.rule.type == "machine_learning", "Machine learning job",
                     rulescope.rule.type == "esql", "ES|QL query",
                     "No index patterns")
| STATS rules = COUNT(*) BY source
| SORT rules DESC""",

    "no_data": """FROM rulescope-catalog-rules
| WHERE rulescope.rule.enabled AND rulescope.rule.index_patterns_csv IS NOT NULL
// Renamed first: in LOOKUP JOIN a lookup field replaces a column of the same name,
// and is null for a rule with no pairs.
| EVAL space = rulescope.space.id, rule = rulescope.rule.name, patterns = rulescope.rule.index_patterns_csv
| LOOKUP JOIN rulescope-catalog-rule-streams ON rulescope.space.id, rulescope.rule.id
| STATS streams = COUNT(data_stream.name) BY space, rule, patterns
| WHERE streams == 0
| KEEP rule, space, patterns
| SORT space, rule
| LIMIT 500""",

    "last_run_status": """FROM rulescope-catalog-rules
| WHERE rulescope.rule.enabled
| EVAL status = COALESCE(rulescope.rule.last_run.status, "never run")
| STATS rules = COUNT(*) BY status
| SORT rules DESC""",

    "failing_rules": """FROM rulescope-catalog-rules
| WHERE rulescope.rule.enabled AND rulescope.rule.last_run.status IN ("failed", "partial failure")
| KEEP rulescope.rule.name, rulescope.space.id, rulescope.rule.last_run.status, rulescope.rule.last_run.date,
       rulescope.rule.last_run.message
| RENAME rulescope.rule.name AS rule, rulescope.space.id AS space, rulescope.rule.last_run.status AS status,
         rulescope.rule.last_run.date AS last_run, rulescope.rule.last_run.message AS message
| SORT last_run DESC
| LIMIT 200""",

    "rules_over_time": """FROM rulescope-catalog-rule-history
| STATS enabled = SUM(CASE(rulescope.rule.enabled, 1, 0)) BY day = BUCKET(@timestamp, 1 day), space = rulescope.space.id""",

    "recent_runs": """FROM rulescope-catalog-syncs
| WHERE kind IN ("sync", "snapshot", "rule_cap")
| EVAL note = CASE(kind == "rule_cap", CONCAT("More than 10,000 rules in space ", rulescope.space.id,
                                              "; only the first 10,000 are in the catalog"),
                   status == "failed", error,
                   kind == "snapshot", CONCAT(TO_STRING(rules_copied), " rules copied for ", day),
                   null)
| KEEP @timestamp, rulescope.scope, kind, status, rules_seen, pairs_written, note
| RENAME @timestamp AS time, rulescope.scope AS scope, kind AS workflow, status AS run_status,
         pairs_written AS pairs
| SORT time DESC
| LIMIT 50""",
}

LABELS = {
    "scope": "Scope", "workflow": "Workflow", "state": "State", "last_success": "Last success",
    "last_status": "Last status", "minutes_since_success": "Minutes since success", "runs": "Runs",
    "failed": "Failed", "rules_seen": "Rules", "pairs": "Rule/data stream pairs",
    "capped_spaces": "Spaces over 10,000 rules", "space": "Space", "rules": "Rules", "enabled": "Enabled",
    "disabled": "Disabled", "prebuilt": "Prebuilt", "customized": "Prebuilt, customized", "custom": "Custom",
    "building_block": "Building block", "source": "Data source", "rule": "Rule", "patterns": "Index patterns",
    "status": "Last run status", "last_run": "Last run", "message": "Message", "day": "Day",
    "time": "Time", "note": "Note", "run_status": "Status",
}

P = Panels(LABELS)


def build(version: str) -> dict:
    panels = [
        markdown(INTRO, g(0, 0, 48, 4)),
        P.table("Catalog health", Q["health"], ["scope", "workflow"],
                ["state", "last_success", "minutes_since_success", "last_status", "runs", "failed", "rules_seen",
                 "pairs", "capped_spaces"], g(0, 4, 48, 7),
                "The last sync and snapshot per scope. Modules don't use a catalog whose last successful sync is "
                "more than 2 hours old.", widths={"state": 260, "last_success": 220}),
        P.table("Rules by space", Q["rules_by_space"], ["space"],
                ["rules", "enabled", "disabled", "prebuilt", "customized", "custom", "building_block"],
                g(0, 11, 32, 10)),
        P.table("Enabled rules by data source", Q["data_source"], ["source"], ["rules"], g(32, 11, 16, 10),
                "Only rules with index patterns are matched to data streams. Data views, remote cluster patterns, "
                "machine learning jobs and ES|QL queries aren't resolved in this version."),
        P.table("Enabled rules that match no data stream", Q["no_data"], ["rule", "space"], ["patterns"],
                g(0, 21, 32, 14),
                "Enabled rules whose index patterns match no data stream the catalog can see. They can't alert. "
                "Patterns that only match plain indices or aliases also show here.",
                widths={"rule": 360, "patterns": 360}),
        P.table("Enabled rules by last run", Q["last_run_status"], ["status"], ["rules"], g(32, 21, 16, 14),
                "From each rule's last execution, as of the last sync."),
        P.table("Failing rules", Q["failing_rules"], ["rule", "space"], ["status", "last_run", "message"],
                g(0, 35, 48, 12), widths={"rule": 320, "message": 600}),
        P.line("Enabled rules per day", Q["rules_over_time"], "day", "enabled", "space", g(0, 47, 48, 12),
               "From the daily snapshot."),
        P.table("Recent catalog runs", Q["recent_runs"], ["time", "scope", "workflow"],
                ["run_status", "rules_seen", "pairs", "note"], g(0, 59, 48, 12), widths={"time": 220, "note": 600}),
    ]
    return {
        "title": "Rule Catalog",
        "description": f"Every detection rule, its data streams and daily history. Managed by rulescope (pack v{version}).",
        "time_range": {"from": "now-7d", "to": "now"},
        "panels": panels,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="run every query before writing")
    ap.add_argument("--target-file", default="targets/docker.env")
    args = ap.parse_args()
    if args.check and check_queries(Q, args.target_file, untimed=("rulescope-catalog-rules",)):
        return 1
    version = (ROOT / "VERSION").read_text().strip()
    OUT.write_text(json.dumps(build(version), indent=2) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
