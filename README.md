# rulescope

rulescope runs entirely inside Elastic Security 9.5.6 or later. It keeps a **Rule Catalog** of every detection rule and adds report modules that read from it. The first report module is the **Ingest Lag Profiler**.

- **[Rule Catalog](modules/catalog/README.md):** every rule in every space it covers, enabled or not. It records which data streams each enabled rule searches and keeps a daily history. It syncs hourly.
- **[Ingest Lag Profiler](modules/lag/README.md):** estimates how many events each enabled rule misses because logs arrive late. An hourly workflow measures the lag between when events happened (`@timestamp`) and when they arrived (`event.ingested`). It then estimates each rule's miss rate from the rule's schedule and look-back in the catalog, and keeps the history with a dashboard and alerts.

Both modules come in a **global** scope (one copy covers every space) or **per space** (one copy per space, each with a role limited to that space). The design, including the planned cross-cluster support for managed service providers, is in [rulescope-design.md](rulescope-design.md).

> **Status: in development (v0.1.0-dev).** Everything runs end to end on 9.5.6 in the lab: workflows, queries, pipelines, installer, dashboards and alerts. **The lab validation against real rule alerts passed:** the profiler estimated 55.8% against an observed 55.6% ([results](modules/lag/docs/validation-results.md)). Installer tests on Elastic Cloud and Serverless are still to do. See the [plan](rulescope-design.md#plan).

**Results are estimates, not proof.** `est_missed` is the expected number of events, in the data streams a rule searches, that fall outside the rule's window. It isn't a count of missed alerts. See the [miss model](modules/lag/docs/miss-model.md).

**Who it's for.** In 9.5.6, **89% of Elastic's prebuilt rules** (2,039 of 2,297) search on `event.ingested`, so lag can't push events out of their window. They do fall back to `@timestamp` for events that have no `event.ingested`, and the profiler counts those events.

The rules at risk are:
- the 146 supported prebuilt rules that search on `@timestamp`
- custom, third-party and imported rules

The dashboard's *Rule coverage* panel shows which group each rule is in.

![Ingest Lag Profiler dashboard: rules at risk ranked by estimated missed events, with recommended look-backs, plus lag trends, slow hosts, clock problems, measurement gaps and rule coverage](docs/images/dashboard.png)

*The lag dashboard after an hour of lab traffic (`make lab-traffic`). See the [user guide](docs/user-guide.md#the-dashboard) for each panel.*

**Cost.** The hourly miss-rate query takes about 1 second per million events in the hour on a single-node laptop lab, however many rules search each stream.

## Documentation

| Guide | For |
| --- | --- |
| [Setup guide](docs/setup-guide.md) | Installing it in an Elastic deployment, step by step |
| [User guide](docs/user-guide.md) | Reading the lag dashboard and acting on findings |
| [Rule Catalog](modules/catalog/README.md) | The catalog module: scopes, workflows, indices, dashboard, limits |
| [Ingest Lag Profiler](modules/lag/README.md) | The lag module: scopes, workflow, indices, dashboard, alerts |
| [Why use it](modules/lag/docs/value.md) | The lag module's business and operational value, costs and limits |
| [Validation results](modules/lag/docs/validation-results.md) | How the lag estimates were tested against real rules |
| [Miss model](modules/lag/docs/miss-model.md) | The formula, its simulation check, and what it leaves out |
| [Field reference](modules/lag/docs/fields.md) | Every field in the `rulescope-lag-*` data streams |
| [rulescope design](rulescope-design.md) | Architecture: catalog, modules, space scopes, cross-cluster planning, and the plan |
| [Lag module design](ingest-lag-profiler-design.md) | How the lag estimates work, with [notes for reviewers](ingest-lag-profiler-design.md#notes-for-reviewers) and [known issues](ingest-lag-profiler-design.md#known-issues). Written before the restructure; the module now reads rules from the catalog. |
| [Build plan](build-plan.md) | The lag module's original milestones |

## Requirements

| | |
| --- | --- |
| Elastic Stack | 9.5.6 or later (Workflows, multi-field `LOOKUP JOIN`) |
| License | Enterprise or trial (Workflows) |
| Serverless | Security project; Workflows availability still to be confirmed |
| Data | `logs-*` data streams with `@timestamp` and `event.ingested` (Fleet integrations set both) |
| Installer | Python 3.9 or later; no packages needed |

## Install

1. Create a target file from one of the examples in [targets/](targets/): `self-managed`, `cloud` or `serverless`.
2. Check the target, then install:

   ```bash
   python3 -m rulescope check   --target-file targets/prod.env
   python3 -m rulescope install --target-file targets/prod.env
   ```

   This installs the Rule Catalog and the lag module: index templates, ingest pipelines, roles, workflows, dashboards and alert rules. The workflows start their first runs straight away, then run hourly. The lag module's first run usually records `catalog_stale`, because it starts before the catalog's first sync finishes.

3. Choose a scope. **Global** is the default. See [the lag module's README](modules/lag/README.md#which-scope) for when to use **per space** instead:

   ```bash
   python3 -m rulescope install --target-file targets/prod.env \
       --workflow per-space --space soc --shared-stats
   ```

`--modules catalog` installs the catalog alone; `--modules lag` installs the lag module and the catalog it needs. Other commands: `status` (installed versions), `uninstall` (keeps history unless `--delete-data`), `lint` (offline checks), `render-workflow` (writes the workflow YAML to `build/` for a manual import).

In per-space mode, `uninstall --space <id>` removes only that copy while other copies remain: shared templates, pipelines and indices stay, and `--delete-data` deletes only that space's documents.

### Workflow identity

A workflow's **scheduled runs use the permissions of the account that last saved it.** Manual runs use whoever starts them. Both were confirmed on 9.5.6. If the installer saves the workflows as an administrator, the hourly runs get administrator permissions.

To keep them limited, give each module its own user with only that module's role, and its own API key:

| Module | Role (per space: `<role>-<space>`) | Target file setting |
| --- | --- | --- |
| Rule Catalog | `rulescope-catalog` | `RULESCOPE_CATALOG_WORKFLOW_API_KEY` |
| Ingest Lag Profiler | `rulescope-lag` | `RULESCOPE_LAG_WORKFLOW_API_KEY` |

Then run `install` again. The [setup guide](docs/setup-guide.md#7-give-the-workflows-their-own-limited-identity) has the steps. Without the keys, `install` warns that scheduled runs will use the installer's permissions.

## Lab (Rancher Desktop)

The lab runs Elasticsearch and Kibana 9.5.6 with security, TLS and a trial license, on [Rancher Desktop](https://rancherdesktop.io) with the dockerd (moby) engine.

```bash
make lab-up        # checks Rancher Desktop, creates passwords, starts the lab, installs the pack
make lab-traffic   # sends lab events with slow, bursty and clock-skewed hosts
make lab-down      # stop (keeps data); make lab-reset deletes everything
```

- **Rancher Desktop:** needs at least 6 GB for its VM (8 recommended). Set it in Preferences > Virtual Machine, or run `rdctl set --virtual-machine.memory-in-gb 8`.
- **Kernel setting:** `make lab-up` raises the VM's `vm.max_map_count` until the next VM restart. To keep it across restarts, run `scripts/lab/rancher-setup.sh --persist` once.
- **Login:** Kibana is at http://localhost:5601. The user is `elastic`, and the password is in `scripts/lab/.env`. The dashboards are **Rule Catalog** and **Ingest Lag Profiler** under Dashboards.
- **`make lab-owner`** creates the `rulescope-catalog-runner` and `rulescope-lag-runner` users and saves each module's workflows with its user's API key. See [Workflow identity](#workflow-identity).
- **`make validate`** runs the lab validation: real rules, events with known lag, and alerts matched to events (about 105 minutes). See the [validation results](modules/lag/docs/validation-results.md).
- **A lab from before the rename** still has `lagprof-*` indices. [scripts/migrate_lagprof.py](scripts/migrate_lagprof.py) copies their history into `rulescope-lag-*` and removes the old objects.

## Development

```bash
make test             # lint, unit tests, quick formula simulation (no cluster needed)
make simulate         # full formula simulation
make test-pipelines   # ingest pipeline fixtures against the lab
make dashboard        # run the dashboard queries against the lab, then rebuild their JSON
make package          # zip the pack into dist/
```

| Path | Contents |
| --- | --- |
| [modules/catalog/](modules/catalog/) | Rule Catalog: templates, pipelines, role, workflow templates, dashboard, alert |
| [modules/lag/](modules/lag/) | Ingest Lag Profiler: templates, role, workflow template, the three hourly queries ([esql/](modules/lag/esql/), checked against the workflow by `lint`), dashboard, alerts, docs |
| [rulescope/](rulescope/) | Installer package for Docker, self-managed, Cloud and Serverless (`python3 -m rulescope`). [scripts/lagprof.py](scripts/lagprof.py) is a wrapper kept for the lab scripts. |
| [scripts/lab/](scripts/lab/) | Rancher Desktop lab, traffic generator, validation |
| [tests/](tests/) | Formula simulation, installer and render unit tests, pipeline fixtures; `tests/lab/` holds spikes and helpers run by hand |
| [docs/](docs/) | Setup and user guides |

Design: [rulescope-design.md](rulescope-design.md). Reviewing the lag module's code? Start with the lag design's [notes for reviewers](ingest-lag-profiler-design.md#notes-for-reviewers): the invariants a change can break. Its code map predates the restructure; the [repository layout](rulescope-design.md#repository-layout) is current.
