# Field reference: lag module

Every `rulescope-lag-*` data stream is built from two component templates, both with `dynamic: false`:
- [rulescope-common](../../catalog/elasticsearch/component-templates/rulescope-common.json): the identity fields on every rulescope document, from the catalog module.
- [rulescope-lag](../elasticsearch/component-templates/rulescope-lag.json): the lag module's fields.

Fields not listed here are kept in `_source` but not indexed.

The lag module reads rules and their data streams from the Rule Catalog: `rulescope-catalog-rules` and the `rulescope-catalog-rule-streams` lookup index. Their fields are in the [catalog's field table](../../../rulescope-design.md#rule-document-fields). `python3 -m rulescope lint` fails if the lookup index maps a name that the miss-rate query creates before its join, because a lookup field replaces a query column with the same name.

**Naming:**
- rulescope fields are under `rulescope.`.
- ECS fields keep their ECS names (`@timestamp`, `data_stream.*`, `host.name`).
- Measurements are flat.

## Indices

| Index | Type | One document per | Retention |
| --- | --- | --- | --- |
| `rulescope-lag-results` | Data stream | Rule, data stream and hour | 90 days |
| `rulescope-lag-stream-stats` | Data stream | Data stream and hour | 90 days |
| `rulescope-lag-host-lag` | Data stream | Data stream, host and hour (slow or skewed hosts only) | 30 days |
| `rulescope-lag-runs` | Data stream | Workflow run | 90 days |

## Shared fields

| Field | Type | Meaning |
| --- | --- | --- |
| `@timestamp` | date | Results and stats: start of the hour measured. Runs: when the run finished. |
| `rulescope.run.id` | keyword | Workflow execution ID |
| `rulescope.window.start` | date | Start of the clock hour measured (results, stream stats, host lag) |
| `rulescope.scope` | keyword | Results and runs: the copy that wrote them, `global` or `space:<id>`. Stream stats and host lag have none. |
| `rulescope.space.id` | keyword | Kibana space of the rule (results) or of the per-space copy (runs) |
| `data_stream.dataset`, `data_stream.namespace` | keyword | The measured data stream; the join keys |

`rulescope.cluster.name` and `rulescope.tenant.id` are mapped for cross-cluster support and aren't set yet.

## `rulescope-lag-results`

| Field | Type | Meaning |
| --- | --- | --- |
| `rulescope.rule.id`, `name`, `interval_s`, `lookback_s` | | The rule, as the catalog had it |
| `events` | long | Events that arrived in the hour (by `event.ingested`) in this data stream |
| `est_missed` | double | Expected number of those events outside the rule's window |
| `miss_rate_pct` | double | `est_missed / events × 100` |
| `lag_p50_s`, `lag_p95_s`, `lag_p99_s` | double | Lag percentiles (approximate), seconds |
| `lag_max_s` | long | Largest lag, seconds |
| `negative_lag_events` | long | Events stamped more than 60 seconds after they arrived |
| `recommended_lookback_s` | long | Interval + p99 lag rounded up to a minute, at least interval + 60 |

Rules that search on `event.ingested` have no results rows, because lag can't make them miss events.

## `rulescope-lag-stream-stats`

| Field | Type | Meaning |
| --- | --- | --- |
| `events` | long | Measured events (arrived in the hour) plus unmeasured events (stamped in the hour) |
| `unmeasured_events` | long | Events with no `event.ingested` |
| `lag_p50_s` … `lag_max_s`, `negative_lag_events` | | As in results, for the whole data stream |

The two kinds of event are placed in the hour differently. Measured events count by when they arrived (`event.ingested`). Unmeasured events have no arrival time, so they count by when they happened (`@timestamp`).

## `rulescope-lag-host-lag`

The fields are `host.name`, `events`, `lag_p95_s`, `lag_max_s` and `negative_lag_events`. A host and data stream gets a row only if its p95 lag is over 60 seconds, or it has events stamped in the future.

## `rulescope-lag-runs`

| Field | Type | Meaning |
| --- | --- | --- |
| `status` | keyword | `success`; `no_data` (the run worked but had nothing to measure: no logs in `logs-*` yet, or none with `event.ingested`); `catalog_stale` (the Rule Catalog's last successful sync was more than 2 hours old, or there was none, so no results were written); or `failed` |
| `reason` | keyword | For `no_data` and `catalog_stale` runs, why, in plain words. `catalog_stale` reasons include the catalog's last successful sync time. |
| `error` | match_only_text | Error message for failed runs |
| `wrote_shared_stats` | boolean | Per-space copies: whether this run wrote stream stats and host lag |
| `spaces_with_rules` | long | Global runs: spaces with enabled rules in the catalog |
| `rules_seen`, `rules_supported` | long | Enabled rules in the catalog for this scope, and how many the profiler can estimate |
| `streams_matched` | long | Rule and data stream pairs for rules with a parsed schedule that search `@timestamp` |
| `result_rows` | long | Rows the miss-rate query returned |

`catalog_stale` and `failed` runs don't have the rule counts.
