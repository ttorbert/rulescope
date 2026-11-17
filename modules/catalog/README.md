# Rule Catalog

The catalog keeps a copy of every Elastic Security detection rule, enabled or not, in every space it covers. It also records which data streams each enabled rule searches, and keeps a daily history. Report modules read the catalog instead of each calling the rules API. It runs entirely inside Elastic: two workflows, three ingest pipelines, four indices, a dashboard and an alert.

The [lag module](../lag/README.md) reads its rules and their data streams from the catalog, so installing it installs the catalog too. The installer refuses to remove the catalog while the lag module is installed, unless you add `--force`.

## Install

```bash
python3 -m rulescope install --target-file targets/prod.env --modules catalog
python3 -m rulescope install --target-file targets/prod.env --modules catalog --workflow per-space --space soc
```

- **Global** (the default) installs one copy in the Default space that reads rules in every space. Its role has `spaces: ["*"]`.
- **Per space** installs one copy per space, each with its own role (`rulescope-catalog-<space>`), dashboard and alert. Each copy only writes and cleans up its own documents.
- **One scope per cluster.** The installer refuses a global copy while per-space copies exist, and the other way round, unless you add `--force`.
- **Uninstall:** `uninstall --modules catalog [--space <id>] [--delete-data]`. Shared templates, pipelines and indices stay while another copy is installed. `--delete-data` on one space's copy deletes only that scope's documents.

### Workflow identity

Scheduled runs use the permissions of the account that last saved the workflow. Give the catalog its own user:

1. Create a user with only the `rulescope-catalog` role (or `rulescope-catalog-<space>`). The installer creates the role.
2. As that user, create an API key: `POST /_security/api_key` with `{"name": "rulescope-catalog-owner"}`.
3. Add `RULESCOPE_CATALOG_WORKFLOW_API_KEY=<encoded key>` to your target file and run `install` again.

`RULESCOPE_WORKFLOW_API_KEY` sets one owner for every module, a shortcut for labs and small installs. In the lab, `scripts/lab/lab-owner.py --module catalog` creates the user and key.

The role is enough for a full sync, confirmed on 9.5.6 with a user holding only this role. It has `view_index_metadata` on `logs-*` for pattern resolution and no `read`, because the catalog never reads events.

## What runs

| Workflow | Runs | Does |
| --- | --- | --- |
| `rulescope-catalog-sync` | Hourly | Fetches every rule with the find API (one page of up to 10,000 per space), saves them, resolves each enabled rule's local index patterns to data streams, removes documents from earlier syncs, and records the sync |
| `rulescope-catalog-snapshot` | Hourly, copies once a day | Copies the rules into the history data stream with that day's date, once a sync has succeeded in the last 2 hours |

Per-space copies add `-space-<id>` to each name. Both workflows are generated from templates in [workflows/](workflows/). `python3 -m rulescope render-workflow --modules catalog [--workflow per-space --space <id>]` writes them to `build/` for a manual import.

**How a rule gets into the catalog.** The sync sends each rule to the `rulescope-catalog-rules-parse` pipeline, reduced to the fields the catalog uses (Liquid's `pick` filter). The pipeline extracts the catalog fields and drops the rest. **Query text is never stored**; `rulescope.rule.query_hash` changes when the query does. Notes, setup guides and descriptions never leave Kibana.

## Indices

| Index | Type | One document per |
| --- | --- | --- |
| `rulescope-catalog-rules` | Index | Rule, ID `<space>:<rule id>`. Current state. |
| `rulescope-catalog-rule-streams` | Lookup index | Enabled rule and data stream, ID `<space>:<rule id>:<data stream>` |
| `rulescope-catalog-rule-history` | Data stream, 400 days | Rule per day, ID `<date>:<space>:<rule id>` |
| `rulescope-catalog-syncs` | Data stream, 90 days | Sync or snapshot run (`kind: sync`, `snapshot`), or a rule cap notice (`kind: rule_cap`) |

Every document carries `rulescope.scope` (`global` or `space:<id>`), `rulescope.space.id` and `rulescope.sync.id`. The rule fields are listed in the design under [Rule document fields](../../rulescope-design.md#rule-document-fields). They include schedule (`interval_s`, `lookback_s`, `additional_lookback_s`, `schedule_parsed`), index patterns (local, remote and as written), the effective `timestamp_field`, ATT&CK tactics, techniques and sub-techniques, related integrations, required fields, prebuilt or custom, and the last run's status, message and search time.

The catalog records **facts**, not whether a module supports a rule. Each module decides from those facts in its own queries.

## Dashboard and alert

The **Rule Catalog** dashboard (`rulescope-catalog-health`) shows:
- Catalog health: the last sync and snapshot per scope, and whether the catalog is stale.
- Rules by space: enabled, disabled, prebuilt, customized and building block rules.
- Enabled rules by data source.
- Enabled rules that match no data stream.
- Last run status and failing rules.
- Enabled rules per day, from the history.
- Recent runs.

Its builder is [dashboards/build.py](dashboards/build.py) (`make dashboard` checks every query against the lab first).

The **Rule Catalog: no successful sync in 3 hours** alert (`rulescope-catalog-stale`) fires for each scope whose syncs have stopped succeeding. Report modules stop using a catalog more than 2 hours old: the lag module records its runs as `catalog_stale`, so its results have stopped too.

## Limits

- **10,000 rules per space.** One page of the find API. A space with more gets a `rule_cap` notice in the dashboard's recent runs. Paging is a later item.
- **10,000 enabled rules with index patterns per scope** are resolved to data streams (one search page).
- **Liquid limits.** Each space's rules are written in one bulk request rendered by Liquid. The sync raises Liquid's render time and memory limits to their maximums (2 seconds, 60 million). It syncs 2,304 rules in about 6 seconds in the lab. Very large spaces may need paging before they reach those limits.
- **Not resolved yet:** data views, remote cluster patterns (kept in `index_patterns_remote` for cross-cluster support), machine learning jobs, ES|QL queries. Patterns that match only plain indices or aliases produce no pairs.
- **Snapshot dates are UTC**, from Elasticsearch's clock. A rerun on the same day skips rules already copied, unless the data stream rolled over in between.
