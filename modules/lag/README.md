# Ingest Lag Profiler

The lag module estimates how many events each enabled detection rule misses because logs arrive late. Every hour it measures the lag between when events happened (`@timestamp`) and when they arrived (`event.ingested`), and estimates each rule's miss rate from its schedule and look-back. It runs entirely inside Elastic: one workflow, four data streams, a dashboard and two alerts.

It reads rules from the [Rule Catalog](../catalog/README.md), so the catalog is installed with it. The lag module never calls the rules API.

## Install

```bash
python3 -m rulescope install --target-file targets/prod.env
python3 -m rulescope install --target-file targets/prod.env --workflow per-space --space soc --shared-stats
```

`install` with no `--modules` installs the catalog and the lag module. `--modules lag` does the same, because the lag module requires the catalog.

- **Global** (the default): one copy in the Default space covers every space the catalog covers. Its role has `spaces: ["*"]`.
- **Per space:** one copy per space. Each copy has its own role (`rulescope-lag-<space>`), dashboard (*Ingest Lag Profiler (<space>)*) and alert rules, covering only that space's rules.
- **One scope per cluster, shared with the catalog.** Each copy reads the catalog of its own scope, so the catalog and the lag module are installed in the same scope. The installer refuses a global copy while per-space copies exist, and the other way round, unless you add `--force`.
- **Shared stats.** Stream stats and host lag come from raw logs, not rules, so they're the same for every space. In per-space mode exactly one copy writes them: install that one with `--shared-stats`. If none does, the stream and host panels go empty. The installer refuses a second `--shared-stats` copy unless you add `--force`.
- **Uninstall:** `uninstall --modules lag [--space <id>] [--delete-data]` removes the lag module and leaves the catalog. `uninstall` with no `--modules` removes both. The installer refuses to remove the catalog while a lag copy needs it, unless you add `--force`.

### Which scope

**Use global unless you can't.** It's simpler to run, picks up new spaces on its own (once the catalog has synced them), and scans `logs-*` the fewest times: 3 times an hour.

**Use per space when one role that covers every space isn't acceptable.** This is typical for a managed security provider, or any setup where each space belongs to a different team or customer. Each copy scans `logs-*` once an hour, plus twice in the copy that writes shared stats. A failed run affects only its own space.

### Workflow identity

Scheduled runs use the permissions of the account that last saved the workflow. Give the lag module its own user:

1. Create a user with only the `rulescope-lag` role (or `rulescope-lag-<space>`). The installer creates the role.
2. As that user, create an API key: `POST /_security/api_key` with `{"name": "rulescope-lag-owner"}`.
3. Add `RULESCOPE_LAG_WORKFLOW_API_KEY=<encoded key>` to your target file and run `install` again.

`LAGPROF_WORKFLOW_API_KEY`, from before the rename, still works for the lag module. `RULESCOPE_WORKFLOW_API_KEY` sets one owner for every module. In the lab, `scripts/lab/lab-owner.py --module lag` creates the user and key, and `make lab-owner` does both modules.

The role reads `logs-*` and `rulescope-catalog-*`, and writes `rulescope-lag-*`. It has no Security feature privilege, because the lag module never reads rules from Kibana. A full run as a user holding only this role was confirmed on 9.5.6.

## What runs

`rulescope-lag-hourly` (per space: `rulescope-lag-hourly-space-<id>`) runs hourly:

1. **Catalog freshness.** It looks up the latest successful catalog sync for its scope. If that's more than 2 hours old, or there's none, it records the run as `catalog_stale` with the reason and writes no results.
2. **Counts** the catalog's enabled rules, the rules it can estimate, and their data streams, for the run record.
3. **Data check.** With no logs, or no logs with `event.ingested`, it records `no_data` with the reason.
4. **Queries.** The miss-rate query joins hourly lag counts to the catalog's `rulescope-catalog-rule-streams` lookup index. Stream stats and host lag come from raw logs. Each writes one bulk request.
5. **Records** the run in `rulescope-lag-runs`.

A rule can be estimated when the catalog shows it has local index patterns and a parsed schedule, and it isn't a machine learning or ES|QL rule. Of those, only rules that search `@timestamp` get results. Rules on `event.ingested` can't miss events because of lag.

**On a new install the first run usually records `catalog_stale`.** Kibana starts a newly imported scheduled workflow straight away, before the catalog's first sync has finished. The next hourly run uses the catalog.

The catalog syncs hourly, so a rule whose look-back changed in the last hour is estimated with its old settings once.

The workflow is written once as [workflows/hourly.yaml.tmpl](workflows/hourly.yaml.tmpl) and rendered for each scope. `python3 -m rulescope render-workflow --modules lag [--workflow per-space --space <id> [--shared-stats]]` writes it, with the catalog's workflows, to `build/` for a manual import. The three queries also live in [esql/](esql/) as readable copies; `lint` fails if they drift from the workflow.

## Indices

| Index | Type | One document per | Retention |
| --- | --- | --- | --- |
| `rulescope-lag-results` | Data stream | Rule, data stream and hour | 90 days |
| `rulescope-lag-stream-stats` | Data stream | Data stream and hour | 90 days |
| `rulescope-lag-host-lag` | Data stream | Data stream, host and hour (slow or skewed hosts only) | 30 days |
| `rulescope-lag-runs` | Data stream | Workflow run | 90 days |

Results and runs carry `rulescope.scope` and `rulescope.space.id`. Stream stats and host lag don't, because raw logs aren't divided by space. Every field is listed in the [field reference](docs/fields.md).

## Dashboard and alerts

The **Ingest Lag Profiler** dashboard (`rulescope-lag-overview`) shows rules at risk with recommended look-backs, miss rate and lag over time, slow hosts, clock problems, measurement gaps, rule coverage, the rules it can't estimate and why, and profiler health. Coverage panels read the catalog's rules. Its builder is [dashboards/build.py](dashboards/build.py); `make dashboard` checks every query against the lab first.

Alerts:
- **Rule miss rate above 1%** (`rulescope-lag-miss-rate`).
- **No completed run in 3 hours** (`rulescope-lag-stale-profiler`), per workflow copy. `success` and `no_data` count as completed; `catalog_stale` and `failed` don't, because no results were written. The alert shows the last status. When the catalog is the cause, the catalog's own alert fires too.

## Docs

- [Why use it](docs/value.md): business and operational value, costs and limits.
- [Validation results](docs/validation-results.md): how the estimates were tested against real rules.
- [Miss model](docs/miss-model.md): the formula, its simulation check, and what it leaves out.
- [Field reference](docs/fields.md).

## Moving a lab from lagprof

Before the restructure the module wrote `lagprof-*` indices and installed `lagprof-*` resources. [scripts/migrate_lagprof.py](../../scripts/migrate_lagprof.py) copies the history into `rulescope-lag-*`, renaming the fields. With `--remove-old` it also removes the old workflows, dashboards, alert rules, roles, templates and pipelines, and with `--delete-old-data` the old data. Nothing outside the lab needs it: there was no release before the rename.
