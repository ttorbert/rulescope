# rulescope: Architecture and Rule Catalog Design

Nov 15, 2026 · proposal · status: draft for review, no code changed yet

rulescope turns the Ingest Lag Profiler into one of several modules that share a **rule catalog**. The catalog module syncs every detection rule, in every space, into ordinary indices. Report modules read the catalog: the Ingest Lag Profiler first, then coverage and schedule reports. Everything still runs inside Elastic with Workflows, ES|QL, dashboards and alerts.

This document covers the module structure, the catalog schema and sync, the two space models, cross-cluster planning, the repository layout and the migration from today's code. It supersedes the architecture parts of [ingest-lag-profiler-design.md](ingest-lag-profiler-design.md) once adopted. The miss model, validation and lag queries in that document stay as they are.

## Decisions recorded

| Decision | Choice | Date |
| --- | --- | --- |
| Project structure | One repository, a shared catalog module plus separate report modules | Nov 15, 2026 |
| Name | **rulescope**. The lag module keeps its product name, *Ingest Lag Profiler*. | Nov 15, 2026 |
| First public release | After the restructure: catalog plus lag | Nov 15, 2026 |
| Space models | Every module supports per-space copies **or** one global view across spaces | Nov 15, 2026 |
| Cross-cluster search | Designed now, built later, for managed security providers that want one view across customer clusters | Nov 15, 2026 |
| License | Enterprise or trial, because of Workflows (unchanged) | Nov 13, 2026 |

## Goals and non-goals

**Goals**

1. One catalog of every detection rule, enabled or not, with its space, schedule, ATT&CK mappings, source and last run.
2. Report modules that are installed, scheduled, permissioned and failed independently of each other.
3. Both space models for every module, chosen at install time.
4. A schema that already carries cluster and tenant identity, so cross-cluster views need no schema change later.
5. No regression in what the lag profiler already proved: the miss estimates, overlap-safe cleanup and space handling.

**Non-goals for the first release**

- Building cross-cluster views. Only the schema and naming support them.
- Changing rules. Modules report; people decide.
- Storing rule query text by default (see [Privacy](#privacy)).
- A free edition without Workflows.

## Modules

| Module | Product name | What it does | Runs | Reads | Writes | Needs `logs-*` read |
| --- | --- | --- | --- | --- | --- | --- |
| `catalog` | Rule Catalog | Syncs every rule, resolves index patterns to data streams, keeps daily history | Hourly sync, daily snapshot | Kibana rules and spaces APIs, `_resolve/index` | `rulescope-catalog-*` | No (index metadata only) |
| `lag` | Ingest Lag Profiler | Estimates events each rule misses because logs arrive late | Hourly | Catalog, `logs-*` | `rulescope-lag-*` | Yes |
| `coverage` | Coverage Report | Executive view: rules enabled, prebuilt versus custom, ATT&CK tactic and technique coverage, rules with no data | Daily, plus on demand | Catalog, ATT&CK reference | `rulescope-coverage-*` | No |
| `schedule` | Schedule Report | Rule intervals, look-backs, load by minute, last run status and duration | Hourly | Catalog | `rulescope-schedule-*` | No |

`catalog` is required. Each report module declares which catalog fields it needs, so a schema change shows which modules it affects.

**Why `coverage` and `schedule` may need no workflow of their own.** Their first versions could be dashboards and alert rules that query the catalog directly with ES|QL. A workflow is only needed to keep history the catalog doesn't already keep, such as a daily coverage score. Decide per module when it's built.

## Deployment models

### Space scope

Each module is installed with one scope:

| Scope | What runs | Role | Results |
| --- | --- | --- | --- |
| `global` | One copy of each module, in the Default space, covering every space | Reads rules in all spaces (`spaces: ["*"]`) | Every document carries `rulescope.space.id`, so dashboards filter by space |
| `space:<id>` | One copy of each module per space | Each copy's role covers only its own space | Each copy writes only its own space's documents and gets its own dashboard and alerts |

Rules belong to a space, and the catalog records which space each rule is in, whichever scope is used.

**Rules that keep scopes consistent:**

1. A module's scope must sit inside the catalog's scope. A `global` catalog serves any module scope. A `space:soc` catalog only serves modules scoped to `soc`.
2. One scope per module per cluster. The installer refuses a `global` copy while `space:` copies exist, and the other way round, as it does today for the two lag workflow versions.
3. Every document records the scope that wrote it in `rulescope.scope`, so cleanup in one copy never touches another copy's documents.
4. **Shared statistics** (lag per data stream and per host) come from raw logs, which aren't divided by space. In `space:` scope exactly one lag copy writes them, as today's `--shared-stats` does.

### Cluster identity

Every document carries:

| Field | Set from | Example |
| --- | --- | --- |
| `rulescope.cluster.name` | Install setting `RULESCOPE_CLUSTER_NAME`, defaulting to the Elasticsearch cluster name | `acme-prod` |
| `rulescope.tenant.id` | Optional install setting `RULESCOPE_TENANT_ID` | `acme` |

The installer renders these values into a `rulescope-stamp` pipeline, set as `index.default_pipeline` in every rulescope index template. Today only rule, pair and run documents pass through a pipeline; the result bulk writes name none. So a default pipeline is the one place that reaches every document, without each workflow passing the values. The parse pipelines set the same fields, because a request that names its own pipeline skips the default one. Confirm that Serverless accepts `index.default_pipeline` in templates.

### Cross-cluster search (planned, built later)

A managed security provider can work in two ways. The design supports both, and the first release builds neither.

| | Distributed detection | Central detection |
| --- | --- | --- |
| Where rules run | In each customer's own cluster and Kibana | In the provider's Kibana, searching customer data across clusters |
| Where rulescope runs | In every customer cluster, unchanged | In the provider's cluster |
| Central view | Dashboards that query `*:rulescope-*` and the local `rulescope-*` with cross-cluster search, filtered by `rulescope.cluster.name` or `rulescope.tenant.id` | The normal dashboards; rules already live there |
| What's missing today | Cross-cluster versions of the dashboards and alerts | Rules whose index patterns point at remote clusters, such as `acme:logs-*`. Today they're marked unsupported. The catalog must resolve them, and the lag module must measure lag with cross-cluster queries. |

**What the first release must get right so that this works later:**

- The cluster and tenant fields above, on every document.
- The same index names in every cluster, so one cross-cluster pattern covers them all.
- The catalog **keeps remote index patterns** in `rulescope.rule.index_patterns_remote` instead of discarding them.
- Dashboards and alert queries built by `build.py` take their index pattern from one setting, so a cross-cluster variant is a build option, not a rewrite.

**To confirm in the cross-cluster milestone:**

- Cross-cluster search reaches Elasticsearch indices but not a remote Kibana's APIs, so each customer cluster has to sync its own rules. Confirm there's no supported way for a central workflow to call a remote Kibana.
- ES|QL `LOOKUP JOIN` with remote clusters. An earlier search said cross-cluster support became generally available in 9.2, but it hasn't been tested here.
- Whether `_resolve/index` resolves remote cluster patterns.
- How ES|QL alert rules behave on cross-cluster queries when a remote cluster is unavailable.
- The privileges a central role needs on remote clusters.

## Naming

Every index starts with `rulescope-<module>-`, so one pattern, `rulescope-*`, covers the whole pack locally and across clusters. Fields are namespaced under `rulescope.` to avoid clashing with ECS, which has its own top-level `rule.*` fields with different meanings.

The lag module's indices are renamed from `lagprof-*` to `rulescope-lag-*`. Nothing has been released, so there are no users to migrate. The lab and its history get a migration script (see [Migration](#migration-from-the-current-code)).

## Catalog schema

### Indices

| Index | Type | One document per | Lifetime |
| --- | --- | --- | --- |
| `rulescope-catalog-rules` | Standard index | Rule, in its space: ID `<space>:<rule id>` | Current state. Rebuilt each sync; stale rules removed. |
| `rulescope-catalog-rule-history` | Data stream | Rule per day: ID `<date>:<space>:<rule id>` | Daily snapshot of the rules index. Default retention 400 days, so a year can be compared with the one before. Confirm Serverless allows that retention. |
| `rulescope-catalog-rule-streams` | Lookup index | Enabled rule and data stream pair: ID `<space>:<rule id>:<data stream>` | Current state, rebuilt each sync |
| `rulescope-catalog-syncs` | Data stream | Sync run | 90 days |

The rule-to-data-stream pairs move here from the lag module, because coverage needs them too: an enabled rule that matches no data stream is a coverage gap.

**Size.** The lab holds 2,302 rules (confirmed Nov 15, 2026). A year of daily history for one space is about 840,000 small documents, which is modest. Ten spaces with similar rule sets is about 8.4 million.

### Rule document fields

Field names below are under `rulescope.rule.` unless shown otherwise. The source column names the field in the rules find API response. All of these source fields were confirmed in the lab's find response on 9.5.6.

| Field | Type | Source | Notes |
| --- | --- | --- | --- |
| `@timestamp` | date | Ingest time | When this sync saw the rule. Drives stale cleanup. |
| `rulescope.sync.id`, `rulescope.scope`, `rulescope.space.id`, `rulescope.cluster.name`, `rulescope.tenant.id` | keyword | Workflow, pipeline | Identity of the run, scope, space, cluster and tenant |
| `id`, `rule_id` | keyword | `id`, `rule_id` | `id` is unique per space; `rule_id` is shared by copies of a rule across spaces |
| `name` | keyword | `name` | |
| `enabled` | boolean | `enabled` | |
| `type`, `language` | keyword | `type`, `language` | For example `query` and `kuery`, `esql` and `esql` |
| `severity`, `risk_score` | keyword, long | `severity`, `risk_score` | |
| `tags` | keyword | `tags` | Prebuilt rules carry tags such as `Tactic: Collection` and `Data Source: …` |
| `building_block` | boolean | `building_block_type` | True when set |
| `version`, `revision` | long | `version`, `revision` | |
| `source.type` | keyword | `rule_source.type` | `external` for prebuilt rules, `internal` for custom |
| `source.is_customized` | boolean | `rule_source.is_customized` | Prebuilt rules changed locally |
| `prebuilt` | boolean | `immutable` | Kept for readability |
| `author`, `license` | keyword | `author`, `license` | |
| `created_at`, `updated_at` | date | `created_at`, `updated_at` | |
| `created_by`, `updated_by` | keyword | `created_by`, `updated_by` | User names. See [Privacy](#privacy). |
| `interval_raw`, `from_raw` | keyword | `interval`, `from` | |
| `interval_s`, `lookback_s`, `additional_lookback_s` | long | Parsed | `lookback_s` is the whole window; additional is look-back minus interval |
| `schedule_parsed` | boolean | Parsed | False when interval or `from` couldn't be parsed |
| `max_signals` | long | `max_signals` | |
| `index_patterns` | keyword | `index` | As written |
| `index_patterns_local`, `index_patterns_remote` | keyword | Parsed | Remote patterns contain `:` and are kept for cross-cluster support |
| `index_patterns_csv` | keyword | Parsed | Local patterns joined, for `_resolve/index` |
| `data_view_id` | keyword | `data_view_id` | Resolving data views is still a later item |
| `timestamp_override`, `timestamp_override_fallback_disabled` | keyword, boolean | Same names | |
| `timestamp_field` | keyword | Parsed | `timestamp_override`, or `@timestamp` when there is none |
| `threat.framework` | keyword | `threat[].framework` | `MITRE ATT&CK` |
| `threat.tactic.id`, `threat.tactic.name` | keyword | `threat[].tactic` | Multi-valued: one rule can map to several tactics |
| `threat.technique.id`, `threat.technique.name` | keyword | `threat[].technique[]` | Multi-valued |
| `threat.subtechnique.id` | keyword | `threat[].technique[].subtechnique[]` | Multi-valued |
| `integrations` | keyword | `related_integrations[].package` | For example `github`, `endpoint` |
| `required_fields` | keyword | `required_fields[].name` | Lets coverage check whether the data has the fields a rule needs |
| `query_hash` | keyword | Hash of `query` | Detects query changes without storing the query |
| `last_run.date`, `last_run.status`, `last_run.message`, `last_run.search_duration_ms` | date, keyword, keyword, long | `execution_summary.last_execution` | `null` for rules that have never run. Gap fields, when present, are added after a lab check. |

**Data streams matched** aren't stored on the rule. Writing a count back to every rule would need a second write per rule each sync. Instead, dashboards count a rule's pairs in `rulescope-catalog-rule-streams` with ES|QL, and a rule with no pairs sees no data.

ATT&CK mappings are flattened into multi-valued fields rather than kept as nested objects. ES|QL can count by tactic or technique with `MV_EXPAND`, which keeps dashboards simple. The pairing of a technique with its own tactic is lost in the flat fields. If a report needs that pairing, add a `threat.pair` field such as `TA0009/T1213`.

### Sync run fields

`rulescope-catalog-syncs` records each run: `rulescope.sync.id`, `rulescope.scope`, `kind` (`sync` or `snapshot`), `status` (`success` or `failed`), `started`, `@timestamp` (finished), `spaces`, `rules_seen`, `rules_enabled`, `pairs_written`, `rule_cap_spaces`, and `error`. Snapshots add `day`, `rules_copied` and `total`. A space over the 10,000-rule cap gets its own `kind: rule_cap` document, and the sync record counts them in `rule_cap_spaces`. A Workflows `foreach` can't add up a flag across spaces, so the sync counts these documents at the end instead.

### Privacy

The catalog stores rule metadata, not rule logic or event data:

- **Query text is off by default.** `query_hash` detects changes. An install option, `RULESCOPE_STORE_QUERY=true`, stores the query for teams that want it. Query text can reveal a customer's environment, which matters when catalogs are shared across clusters.
- **User names** in `created_by` and `updated_by` are stored, because change accountability is part of the executive report. An install option can drop them.

## Catalog sync

### Workflows

| Workflow | Schedule | Does |
| --- | --- | --- |
| `rulescope-catalog-sync` | Hourly | Fetch rules, save them, resolve patterns, clean up stale documents, record the sync |
| `rulescope-catalog-snapshot` | Hourly, copies once a day | Copy the rules index into the history data stream with that day's date, after a successful sync |

Both are generated from one template per workflow in `global` and `space:` versions. See [Workflow generation](#workflow-generation).

### Sync stages

The sync reuses what the lag workflows already proved in the lab:

1. **Run start** from Elasticsearch's clock (`ROW started = NOW()`).
2. **Fetch rules.** For each space in scope, call the find API with `per_page=10000` and **no enabled filter**, so disabled rules are included. If a space has more than 10,000 rules, record a notice, as today. Paging is a later item.
3. **Save rules** with one `_bulk` request per space, through the `rulescope-catalog-rules-parse` pipeline.
4. **Resolve patterns** for enabled rules with local patterns, with `_resolve/index`, and save the pairs through `rulescope-catalog-streams-parse`.
5. **Clean up** rules and pairs stamped before the run start, inside this copy's scope only. Pairs are written before cleanup, so a reader always sees the old set, the new set or both, never an empty set.
6. **Record the sync** in both the success and failure paths.

**Including disabled rules makes each fetch larger.** In the lab, all 2,302 rules come back in one page. Rules average about 4.5 KB, so that's around 10 MB, inside the 64 MB step limit the lag workflow already sets. Only enabled rules go through pattern resolution, so the number of loop iterations doesn't grow.

**As built (step 3, confirmed on 9.5.6):**
- Each rule is sent to the parse pipeline whole, as `raw`, with `{{ r | pick: consts.rule_fields | json }}`. Painless extracts the fields and removes `raw`. This replaces listing every field in Liquid.
- Sending whole rules, notes and setup guides included, exceeded Liquid's default render limits. `pick`, a Kibana filter that keeps only the listed fields, fixed it. The sync also raises `settings.liquid.renderLimit` and `memoryLimit` to their maximums (2,000 ms and 60,000,000).
- In the lab, a global sync of 2,304 rules in 2 spaces takes about 6 seconds. Three overlapping syncs left exactly 2,304 rules and 16 pairs, each recording success.
- Painless has `String.sha256()`, used for `query_hash`.

### Daily snapshot

The snapshot workflow copies the rules index into the history data stream with one `_reindex` request:
- `op_type: create`, which data streams require;
- a script that sets `@timestamp` to the snapshot date and `_id` to `<date>:<space>:<rule id>`.

A second run on the same day then skips existing documents instead of duplicating them, with `conflicts: proceed` so the conflicts don't fail the step. **Confirmed in the lab:** `_reindex` into a data stream keeps script-set IDs.

**As built:** the snapshot runs hourly but copies only when today's snapshot hasn't succeeded with at least one rule, and a sync has succeeded in the last 2 hours. A newly imported scheduled workflow runs straight away, so a daily snapshot imported with the sync would otherwise save an empty first day. The guard also retries a failed snapshot within the hour. Document IDs are unique per backing index, so a same-day rerun after a rollover could duplicate that day. That's rare, and dashboards count distinct rules per day.

### Parsing moves out of the lag module

Today the rules parse pipeline decides `supported`, which only means something for lag. The catalog pipeline records **facts** instead: whether the schedule parsed, which patterns are local or remote, the effective timestamp field and the rule type. Each module decides eligibility from those facts in its own queries. For example, the lag module selects rules where `enabled`, `schedule_parsed` and `timestamp_field == "@timestamp"` are all true, and the type isn't machine learning or ES|QL.

### Freshness

Workflows has no confirmed way to start one workflow when another finishes, so modules don't run in a chain. Instead:

- Each module run first reads the latest successful sync for its scope from `rulescope-catalog-syncs`.
- If it's older than **2 hours**, the module records its run as `catalog_stale` with the sync time, and doesn't write results.
- Every dashboard shows the catalog's last successful sync in its health panel.

How a module checks: one ES|QL step finds the latest successful sync for its scope and returns whether it's stale, then an `if` step chooses between the normal run and recording `catalog_stale`. This is the same pattern the lag workflow uses today for its `no_data` check.

**Alerts change with it:**
- Today's stale-profiler alert treats `success` and `no_data` as completed runs. `catalog_stale` must count as **not** completed, because no results were written.
- A new catalog alert fires when no sync succeeded in the last 3 hours, for each scope.

The lag module reads rule settings that are at most one sync old, which is up to an hour. A rule whose look-back changed in that hour is estimated with its old settings once.

## Module designs

### lag: Ingest Lag Profiler

- **Removed:** rule fetching, rule saving, pattern resolution and stale cleanup. The catalog does these.
- **Kept:** the miss-rate, stream stats and host lag queries, result writing, the miss model, the dashboard and alerts.
- **Changed:**
  - The lookup join uses `rulescope-catalog-rule-streams`.
  - Rule eligibility moves into the query, as described above.
  - The *Unsupported rules* panel becomes *Rules the profiler can't estimate*, built from catalog facts.
- **Indices:** `rulescope-lag-results`, `rulescope-lag-stream-stats`, `rulescope-lag-host-lag`, `rulescope-lag-runs`.
- **Done when:** the 105-minute lab validation passes again, within the same tolerance as before.

### coverage: Coverage Report

- **Reference data:** `rulescope-coverage-attack`, a lookup index built by `scripts/build_attack_reference.py` from MITRE's published ATT&CK data. It holds tactic IDs, names and display order, technique IDs and names, sub-technique flags, and deprecated or revoked flags. Each document records its ATT&CK version. The installer loads the version shipped with the pack, and a newer version can be loaded later.
- **Panels:**
  - Rules by status: enabled, disabled, building block.
  - Prebuilt versus custom, and customized prebuilt rules.
  - Enabled rules by severity.
  - Coverage by tactic: techniques with at least one enabled rule, out of the techniques in each tactic.
  - Techniques with no enabled rule.
  - Enabled rules that match no data stream.
  - Rules that failed their last run.
  - Change over time from the history data stream.
- **Evidence levels:** each technique is shown at the highest level it reaches, which matches the argument in the heatmap essay.

| Level | Meaning | Source |
| --- | --- | --- |
| Mapped | A rule maps to the technique | Catalog, any rule |
| Enabled | An enabled rule maps to it | Catalog |
| Has data | An enabled rule matches at least one data stream | Catalog pairs |
| Running | An enabled rule with data succeeded on its last run | Catalog `last_run.status` |
| Fired | An enabled rule produced an alert in the last 30 days | Alerts index, later |

- **Deliverable for executives:** a dashboard designed to read top-down on one screen. Scheduled PDF delivery depends on the license; confirm before promising it.
- **Caveat on every panel:** mapped coverage isn't tested coverage.

### schedule: Schedule Report

- **From the catalog alone:**
  - Intervals and look-backs by rule.
  - Rules whose look-back isn't longer than their interval, which can leave gaps.
  - Rule starts per minute across the hour, as a sign of uneven load.
  - Last run status and search duration.
- **Needs a spike first:** run history and execution gaps over time. The find API returns only the last run. The find API also accepts gap parameters (`gaps_range_start`, `gaps_range_end`), which may be the supported route. The Kibana event log index is another source, but reading it directly isn't supported. Settle this before building more than the catalog-only panels.

## Workflow generation

With four modules and two scopes, hand-kept YAML copies would mean up to eight near-identical files. Instead:

- Each workflow is written once as a template in `modules/<module>/workflows/<name>.yaml.tmpl`. The catalog's are in [modules/catalog/workflows/](modules/catalog/workflows/). (Built in step 2: the lag template is `kibana/workflows/lagprof-hourly.yaml.tmpl` until the module folders arrive in step 4. Its directives are line-based `#%` comments, so they never collide with Liquid; see `rulescope/render.py`.)
- `rulescope render` produces the `global` and `space:<id>` versions, using Python's standard library only, as the installer does today.
- `lint` renders every version and checks:
  - the ES|QL queries match `modules/<module>/esql/`;
  - the row positions in each bulk body match the query's `KEEP` columns, which today's lint doesn't check;
  - no lookup field collides with a pre-join column.

## Installer

`scripts/lagprof.py` becomes the `rulescope` command, with its code split into a small package under `rulescope/`:

| Part | Holds |
| --- | --- |
| `client.py` | Connection, targets and authentication, unchanged |
| `resources.py` | Templates, pipelines, indices, roles, workflows, dashboards and alerts, with fingerprints |
| `modules.py` | The module list, each module's resources, dependencies and the catalog fields it needs |
| `render.py` | Workflow and dashboard generation |
| `cli.py` | `check`, `install`, `status`, `uninstall`, `lint`, `render` |

Example commands:

```bash
rulescope install --target-file targets/prod.env --modules catalog,lag --scope global
rulescope install --target-file targets/prod.env --modules catalog,lag,coverage --scope space:soc --shared-stats
rulescope uninstall --target-file targets/prod.env --modules coverage --scope space:soc
```

**Roles are per module,** so each module's workflow gets only what it needs:

| Role | Kibana | Elasticsearch |
| --- | --- | --- |
| `rulescope-catalog` | Security rules read, Workflows, in its scope's spaces | `view_index_metadata` on `logs-*` for pattern resolution; write on `rulescope-catalog-*` |
| `rulescope-lag` | Workflows | Read on `logs-*` and `rulescope-catalog-*`; write on `rulescope-lag-*` |
| `rulescope-coverage` | Workflows, only if it has a workflow | Read on `rulescope-catalog-*`; write on `rulescope-coverage-*` |
| `rulescope-schedule` | Workflows, only if it has a workflow | Read on `rulescope-catalog-*`; write on `rulescope-schedule-*` |

Per-space roles add the space ID to the name, as today. **Confirmed in the lab (step 3):** the catalog role, with `view_index_metadata` on `logs-*` and no `read`, is enough for a full sync, `_resolve/index` included. A user with only that role resolved the same pairs as an administrator.

**Owner keys:** `RULESCOPE_<MODULE>_WORKFLOW_API_KEY` per module, or `RULESCOPE_WORKFLOW_API_KEY` for one owner of every module. `LAGPROF_WORKFLOW_API_KEY` still works for the lag module.

**Workflow owners.** Scheduled runs use the permissions of the account that last saved the workflow. Today that's one dedicated user with one API key. With per-module roles, there are two options:

| | One owner per module | One owner for all modules |
| --- | --- | --- |
| Setup | One dedicated user and API key per module role, per space | One user with every module role |
| Least privilege | Each workflow gets only its module's access | Every workflow gets the combined access, including `logs-*` read |
| Effort for a 3-space, 3-module install | 9 users and keys | 3 users and keys |

The installer supports both, with one owner per module as the documented default. A single owner is a documented shortcut for labs and small installs.

**Indices created at install.** As today, the installer creates the catalog indices and data streams up front, so ES|QL panels don't show "Unknown index" before the first sync.

**Uninstall order** reverses dependencies: report modules first, catalog last. The installer refuses to remove the catalog while a module that needs it is installed, unless `--force` is given.

## Repository layout

```
rulescope/                     installer package (standard library only)
modules/
  catalog/
    elasticsearch/             component and index templates, ingest pipelines, role
    workflows/                 sync.yaml.tmpl, snapshot.yaml.tmpl
    esql/
    dashboards/                build.py (catalog health panels)
  lag/
    elasticsearch/  workflows/  esql/  dashboards/  alerting/  docs/
  coverage/
    elasticsearch/  reference/  dashboards/  alerting/  docs/
  schedule/
    elasticsearch/  dashboards/  docs/
scripts/
  build_attack_reference.py
  lab/                         unchanged: Rancher Desktop lab, traffic, validation
targets/                       unchanged
tests/
  installer/  render/  pipelines/  model/  lab/
docs/
  architecture.md              this document, once adopted
  setup-guide.md  user guides per module
```

The lag module keeps its docs, validation results and miss model under `modules/lag/docs/`, so its story stays intact.

## Migration from the current code

| Today | Becomes |
| --- | --- |
| `lagprof-rules` | `rulescope-catalog-rules`, with more fields and disabled rules |
| `lagprof-rule-streams` | `rulescope-catalog-rule-streams` |
| `lagprof-results`, `lagprof-stream-stats`, `lagprof-host-lag`, `lagprof-runs` | `rulescope-lag-results`, `rulescope-lag-stream-stats`, `rulescope-lag-host-lag`, `rulescope-lag-runs` |
| `lagprof.*` fields | `rulescope.*` fields |
| `lagprof-rules-parse`, `lagprof-streams-parse`, `lagprof-stamp` | `rulescope-catalog-rules-parse`, `rulescope-catalog-streams-parse`, `rulescope-stamp` |
| Two lag workflow files | One catalog sync template and one lag template, each rendered for both scopes |
| `lagprof-workflow` role | Per-module roles |
| `scripts/lagprof.py` | `rulescope` package and command |

**Lab history:** a one-off `scripts/migrate_lagprof.py` reindexes `lagprof-results` into `rulescope-lag-results`, renaming fields, so the lab dashboard keeps its trend lines. Nothing outside the lab needs migrating.

**GitHub:** rename the repository to `rulescope` in its settings. GitHub redirects the old URL. Rename the local folder at the same time.

## Plan

Hours are estimates.

| Step | Work | Hours | Done when |
| --- | --- | --- | --- |
| 0 | Tag `main` as `v0.1.0-lab` (private checkpoint). Review and adopt this design. | 1–2 | Design merged |
| 1 | Package the installer and add module support, without behavior changes | 8–12 | The 52 unit tests pass; install and uninstall work in the lab |
| 2 | Workflow templates and rendering; the row-position lint check | 5–8 | The rendered lag workflows match today's files exactly |
| 3 | Catalog module: schema, pipelines, sync and snapshot workflows, health panels | 12–18 | Both scopes sync 2,302 rules in the lab; overlap test passes; disabled rules present |
| 4 | Move the lag module onto the catalog; rename indices; migrate lab history | 8–12 | Lab validation passes within its earlier tolerance |
| 5 | Cluster and tenant fields, query privacy option, docs | 3–5 | Fields present on every document |
| 6 | Cloud and Serverless smoke tests, then the first public release (`v0.2.0`: catalog plus lag) | 4–8 | Installs cleanly on both |
| 7 | Coverage module | 15–25 | Dashboard complete in both scopes; ATT&CK reference loads |
| 8 | Schedule module: spike on run history, then build | 3–5, then 10–20 | Data source settled; dashboard complete |
| 9 | Cross-cluster milestone | 15–25 | A central cluster shows two customer clusters in one dashboard |

Steps 0 to 6 total about 41 to 65 hours before the first public release.

## Risks

| Risk | Effect | Mitigation |
| --- | --- | --- |
| The restructure breaks validated lag behavior | Wrong estimates or lost history | Step 2 must reproduce today's workflows exactly; rerun the lab validation in step 4 |
| Scope multiplies with modules | Per-space installs grow complicated | Generated workflows, scope rules enforced by the installer, scope recorded on every document |
| Modules read a stale catalog | Estimates use rule settings up to an hour old | Freshness check in every module run and on every dashboard |
| Scope creep before the first release | Release slips | Coverage, schedule and cross-cluster come after `v0.2.0` |
| Cross-cluster assumptions turn out wrong | Later rework | Schema fields and naming are in place now; the risky assumptions are listed to confirm in step 9 |
| The rule cap at 10,000 per space | Large spaces are cut off | The notice stays; add paging in step 3 if a lab test with more than 10,000 rules is practical |
| Workflow limits at scale | Runs time out with many rules or spaces | Measured in step 3: 2,304 rules in 2 spaces sync in about 6 seconds. Liquid's maximum limits (2 seconds, 60 million) cap how many rules one bulk body can hold; very large spaces will need paging |

## Open questions

- [ ] Do `execution_summary` gap fields appear when a rule has gaps, and what are they called?
- [ ] Do the find API's gap parameters give enough history for the schedule report?
- [ ] Is there a supported way for a central workflow to read a remote Kibana's rules?
- [ ] Does `_resolve/index` resolve remote cluster patterns, and does cross-cluster `LOOKUP JOIN` work with the lag query?
- [ ] Which ATT&CK version do Elastic's prebuilt rules map to in 9.5.6?
- [ ] Which license tier includes scheduled PDF reports?
- [ ] Does Serverless support Workflows, custom roles and the APIs used here? This is carried over from the lag design.
