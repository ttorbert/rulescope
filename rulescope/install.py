"""install, status and uninstall."""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import resources
from .client import Client, HttpError
from .core import PACK_ROOT, VERSION, WORKFLOWS_API, ConfigError, space_prefix
from .modules import DEFAULT_MODULES
from .report import Reporter
from .resources import (ES_PATHS, LOOKUP_INDEX, Resource, installed_version, load_json, mapped_fields,
                        marker_extra, marker_version, pack_resources, render_alert_rule,
                        render_dashboard, render_role, render_workflow, role_spaces, with_meta)


@dataclass
class InstallOptions:
    workflow: str = "all-spaces"
    space: str | None = None
    shared_stats: bool = False
    import_workflow: bool = True
    force: bool = False
    modules: tuple[str, ...] = DEFAULT_MODULES

    @property
    def kibana_space(self) -> str | None:
        return self.space if self.workflow == "per-space" else None


def installed_workflows(client: Client) -> list[dict] | None:
    """Profiler workflows installed in any space: [{space, name, id, shared_stats}].

    None if workflows can't be listed (for example, the Workflows API is off)."""
    code, spaces = client.kb("GET", "/api/spaces/space", allow=(403, 404))
    ids = [s["id"] for s in spaces] if code == 200 and isinstance(spaces, list) else ["default"]
    found, listed = [], False
    for sid in ids:
        code, out = client.kb("GET", f"{space_prefix(sid)}{WORKFLOWS_API}?size=1000", allow=(400, 403, 404))
        if code != 200:
            continue
        listed = True
        for w in (out or {}).get("results", []):
            name = str(w.get("name", ""))
            if name.startswith("lagprof-hourly"):
                shared = bool(re.search(r"(?m)^\s*write_shared_stats:\s*true\b", w.get("yaml") or ""))
                found.append({"space": sid, "name": name, "id": w.get("id"), "shared_stats": shared})
    return found if listed else None


def mode_conflict(installed: list[dict], opts: "InstallOptions") -> str | None:
    """The two workflow versions are alternatives; running both profiles every space twice."""
    if opts.workflow == "per-space":
        other = [w for w in installed if w["name"] == "lagprof-hourly-all-spaces"]
        if other:
            return ("the all-spaces workflow is installed; install one version, not both. "
                    "Run uninstall first (history is kept), or use --force")
    else:
        other = [w for w in installed if w["name"].startswith("lagprof-hourly-space-")]
        if other:
            spaces = ", ".join(sorted({w["space"] for w in other}))
            return (f"per-space workflows are installed (spaces: {spaces}); install one version, not both. "
                    "Run uninstall --space <id> for each first (history is kept), or use --force")
    return None


def shared_stats_conflict(client: Client, space: str, installed: list[dict] | None = None) -> str | None:
    """Return another space whose installed copy already writes shared stats, if any.

    Uses the installed workflows when they can be listed, so a removed copy doesn't
    block. Otherwise falls back to the last 7 days of run history."""
    if installed is not None:
        others = [w["space"] for w in installed
                  if w["shared_stats"] and w["name"].startswith("lagprof-hourly-space-") and w["space"] != space]
        return others[0] if others else None
    body = {"size": 1, "_source": ["lagprof.space.id"],
            "query": {"bool": {"filter": [{"term": {"lagprof.wrote_shared_stats": True}},
                                          {"range": {"@timestamp": {"gte": "now-7d"}}}],
                               "must_not": [{"term": {"lagprof.space.id": space}}]}}}
    code, out = client.es("POST", "/lagprof-runs/_search", body, allow=(404,))
    if code == 404:
        return None
    hits = out.get("hits", {}).get("hits", [])
    return hits[0]["_source"]["lagprof"]["space"]["id"] if hits else None


def install(client: Client, rep: Reporter, opts: InstallOptions) -> int:
    space = opts.kibana_space
    installed = installed_workflows(client) if opts.import_workflow else None
    if installed is not None and not opts.force:
        why = mode_conflict(installed, opts)
        if why:
            rep.line("BLOCKED", "workflow", opts.workflow, why)
            return 1
    if opts.workflow == "per-space" and opts.shared_stats and not opts.force:
        other = shared_stats_conflict(client, opts.space or "", installed)
        if other:
            rep.line("BLOCKED", "workflow", opts.space or "",
                     f"space {other!r} already writes shared stats; use --force to override")
            return 1
    failures = 0
    for r in pack_resources(space, opts.modules):
        try:
            failures += install_one(client, rep, r, opts, space)
        except HttpError as err:
            rep.line("FAILED", r.kind, r.name, str(err))
            failures += 1
    return 1 if failures else 0


def install_one(client: Client, rep: Reporter, r: Resource, opts: InstallOptions,
                space: str | None) -> int:
    if r.kind == "workflow":
        return install_workflow(client, rep, opts)
    if r.kind == "index":
        code, _ = client.es("GET", f"/{r.name}", allow=(404,))
        if code == 404:
            client.es("PUT", f"/{r.name}")  # settings and mappings come from the index template
            rep.line("created", r.kind, r.name, "empty, from its index template")
            return 0
        if r.name == LOOKUP_INDEX:
            _, m = client.es("GET", f"/{r.name}/_mapping")
            installed = mapped_fields(next(iter(m.values()))["mappings"])
            wanted = resources.lookup_template_fields()
            extra, missing = installed - wanted, wanted - installed
            if extra or missing:
                # Extra fields would enter the join. Missing ones (such as @timestamp, which
                # stale cleanup needs) would be unsearchable on existing documents.
                # Rebuilt by every workflow run, so recreating it loses nothing lasting.
                client.es("DELETE", f"/{r.name}")
                client.es("PUT", f"/{r.name}")
                why = ", ".join(p for p in (f"{len(extra)} extra" if extra else "",
                                            f"{len(missing)} missing" if missing else "") if p)
                rep.line("recreated", r.kind, r.name,
                         f"mapping was out of date ({why} fields); the next workflow run refills it")
                return 0
        rep.line("unchanged", r.kind, r.name, "exists")
        return 0
    if r.kind == "data_stream":
        code, _ = client.es("GET", f"/_data_stream/{r.name}", allow=(404,))
        if code == 404:
            client.es("PUT", f"/_data_stream/{r.name}")
            rep.line("created", r.kind, r.name, "empty, so dashboard panels work before the first run")
        else:
            rep.line("unchanged", r.kind, r.name, "exists")
        return 0
    if r.kind == "dashboard" and not (PACK_ROOT / (r.path or "")).exists():
        rep.line("skipped", r.kind, r.name, "run kibana/dashboards/build.py first")
        return 0

    spaces = role_spaces(opts.workflow, opts.space)
    marker = resources.fingerprint(r.path, marker_extra(r, spaces, space))
    current = installed_version(client, r, space)
    if current == marker and not opts.force:
        rep.line("unchanged", r.kind, r.name, f"v{VERSION}")
        return 0
    verb = "created" if current is None else "updated"

    if r.kind in ES_PATHS:
        client.es("PUT", ES_PATHS[r.kind] + r.name, with_meta(load_json(r.path), marker))
        if r.kind == "component_template":
            sync_existing_mappings(client, rep, r)
    elif r.kind == "dashboard":
        # Dashboards API (9.5+): PUT creates with this ID or replaces it.
        client.kb("PUT", f"{space_prefix(space)}/api/dashboards/{r.name}", render_dashboard(r.path, marker, space))
    elif r.kind == "role":
        client.kb("PUT", f"/api/security/role/{r.name}", render_role(spaces, marker))
    elif r.kind == "alert_rule":
        body = render_alert_rule(r.path, space)
        body["tags"] = [t for t in body.get("tags", []) if not t.startswith("lagprof-version:")]
        body["tags"].append(f"lagprof-version:{marker}")
        body.pop("id")
        rule_id = r.name
        base = f"{space_prefix(space)}/api/alerting/rule/{rule_id}"
        if current is None:
            client.kb("POST", base, body)
        else:
            update = {k: body[k] for k in ("name", "schedule", "params", "tags", "actions") if k in body}
            client.kb("PUT", base, update)
    was = ""
    if current:
        was = " (content changed)" if marker_version(current) == VERSION else f" (was v{marker_version(current)})"
    rep.line(verb, r.kind, r.name, f"v{VERSION}{was}")
    return 0


def sync_existing_mappings(client: Client, rep: Reporter, r: Resource) -> None:
    """Add the component template's fields to lagprof-* indices that already exist.

    Templates only shape new backing indices, and ES|QL rejects a column that no index
    maps, so a dashboard panel reading a field added in a pack update would fail until
    the next rollover. Adding fields is always allowed. The lookup index keeps its own
    mapping: every field in it enters the join.
    """
    props = ((load_json(r.path).get("template") or {}).get("mappings") or {}).get("properties")
    if not props:
        return
    path = f"/lagprof-*,-{LOOKUP_INDEX}/_mapping?allow_no_indices=true&expand_wildcards=open,hidden"
    code, out = client.es("PUT", path, {"properties": props}, allow=(400,))
    if code == 400:
        rep.line("WARN", "mapping", "lagprof-*", f"could not add new fields to existing indices: {out}")


def workflow_id(text: str) -> str:
    m = re.search(r"(?m)^name:\s*(\S+)", text)
    if not m:
        raise ConfigError("workflow YAML has no name")
    return m.group(1)


def install_workflow(client: Client, rep: Reporter, opts: InstallOptions) -> int:
    fname, text = render_workflow(opts.workflow, opts.space, opts.shared_stats)
    out_dir = PACK_ROOT / "build"
    out_dir.mkdir(exist_ok=True)
    (out_dir / fname).write_text(text)
    rel = f"build/{fname}"
    if not opts.import_workflow:
        where = f"the {opts.space!r} space" if opts.workflow == "per-space" else "the Default space"
        rep.line("manual", "workflow", fname, f"import {rel} in Kibana > Workflows in {where}")
        return 0
    prefix = space_prefix(opts.kibana_space)
    name = workflow_id(text)
    owner = client.as_workflow_owner()
    if owner is client:
        rep.line("WARN", "workflow", name, "LAGPROF_WORKFLOW_API_KEY not set: scheduled runs will use "
                 "the installer's privileges. See README, 'Workflow identity'.")
    # Match by name: Kibana doesn't reuse a deleted workflow's ID, so a re-created
    # workflow gets an ID like "<name>-1".
    _, listing = client.kb("GET", f"{prefix}{WORKFLOWS_API}?size=1000")
    matches = [w for w in (listing or {}).get("results", []) if w.get("name") == name]
    if not matches:
        _, out = owner.kb("POST", f"{prefix}{WORKFLOWS_API}", {"workflows": [{"yaml": text}]})
        created = (out or {}).get("created") or [{}]
        if not created[0].get("valid", True):
            rep.line("FAILED", "workflow", name, "Kibana marked the imported workflow invalid")
            return 1
        rep.line("created", "workflow", name, f"id {created[0].get('id', '?')}; first scheduled run starts now")
        return 0
    if len(matches) > 1:
        rep.line("WARN", "workflow", name,
                 f"{len(matches)} workflows share this name ({', '.join(w['id'] for w in matches)}); "
                 "updating the first. Delete the extras so they don't all run.")
    wid = matches[0]["id"]
    _, current = client.kb("GET", f"{prefix}{WORKFLOWS_API}/workflow/{wid}")
    owner_name = _whoami(owner)
    owner_changes = owner is not client and (current or {}).get("lastUpdatedBy") != owner_name
    if (current or {}).get("yaml") == text and not opts.force and not owner_changes:
        rep.line("unchanged", "workflow", name, f"v{VERSION} (id {wid}, saved by {(current or {}).get('lastUpdatedBy')})")
        return 0
    _, out = owner.kb("PUT", f"{prefix}{WORKFLOWS_API}/workflow/{wid}", {"yaml": text})
    if not (out or {}).get("valid", True):
        rep.line("FAILED", "workflow", name, "Kibana marked the updated workflow invalid")
        return 1
    rep.line("updated", "workflow", name, f"v{VERSION} (id {wid}, saved by {owner_name})")
    return 0


def _whoami(client: Client) -> str:
    try:
        _, me = client.es("GET", "/_security/_authenticate")
        return (me or {}).get("username", "?")
    except HttpError:
        return "?"


def status(client: Client, rep: Reporter, space: str | None = None,
           modules: tuple[str, ...] = DEFAULT_MODULES) -> int:
    for r in pack_resources(space, modules):
        if r.kind == "workflow":
            # Per-space copies live in their own space.
            code, out = client.kb("GET", f"{space_prefix(space)}{WORKFLOWS_API}?size=1000", allow=(400, 404))
            names = [f"{w['name']} (id {w['id']})" for w in (out or {}).get("results", [])
                     if str(w.get("name", "")).startswith("lagprof-")] if code == 200 else []
            rep.line("present" if names else "missing", r.kind, ", ".join(names) or r.name,
                     "in this space" if names else "")
            continue
        if r.kind in ("index", "data_stream"):
            path = f"/_data_stream/{r.name}" if r.kind == "data_stream" else f"/{r.name}"
            code, _ = client.es("GET", path, allow=(404,))
            rep.line("missing" if code == 404 else "present", r.kind, r.name)
            continue
        try:
            v = installed_version(client, r, space)
        except HttpError as err:
            rep.line("error", r.kind, r.name, str(err))
            continue
        spaces = role_spaces("per-space" if space else "all-spaces", space)
        marker = resources.fingerprint(r.path, marker_extra(r, spaces, space))
        if v is None:
            rep.line("missing", r.kind, r.name)
        elif v == marker:
            rep.line("current", r.kind, r.name, f"v{VERSION}")
        elif marker_version(v) == VERSION:
            rep.line("outdated", r.kind, r.name, f"v{VERSION}, but the pack file has changed since install")
        else:
            rep.line("outdated", r.kind, r.name, f"v{marker_version(v) or '?'} (pack is v{VERSION})")
    return 0


# Templates, pipelines, indices and data streams are shared by every installed workflow:
# the all-spaces version, or each per-space copy.
SHARED_KINDS = ("component_template", "index_template", "pipeline", "index", "data_stream")


def workflow_name(space: str | None) -> str:
    return f"lagprof-hourly-space-{space}" if space else "lagprof-hourly-all-spaces"


def other_workflows(client: Client, space: str | None) -> list[dict] | None:
    """Installed profiler workflows other than the one this uninstall removes.

    None if workflows can't be listed."""
    installed = installed_workflows(client)
    if installed is None:
        return None
    return [w for w in installed if w["name"] != workflow_name(space)]


def uninstall(client: Client, rep: Reporter, delete_data: bool, space: str | None = None,
              modules: tuple[str, ...] = DEFAULT_MODULES) -> int:
    others = other_workflows(client, space)
    if others is None:
        rep.line("WARN", "workflow", "*", "couldn't list workflows to check for other copies; "
                 "removing shared resources too")
        others = []
    # While another copy is installed, its runs need the shared resources and data.
    shared_with = ", ".join(sorted(f"{w['name']} ({w['space']})" for w in others))
    if delete_data:
        if not others:
            client.es("DELETE", "/_data_stream/lagprof-*", allow=(404,))
            client.es("DELETE", "/lagprof-rules,lagprof-rule-streams?ignore_unavailable=true", allow=(404,))
            rep.line("deleted", "data", "lagprof-*", "data streams and rule indices")
        elif space:
            # Only this space's documents. Stream stats and host lag have no space and
            # are shared, so they stay.
            client.es("POST", "/lagprof-*/_delete_by_query?conflicts=proceed&refresh=true"
                      "&allow_no_indices=true&ignore_unavailable=true",
                      {"query": {"term": {"lagprof.space.id": space}}})
            rep.line("deleted", "data", "lagprof-*", f"documents for space {space!r}; "
                     f"shared stream and host stats kept for {shared_with}")
        else:
            rep.line("kept", "data", "lagprof-*", f"other workflows are installed: {shared_with}")
    failures = 0
    for r in reversed(pack_resources(space, modules)):
        if others and r.kind in SHARED_KINDS:
            rep.line("kept", r.kind, r.name, f"shared with {shared_with}")
            continue
        try:
            if r.kind in ES_PATHS:
                code, out = client.es("DELETE", ES_PATHS[r.kind] + r.name, allow=(400, 404))
                if code == 404:
                    rep.line("absent", r.kind, r.name)
                elif code == 400:
                    rep.line("kept", r.kind, r.name, "in use (history kept; use --delete-data)")
                else:
                    rep.line("deleted", r.kind, r.name)
            elif r.kind == "role":
                code, _ = client.kb("DELETE", f"/api/security/role/{r.name}", allow=(404,))
                rep.line("absent" if code == 404 else "deleted", r.kind, r.name)
            elif r.kind == "alert_rule":
                code, _ = client.kb("DELETE", f"{space_prefix(space)}/api/alerting/rule/{r.name}",
                                    allow=(404,))
                rep.line("absent" if code == 404 else "deleted", r.kind, r.name)
            elif r.kind == "dashboard":
                code, _ = client.kb("DELETE", f"{space_prefix(space)}/api/dashboards/{r.name}", allow=(404,))
                rep.line("absent" if code == 404 else "deleted", r.kind, r.name)
            elif r.kind in ("index", "data_stream"):
                if not delete_data:
                    rep.line("kept", r.kind, r.name, "data (use --delete-data)")
            elif r.kind == "workflow":
                prefix = space_prefix(space)
                code, out = client.kb("GET", f"{prefix}{WORKFLOWS_API}?size=1000", allow=(400, 404))
                # Only this install's workflow: a per-space copy, or the all-spaces version.
                wanted = workflow_name(space)
                found = [w for w in (out or {}).get("results", [])
                         if w.get("name") == wanted] if code == 200 else []
                for w in found:
                    client.kb("DELETE", f"{prefix}{WORKFLOWS_API}/workflow/{w['id']}")
                    rep.line("deleted", r.kind, w["name"], f"id {w['id']}")
                if not found:
                    rep.line("absent", r.kind, r.name)
            else:
                rep.line("manual", r.kind, r.name, "remove in Kibana")
        except HttpError as err:
            rep.line("FAILED", r.kind, r.name, str(err))
            failures += 1
    return 1 if failures else 0
