# Setup guide

This guide installs rulescope's two modules, the Rule Catalog and the Ingest Lag Profiler, into an Elastic deployment and gets the first results on the dashboard. Allow about 30 minutes.

**You'll need:** administrator access to the deployment, a machine with Python 3.9 or later that can reach Elasticsearch and Kibana, and a copy of this repository.

---

## 1. Check the requirements

| Requirement | Detail |
| --- | --- |
| **Elastic version** | 9.5.6 or later. Tested on 9.5.6. Workflows needs 9.4 or later. |
| **License** | Enterprise, or a trial. Workflows isn't available on lower tiers. |
| **Deployment type** | Self-managed, Elastic Cloud (hosted) or Serverless. Serverless hasn't been tested yet; see [Serverless](#serverless). |
| **Data** | Logs in `logs-*` data streams, with `@timestamp`, `event.ingested` and `data_stream.*` fields in each event. Elastic Agent integrations set all three. |
| **Detection rules** | Elastic Security with enabled detection rules. |

If you're not sure about the data, step 4's `check` command measures it for you.

## 2. Get the files

```bash
git clone https://github.com/ttorbert/rulescope.git
cd rulescope
```

Python's standard library is all the installer needs; there's nothing to `pip install`.

## 3. Create a target file

A target file tells the installer where your deployment is and how to sign in. Copy the example that matches your deployment. Files in `targets/` ending in `.env` are git-ignored, so credentials stay out of the repository.

| Deployment | Copy |
| --- | --- |
| Self-managed | `cp targets/self-managed.env.example targets/prod.env` |
| Elastic Cloud (hosted) | `cp targets/cloud.env.example targets/prod.env` |
| Serverless | `cp targets/serverless.env.example targets/prod.env` |

Then edit `targets/prod.env`. Each line is `NAME=value`, and lines starting with `#` are comments:

```bash
# Deployment type: self-managed | cloud | serverless
LAGPROF_TARGET=self-managed

# Where Elasticsearch and Kibana are. These work for every deployment type.
ES_URL=https://elasticsearch.example.internal:9200
KIBANA_URL=https://kibana.example.internal:5601

# Elastic Cloud only, optional: a Cloud ID instead of the two URLs above.
# CLOUD_ID=my-deployment:dXMtZWFzdC0xLmF3cy5mb3VuZC5pbyRhYmMxMjMkZGVmNDU2

# Sign-in: either an API key...
ES_API_KEY=<encoded admin API key>
# ...or a username and password (self-managed only).
# ES_USERNAME=elastic
# ES_PASSWORD=<password>

# Only if your cluster uses a private certificate authority:
# CA_CERT=/path/to/ca.crt
# VERIFY_TLS=true

# Added in step 7: API keys of each module's own workflow user.
# RULESCOPE_CATALOG_WORKFLOW_API_KEY=<encoded key>
# RULESCOPE_LAG_WORKFLOW_API_KEY=<encoded key>
```

**Settings:**

| Setting | Needed | Value |
| --- | --- | --- |
| `LAGPROF_TARGET` | Always | `self-managed`, `cloud` or `serverless` |
| `ES_URL` | Yes, unless `CLOUD_ID` is set | Elasticsearch address, including the port, for example `https://…:9200`. On Elastic Cloud, copy it from the deployment page (*Copy endpoint*). On Serverless, from *Connection details*. |
| `KIBANA_URL` | Yes, unless `CLOUD_ID` is set | Kibana address, found in the same places |
| `CLOUD_ID` | Never; optional on Elastic Cloud | A shortcut that encodes both addresses: the deployment name, a colon, then an encoded string. Copy it from the deployment's overview page. If you also set `ES_URL` and `KIBANA_URL`, the URLs win. Using the URLs everywhere is simplest. |
| `ES_API_KEY` | For Cloud and Serverless; or use a username and password on self-managed | The **Encoded** value shown when you create an API key |
| `ES_USERNAME`, `ES_PASSWORD` | Self-managed only, instead of `ES_API_KEY` | A user's login. The same login is used for Kibana. |
| `CA_CERT` | Only with a private certificate authority | Path to the CA certificate file |
| `VERIFY_TLS` | No | `true` (default). Set `false` only for a throwaway test cluster. |
| `RULESCOPE_CATALOG_WORKFLOW_API_KEY`, `RULESCOPE_LAG_WORKFLOW_API_KEY` | Recommended; see step 7 | API key of each module's own low-privilege workflow user. `LAGPROF_WORKFLOW_API_KEY`, from before the rename, still works for the lag module. `RULESCOPE_WORKFLOW_API_KEY` sets one owner for both. |

**The admin login** (`ES_API_KEY`, or `ES_USERNAME` and `ES_PASSWORD`) is only used while installing. The simplest choice is a user with the `superuser` role. A narrower set of privileges is listed under [Installer privileges](#installer-privileges).

**Quoting:** you don't need quotes. If a value contains `$`, a space or `#`, wrap it in single quotes, for example `ES_PASSWORD='p@ss$word #1'`. Without quotes, `${...}` is read as a variable (see below), and anything after ` #` is read as a comment.

### Keeping secrets out of the file (optional)

Instead of writing a password or API key in the target file, you can keep it in an environment variable. There are two ways.

**Option A: refer to the variable from the file.** Write `${VARIABLE_NAME}` as the value:

```bash
# targets/prod.env
ES_API_KEY=${RULESCOPE_ADMIN_KEY}
RULESCOPE_LAG_WORKFLOW_API_KEY=${RULESCOPE_LAG_OWNER_KEY}
```

Then set the variables in the terminal before running the installer:

```bash
export RULESCOPE_ADMIN_KEY='<encoded admin API key>'
export RULESCOPE_LAG_OWNER_KEY='<encoded workflow API key>'
python3 -m rulescope check --target-file targets/prod.env
```

**Option B: set the setting itself as a variable.** Any setting in the table above can be set directly as an environment variable, and it **overrides** the target file:

```bash
export ES_API_KEY='<encoded admin API key>'
python3 -m rulescope install --target-file targets/prod.env
```

**Things to know:**
- **Variables last only for the current terminal session.** `export` sets them until you close the terminal.
  - To set one for a single command only, put it in front: `ES_PASSWORD='…' python3 -m rulescope check --target-file targets/prod.env`
  - To keep them across sessions, add the `export` lines to your shell profile (`~/.zshrc` on macOS, `~/.bashrc` on most Linux), or load them from a password manager's command-line tool.
- **Your shell history records what you type.** In zsh and bash, starting a command with a space usually keeps it out of the history (zsh needs `setopt HIST_IGNORE_SPACE`). Or read the value without echoing it: `read -rs RULESCOPE_ADMIN_KEY && export RULESCOPE_ADMIN_KEY`.
- **To check a variable is set without printing it:** `[ -n "$RULESCOPE_ADMIN_KEY" ] && echo set || echo missing`.

## 4. Check the deployment

```bash
python3 -m rulescope check --target-file targets/prod.env
```

Each line reports `ok`, `WARN` or `BLOCKED`:

```
ok         elasticsearch      https://...  version 9.5.6, flavor default
ok         license            enterprise
ok         kibana             https://...  status available
ok         workflows          /api/workflows
ok         data               logs-*  48211903 events in the last hour, 99.2% have event.ingested
```

- **`BLOCKED`** means installing would fail, so `install` won't run until it's fixed. The usual causes are an old version, a lower license tier, or Workflows not being available.
- **`WARN` on `data`** means some events have no `event.ingested`. The profiler can't measure lag for those events and lists them on the dashboard's *Measurement gaps* panel. See [Troubleshooting](#troubleshooting).

## 5. Choose a scope

Detection rules belong to a Kibana space. Both modules come in two scopes, and they're installed in the same one:

| | **Global** (default) | **Per space** |
| --- | --- | --- |
| Install | Once, in the Default space | Once in each space that has rules |
| Finds new spaces | Automatically, on the catalog's next sync | You install another copy |
| Permissions | Each workflow's role covers every space | Each copy's roles cover only its own space |
| Use when | Almost always | Spaces belong to separate customers or teams that must stay isolated |

The [lag module's README](../modules/lag/README.md#which-scope) has the full comparison.

## 6. Install

**Global:**

```bash
python3 -m rulescope install --target-file targets/prod.env
```

**Per space:** run this once for each space. Add `--shared-stats` to exactly one copy:

```bash
python3 -m rulescope install --target-file targets/prod.env --workflow per-space --space soc --shared-stats
python3 -m rulescope install --target-file targets/prod.env --workflow per-space --space team-b
```

Each per-space copy gets its own roles (`rulescope-catalog-<space>`, `rulescope-lag-<space>`), its own dashboards (*Rule Catalog (soc)*, *Ingest Lag Profiler (soc)*), and its own alert rules, which cover only that space. Install one scope, not both: the installer refuses a per-space copy while the global version is installed, and the other way round. To switch, uninstall the current version first (history is kept).

The installer runs `check` first, then installs the Rule Catalog, then the lag module:

| Resource | What it is |
| --- | --- |
| **Rule Catalog** | See its [README](../modules/catalog/README.md) |
| Ingest pipelines (3), component templates (2), index templates (4) | Parse rules and data stream names; mappings and retention for the `rulescope-catalog-*` indices |
| `rulescope-catalog-rules`, `rulescope-catalog-rule-streams`, and data streams `rulescope-catalog-rule-history`, `rulescope-catalog-syncs` | Every rule, the lookup index of rules and the data streams they search, daily history, and sync records |
| Role `rulescope-catalog` (per space: `rulescope-catalog-<space>`) | The permissions the catalog workflows run with |
| Workflows `rulescope-catalog-sync` and `rulescope-catalog-snapshot` | The hourly sync, and the daily snapshot. **Their first runs start immediately.** |
| Dashboard *Rule Catalog*, alert rule | Catalog health; no successful sync in 3 hours |
| **Ingest Lag Profiler** | See its [README](../modules/lag/README.md) |
| Component template, index templates (4) | Mappings and retention for the `rulescope-lag-*` data streams |
| Data streams (4): `rulescope-lag-results`, `rulescope-lag-stream-stats`, `rulescope-lag-host-lag`, `rulescope-lag-runs` | Created empty, so the dashboard shows empty panels rather than errors before the first run |
| Role `rulescope-lag` (per space: `rulescope-lag-<space>`) | The permissions the lag workflow runs with |
| Workflow `rulescope-lag-hourly` | Runs every hour; **its first run starts immediately**, usually before the catalog's first sync has finished, so it records `catalog_stale`. The next run uses the catalog. |
| Dashboard *Ingest Lag Profiler* | Ten panels; see the [user guide](user-guide.md) |
| Alert rules (2) | Miss rate above 1%; no completed run in 3 hours |

Running `install` again is safe. It updates only what has changed and reports `unchanged` for the rest.

The first install shows warnings like these:

```
WARN       workflow           rulescope-catalog-sync  RULESCOPE_CATALOG_WORKFLOW_API_KEY not set: scheduled runs will use the installer's privileges.
WARN       workflow           rulescope-lag-hourly  RULESCOPE_LAG_WORKFLOW_API_KEY not set: scheduled runs will use the installer's privileges.
```

Step 7 fixes it.

## 7. Give the workflows their own limited identity

**This step matters for security.** A workflow's scheduled runs use the permissions of **whoever last saved it**. After step 6 that's your administrator account, so the hourly runs have full administrator rights.

Give each module its own user with only that module's role. In per-space mode, do this for each space, with users that have only that space's roles (`rulescope-catalog-<space>`, `rulescope-lag-<space>`).

1. **Create two dedicated users,** for example `rulescope-catalog-runner` with only the `rulescope-catalog` role, and `rulescope-lag-runner` with only the `rulescope-lag` role. The installer created both roles. Do this in **Stack Management › Users**, or with the API:

   ```bash
   curl -u admin -X PUT "$ES_URL/_security/user/rulescope-lag-runner" -H 'Content-Type: application/json' \
     -d '{"password": "<strong password>", "roles": ["rulescope-lag"], "full_name": "rulescope lag module"}'
   ```

2. **Create an API key as each user.** Sign in to Kibana as the user, open **Stack Management › API keys › Create API key**, and copy the *Encoded* value. Or use the API:

   ```bash
   curl -u rulescope-lag-runner -X POST "$ES_URL/_security/api_key" -H 'Content-Type: application/json' \
     -d '{"name": "rulescope-lag-owner"}'
   ```

3. **Add the keys to your target file and install again:**

   ```bash
   echo 'RULESCOPE_CATALOG_WORKFLOW_API_KEY=<encoded key>' >> targets/prod.env
   echo 'RULESCOPE_LAG_WORKFLOW_API_KEY=<encoded key>' >> targets/prod.env
   python3 -m rulescope install --target-file targets/prod.env
   ```

   Each workflow line should now end with `saved by` the module's user.

One user for both modules (`RULESCOPE_WORKFLOW_API_KEY`) is a shortcut for small installs, but then every workflow gets both roles' access.

**What the roles allow:**
- **`rulescope-catalog`:** read detection rules in Kibana; list which `logs-*` data streams a pattern matches (no `read` on events); read and write `rulescope-catalog-*`.
- **`rulescope-lag`:** read `logs-*` and `rulescope-catalog-*`; read and write `rulescope-lag-*`. No access to detection rules in Kibana.
- **Both:** read, create, update and run workflows, but not delete them; `manage_own_api_key`, which covers only the user's own keys.

## 8. Confirm it's working

1. **Status:** `python3 -m rulescope status --target-file targets/prod.env` should list every resource as `current` or `present`.
2. **Workflows:** in Kibana, open **Workflows › rulescope-catalog-sync › Executions**. The first run should be *completed*, usually within a minute. Then check **rulescope-lag-hourly**: its first run is *completed* too, usually recorded as `catalog_stale`.
3. **Dashboards:** open **Dashboards › Rule Catalog**. *Catalog health* should show a successful sync. Then open **Dashboards › Ingest Lag Profiler**.
   - *Profiler health* shows the first run, usually **catalog_stale**: it started before the catalog's first sync finished. To get results without waiting for the next hourly run, run **rulescope-lag-hourly** by hand in Kibana (**Run**). On a new cluster with no logs yet, its status is then **no_data** with the note *No data in logs-\* yet*. Neither is a failure.
   - *Rule coverage* shows your rules.
   - *Rules at risk* and the lag panels fill in from the **previous full clock hour**. If logs were flowing then, they show data straight away. If not, they fill in after the next hourly run.

The [user guide](user-guide.md) explains how to read the results.

## 9. Send alerts somewhere

The three alert rules are installed **without notifications**. To get them by email, Slack or another channel:

1. Open **Stack Management › Rules**.
2. Edit **Ingest Lag Profiler: rule miss rate above 1%**, **Ingest Lag Profiler: no completed run in 3 hours** and **Rule Catalog: no successful sync in 3 hours**.
3. Under **Actions**, add a connector.

**To change the 1% threshold**, edit the last `WHERE miss_rate_pct > 1.0` line in the miss-rate rule's ES|QL query. Make the same change in `modules/lag/alerting/miss-rate.json`, or the next upgrade will put the old value back.

---

## Upgrading

Pull the new version and run `install` again with the same options. Changed resources are updated, and history in the `rulescope-*` data streams is kept.

## Uninstalling

```bash
python3 -m rulescope uninstall --target-file targets/prod.env                 # keep history
python3 -m rulescope uninstall --target-file targets/prod.env --delete-data   # remove history too
```

This removes both modules. `--modules lag` removes only the lag module; the installer refuses `--modules catalog` while the lag module is installed, because the lag module reads the catalog.

In per-space mode, add `--space <id>` for each space. The workflow users and their API keys aren't touched; delete them yourself if you no longer need them.

While other per-space copies remain, `uninstall --space <id>` removes only that copy's workflows, roles, dashboards and alert rules. The shared templates, pipelines and indices stay for the other copies. With `--delete-data`, it deletes only that space's documents; stream stats and host lag are shared, so they stay. Uninstalling the last copy removes everything.

## Serverless

The installer supports Serverless Security projects, but this pack **hasn't been tested on Serverless yet**. `check` reports anything that blocks it. Two differences are likely:
- **Workflows availability.** It depends on the project, and `check` tells you.
- **Users.** Serverless has no local users, so step 7 needs an API key created by a project member who has a custom role with the same privileges as each module's role. That's possible only if your project supports custom roles.

## Installer privileges

The admin account used for installing needs the privileges below. A `superuser` key is the simple option. This narrower list hasn't been tested.

| Where | Privileges |
| --- | --- |
| **Elasticsearch, cluster** | `manage_index_templates`, `manage_ingest_pipelines`, `manage_security` (to create the role), `monitor` |
| **Elasticsearch, indices** | `rulescope-*`: all. `logs-*`: `read` (for the data check). |
| **Kibana** | Workflows: all. Dashboards: all. Stack rules: all. Security › Rules: read. Every space you install into. |

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `BLOCKED license` | Workflows needs Enterprise or a trial. |
| `BLOCKED workflows` | The deployment doesn't offer Workflows. Check the version (9.4 or later) and license, and that the admin key's user can see Workflows. |
| `WARN data … % have event.ingested` | Some sources don't set `event.ingested`. Elastic Agent integrations do; custom ingest paths, such as Logstash, Beats outputs or direct API writes, often don't. Add a final pipeline that sets `event.ingested` to `{{{_ingest.timestamp}}}`. |
| *Rules at risk* is empty | Often **good news**. Check *Rule coverage*. Most of Elastic's prebuilt rules search `event.ingested`, so lag can't make them miss. Only rules on `@timestamp` with lag past their margin appear. |
| A rule you expect is missing | Its index patterns must match `logs-*` data streams with `data_stream.dataset` and `namespace` in the events. The *Rule Catalog* dashboard lists enabled rules that match no data stream. Rules using a data view, ES\|QL rules and machine learning rules are listed under *Rules the profiler can't estimate*. |
| *Profiler health*: `catalog_stale` | The Rule Catalog's last successful sync is more than 2 hours old, or there's none yet. Right after installing, that's expected for the first run. Otherwise open **Workflows › rulescope-catalog-sync › Executions**. |
| Catalog sync failed: `rule_bulk_write_failed_see_step_save_rules` | Writing the rules failed, usually because an ingest pipeline is missing. Run `install` again. |
| *Rule Catalog* recent runs: *More than 10,000 rules in space …* | The catalog fetches one page of up to 10,000 rules per space, so the rest aren't in the catalog or profiled. This is a known limit of this version. |
| *Profiler health*: `no_data`, *No data in logs-\* yet* | Nothing has arrived in `logs-*` data streams. Normal on a new cluster. Results start in the first full clock hour after logs arrive. |
| *Profiler health*: `no_data`, *none have event.ingested* | Logs arrive, but none carry `event.ingested`, so lag can't be measured. Add the final pipeline described in the `WARN data` row above. |
| *Profiler health* shows nothing new | The workflow is disabled, or its owner's API key was deleted or expired. Repeat step 7. |
