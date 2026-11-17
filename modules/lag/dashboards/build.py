#!/usr/bin/env python3
"""Build modules/lag/dashboards/rulescope-lag-overview.json, the Ingest Lag Profiler dashboard.

The JSON is a Kibana Dashboards API body (PUT /api/dashboards/rulescope-lag-overview, 9.5+).
Every panel reads rulescope indices with ES|QL, never raw logs: the lag module's
results and stats, and the Rule Catalog's rules for coverage. A per-space install
filters each panel to its own space (see resources.scope_query).

    build.py                                       write the JSON
    build.py --check --target-file targets/docker.env   also run every query first

Panel queries see the dashboard's time range on @timestamp. "Latest hour" panels pick
the newest window inside that range with INLINE STATS.
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

OUT = HERE / "rulescope-lag-overview.json"

INTRO = """**Ingest Lag Profiler.** Estimated share of events each detection rule misses because logs arrive late. \
Results are **estimates**: `est_missed` counts events in the data streams a rule searches that fall outside its \
window, not missed alerts. Rules that search on `event.ingested` can't miss events because of lag; see *Rule coverage*. \
For rules that run every minute, the estimate can be a few points low when rule start times vary on a busy Kibana. \
Updated hourly by the rulescope-lag-hourly workflow, from the rules in the Rule Catalog."""

# Which enabled rules the profiler can estimate, from the catalog's facts. The same
# conditions as the workflow's miss-rate query, which only measures rules on @timestamp.
CANT_ESTIMATE = """CASE(rulescope.rule.type == "machine_learning", "Machine learning rule",
                    rulescope.rule.type == "esql", "ES|QL rule",
                    rulescope.rule.index_patterns_csv IS NULL AND rulescope.rule.data_view_id IS NOT NULL,
                      "Uses a data view (not resolved yet)",
                    rulescope.rule.index_patterns_csv IS NULL AND rulescope.rule.index_patterns_remote IS NOT NULL,
                      "Only remote cluster index patterns",
                    rulescope.rule.index_patterns_csv IS NULL, "No index patterns",
                    rulescope.rule.schedule_parsed == false, "Schedule couldn't be parsed",
                    rulescope.rule.timestamp_field NOT IN ("@timestamp", "event.ingested"),
                      CONCAT("Searches ", rulescope.rule.timestamp_field),
                    null)"""

Q = {
    "rules_at_risk": """FROM rulescope-lag-results
| INLINE STATS latest = MAX(rulescope.window.start)
| EVAL in_latest = rulescope.window.start == latest
| STATS events = SUM(CASE(in_latest, events, 0)),
        missed = SUM(CASE(in_latest, est_missed, 0.0)),
        recommended_s = MAX(recommended_lookback_s),
        lookback_s = MAX(rulescope.rule.lookback_s),
        interval_s = MAX(rulescope.rule.interval_s)
     BY space = rulescope.space.id, rule = rulescope.rule.name
| EVAL miss_rate_pct = ROUND(missed / events * 100, 2),
       est_missed = TO_LONG(ROUND(missed)),
       interval_min = interval_s / 60.0,
       lookback_min = lookback_s / 60.0,
       recommended_min = recommended_s / 60.0,
       recommended_additional_min = (recommended_s - interval_s) / 60.0
| WHERE miss_rate_pct > 0.1
| KEEP rule, space, est_missed, events, miss_rate_pct, interval_min, lookback_min, recommended_min,
       recommended_additional_min
| SORT est_missed DESC, miss_rate_pct DESC
| LIMIT 200""",

    "miss_rate_over_time": """FROM rulescope-lag-results
| STATS missed = SUM(est_missed), events = SUM(events)
     BY hour = BUCKET(@timestamp, 1 hour), rule = rulescope.rule.name
| EVAL miss_rate_pct = ROUND(missed / events * 100, 2)
| KEEP hour, rule, miss_rate_pct""",

    "lag_by_stream": """FROM rulescope-lag-stream-stats
| WHERE lag_p95_s IS NOT NULL
| STATS p95_lag_s = MAX(lag_p95_s)
     BY hour = BUCKET(@timestamp, 1 hour), stream = data_stream.dataset""",

    "slowest_hosts": """FROM rulescope-lag-host-lag
| INLINE STATS latest = MAX(rulescope.window.start)
| WHERE rulescope.window.start == latest AND lag_p95_s > 60
| STATS p95_lag_s = MAX(lag_p95_s), max_lag_s = MAX(lag_max_s), events = SUM(events)
     BY host = host.name, stream = data_stream.dataset
| SORT p95_lag_s DESC
| LIMIT 100""",

    "clock_problems": """FROM rulescope-lag-host-lag
| WHERE negative_lag_events > 0
| STATS events_in_future = SUM(negative_lag_events), last_seen = MAX(@timestamp)
     BY host = host.name, stream = data_stream.dataset
| SORT events_in_future DESC
| LIMIT 100""",

    "measurement_gaps": """FROM rulescope-lag-stream-stats
| STATS events = SUM(events), unmeasured = SUM(unmeasured_events) BY stream = data_stream.dataset
| EVAL unmeasured_pct = ROUND(unmeasured * 100.0 / events, 1)
| WHERE unmeasured > 0
| SORT unmeasured DESC
| LIMIT 100""",

    "rule_coverage": """FROM rulescope-catalog-rules
| WHERE rulescope.rule.enabled
| EVAL reason = """ + CANT_ESTIMATE + """
| EVAL coverage = CASE(reason IS NOT NULL, "Can't estimate",
                       rulescope.rule.timestamp_field == "event.ingested", "Unaffected (event.ingested)",
                       "Measured (@timestamp)")
| STATS rules = COUNT(*) BY coverage
| SORT rules DESC""",

    "cant_estimate": """FROM rulescope-catalog-rules
| WHERE rulescope.rule.enabled
| EVAL reason = """ + CANT_ESTIMATE + """
| WHERE reason IS NOT NULL
| STATS rules = COUNT(*) BY reason
| SORT rules DESC""",

    "health": """FROM rulescope-lag-runs
| EVAL workflow = COALESCE(rulescope.space.id, "global")
| INLINE STATS latest = MAX(@timestamp) BY workflow
| EVAL is_last = @timestamp == latest
| STATS last_run = MAX(@timestamp), runs = COUNT(*),
        no_data = SUM(CASE(status == "no_data", 1, 0)),
        catalog_stale = SUM(CASE(status == "catalog_stale", 1, 0)),
        failed = SUM(CASE(status == "failed", 1, 0)),
        last_status = MAX(CASE(is_last, status, null)),
        last_note = MAX(CASE(is_last, reason, null)),
        last_rules_seen = MAX(rules_seen)
     BY workflow
| EVAL last_note = CASE(last_status == "failed", "See the error field in rulescope-lag-runs", last_note)
| KEEP workflow, last_run, last_status, last_note, runs, no_data, catalog_stale, failed, last_rules_seen""",
}


LABELS = {
    "rule": "Rule", "space": "Space", "miss_rate_pct": "Est. miss rate (%)", "interval_min": "Runs every (min)",
    "lookback_min": "Total look-back (min)", "recommended_min": "Recommended total look-back (min)",
    "recommended_additional_min": "Set additional look-back to (min)", "est_missed": "Est. missed events",
    "host": "Host",
    "stream": "Data stream", "p95_lag_s": "p95 lag (s)", "max_lag_s": "Max lag (s)", "events": "Events",
    "events_in_future": "Events stamped in the future", "last_seen": "Last seen", "unmeasured": "No event.ingested",
    "unmeasured_pct": "No event.ingested (%)", "coverage": "Coverage", "rules": "Rules", "reason": "Reason",
    "workflow": "Workflow", "last_run": "Last run", "runs": "Runs", "failed": "Failed",
    "last_status": "Last status", "last_note": "Note", "no_data": "No data",
    "last_rules_seen": "Rules seen", "hour": "Hour", "catalog_stale": "Catalog stale",
}


P = Panels(LABELS)


def build(version: str) -> dict:
    panels = [
        markdown(INTRO, g(0, 0, 48, 4)),
        P.table("Rules at risk (latest hour)", Q["rules_at_risk"],
                ["rule", "space"],
                ["est_missed", "events", "miss_rate_pct", "interval_min", "lookback_min", "recommended_min",
               "recommended_additional_min"], g(0, 4, 48, 14),
                "Rules with an estimated miss rate above 0.1% in the newest hour, most missed events first. "
                "Total look-back is the rule's interval plus its additional look-back. The recommendation keeps the "
                "estimate under 1%, from the highest hourly p99 lag in the time range. To apply it, set the rule's "
                "Additional look-back time in Kibana to the last column.",
                widths={"rule": 320}),
        P.line("Miss rate over time (%)", Q["miss_rate_over_time"], "hour", "miss_rate_pct", "rule", g(0, 18, 24, 14)),
        P.line("95th percentile lag by data stream (seconds)", Q["lag_by_stream"], "hour", "p95_lag_s", "stream",
               g(24, 18, 24, 14)),
        P.table("Slowest hosts (latest hour)", Q["slowest_hosts"], ["host", "stream"],
                ["p95_lag_s", "max_lag_s", "events"], g(0, 32, 24, 12)),
        P.table("Clock problems: events stamped in the future", Q["clock_problems"], ["host", "stream"],
                ["events_in_future", "last_seen"], g(24, 32, 24, 12),
                "Events stamped more than a minute after they arrived. Usually a source system's clock is wrong."),
        P.table("Measurement gaps: events with no event.ingested", Q["measurement_gaps"], ["stream"],
                ["events", "unmeasured", "unmeasured_pct"], g(0, 44, 24, 12),
                "Lag can't be measured for these events. Rules on event.ingested fall back to @timestamp for them."),
        P.table("Rule coverage", Q["rule_coverage"], ["coverage"], ["rules"], g(24, 44, 12, 12),
                "Enabled rules in the Rule Catalog, as of its last sync."),
        P.table("Rules the profiler can't estimate", Q["cant_estimate"], ["reason"], ["rules"], g(36, 44, 12, 12),
                "Enabled rules without a miss estimate, and why. From the Rule Catalog's facts."),
        P.table("Profiler health", Q["health"], ["workflow"],
                ["last_run", "last_status", "last_note", "runs", "no_data", "catalog_stale", "failed",
               "last_rules_seen"],
                g(0, 56, 48, 8),
                "No data means the run worked but had nothing to measure: no logs in logs-* yet, or none with "
                "event.ingested. Catalog stale means the Rule Catalog's last successful sync was more than 2 hours "
                "old, so the run wrote no results. Note says which.",
                widths={"last_run": 220, "last_note": 520}),
    ]
    return {
        "title": "Ingest Lag Profiler",
        "description": f"Estimated detection rule miss rates from ingest lag. Managed by rulescope (pack v{version}).",
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
