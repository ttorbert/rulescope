"""The resources each module installs, and how each is rendered and versioned."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .client import Client
from .core import PACK_ROOT, SPACE_ID_RE, VERSION, ConfigError, space_prefix
from .modules import MODULES, resolve
from .render import render_file


def load_json(rel: str | Path) -> dict:
    return json.loads((PACK_ROOT / rel).read_text())


def strip_notes(body: dict) -> dict:
    return {k: v for k, v in body.items() if not k.startswith(("_lagprof", "_rulescope"))}


def fingerprint(path: str | None, extra: str = "") -> str:
    """Pack version plus a hash of the resource's source, e.g. '0.1.0-dev+3f2a9c01be'.

    Installed resources carry it, so a changed file is updated even when the pack
    version hasn't moved (as during development)."""
    import hashlib
    data = (PACK_ROOT / path).read_bytes() if path else b""
    return f"{VERSION}+{hashlib.sha256(data + extra.encode()).hexdigest()[:10]}"


def marker_version(marker: str | None) -> str:
    return (marker or "").split("+", 1)[0]


def with_meta(body: dict, marker: str) -> dict:
    body = strip_notes(body)
    meta = dict(body.get("_meta") or {})
    for old in ("lagprof_version", "lagprof_fingerprint"):
        meta.pop(old, None)
    meta.update(rulescope_version=VERSION, rulescope_fingerprint=marker, managed_by="rulescope")
    body["_meta"] = meta
    return body


def role_spaces(opts_workflow: str, space: str | None) -> list[str]:
    return ["*"] if opts_workflow == "all-spaces" or not space else [space]


def render_role(spaces: list[str], marker: str | None, path: str) -> dict:
    body = strip_notes(load_json(path))
    for entry in body.get("kibana", []):
        if entry.get("spaces") == "__SPACES__":
            entry["spaces"] = spaces
    body["metadata"] = {"rulescope_version": VERSION, "managed_by": "rulescope"}
    if marker:
        body["metadata"]["rulescope_fingerprint"] = marker
    return body


@dataclass
class Resource:
    kind: str
    name: str
    path: str | None = None
    module: str = ""


def module_root(module: str) -> str:
    return f"modules/{module}"


CATALOG_ROOT = module_root("catalog")


def workflow_name(r: Resource, space: str | None) -> str:
    """The name a workflow resource is installed under, in a global or space scope."""
    return f"{r.name}-space-{space}" if space else r.name


def render_module_workflow(r: Resource, space: str | None, shared_stats: bool = False) -> tuple[str, str]:
    """(output file name, YAML) for a module workflow rendered from its template.

    shared_stats fills [[write_shared_stats]] in a space scope (the lag module's switch
    for the one copy that writes stream stats and host lag)."""
    if space is not None and not SPACE_ID_RE.match(space):
        raise ConfigError("--space must be a valid space ID (a-z, 0-9, _ and -)")
    if space:
        scope, params = "space", {"space_id": space, "write_shared_stats": "true" if shared_stats else "false"}
    else:
        scope, params = "global", {}
    text = render_file(r.path, scope, params)
    return f"{workflow_name(r, space)}.yaml", text


def scoped(name: str, space: str | None) -> str:
    """Per-space installs get their own role, dashboard and alert rules.

    Kibana dashboard and alert rule IDs are unique across all spaces, and one shared
    role would be rewritten to the last space installed, so per-space copies must not
    share them."""
    return f"{name}-{space}" if space else name


def pack_resources(space: str | None = None, modules: tuple[str, ...] | list[str] | None = None) -> list[Resource]:
    """The resources of the given modules (all of them when None), in install order.

    space is set for a per-space install (see scoped())."""
    wanted = [m.name for m in resolve(modules)] if modules is not None else [m.name for m in resolve(list(MODULES))]
    out: list[Resource] = []
    for name in wanted:
        out.extend(module_resources(name, space))
    return out


def module_resources(module: str, space: str | None) -> list[Resource]:
    """One module's resources, from its folder under modules/.

    Order: ingest pipelines (rulescope-stamp first, because index templates name it as
    their default pipeline), component templates, index templates, then the indices
    and data streams created up front (so ES|QL panels don't report "Unknown index"
    before the first run), the role, dashboards, alert rules and workflows."""
    root = module_root(module)
    es = PACK_ROOT / root / "elasticsearch"
    res: list[Resource] = []

    def rel(p: Path) -> str:
        return str(p.relative_to(PACK_ROOT))

    pipelines = sorted((es / "ingest-pipelines").glob("*.json"), key=lambda p: (p.stem != "rulescope-stamp", p.stem))
    index_templates = sorted((es / "index-templates").glob("*.json"))
    for kind, files in (("pipeline", pipelines),
                        ("component_template", sorted((es / "component-templates").glob("*.json"))),
                        ("index_template", index_templates)):
        res.extend(Resource(kind, p.stem, rel(p), module) for p in files)
    for p in index_templates:
        body = load_json(rel(p))
        kind = "data_stream" if "data_stream" in body else "index"
        res.append(Resource(kind, body["index_patterns"][0], rel(p), module))
    for p in sorted((es / "security").glob("*-role.json")):
        res.append(Resource("role", scoped(p.stem[:-len("-role")], space), rel(p), module))
    for p in sorted((PACK_ROOT / root / "dashboards").glob("*.json")):
        res.append(Resource("dashboard", scoped(p.stem, space), rel(p), module))
    for p in sorted((PACK_ROOT / root / "alerting").glob("*.json")):
        res.append(Resource("alert_rule", scoped(load_json(rel(p))["id"], space), rel(p), module))
    for wf in MODULES[module].workflows:
        res.append(Resource("workflow", f"rulescope-{module}-{wf}", f"{root}/workflows/{wf}.yaml.tmpl", module))
    return res


# Installed resources record the pack version and fingerprint. Packs before the
# rulescope rename wrote lagprof_* keys and lagprof-version: tags; they're still read.
VERSION_TAG = "rulescope-version:"
VERSION_TAG_PREFIXES = (VERSION_TAG, "lagprof-version:")


def marker_of(meta: dict) -> str:
    for key in ("rulescope_fingerprint", "rulescope_version", "lagprof_fingerprint", "lagprof_version"):
        if meta.get(key):
            return meta[key]
    return ""


ES_PATHS = {
    "component_template": "/_component_template/",
    "index_template": "/_index_template/",
    "pipeline": "/_ingest/pipeline/",
}


def installed_version(client: Client, r: Resource, space: str | None) -> str | None:
    """Return the installed pack version, '' if present but unversioned, None if missing."""
    if r.kind in ES_PATHS:
        code, out = client.es("GET", ES_PATHS[r.kind] + r.name, allow=(404,))
        if code == 404 or not out:
            return None
        if r.kind == "component_template":
            items = out.get("component_templates", [])
            meta = items[0]["component_template"].get("_meta", {}) if items else None
        elif r.kind == "index_template":
            items = out.get("index_templates", [])
            meta = items[0]["index_template"].get("_meta", {}) if items else None
        else:
            meta = (out.get(r.name) or {}).get("_meta", {})
        return None if meta is None else marker_of(meta)
    if r.kind == "role":
        code, out = client.kb("GET", f"/api/security/role/{r.name}", allow=(404,))
        if code == 404:
            return None
        md = out.get("metadata") or {}
        return marker_of(md)
    if r.kind == "alert_rule":
        code, out = client.kb("GET", f"{space_prefix(space)}/api/alerting/rule/{r.name}", allow=(404,))
        if code == 404:
            return None
        tags = [t for t in out.get("tags", []) if t.startswith(VERSION_TAG_PREFIXES)]
        return tags[0].split(":", 1)[1] if tags else ""
    if r.kind == "dashboard":
        code, out = client.kb("GET", f"{space_prefix(space)}/api/dashboards/{r.name}", allow=(404,))
        if code == 404:
            return None
        m = re.search(r"pack v([^,)\s]+)(?:, ([0-9a-f]{10}))?\)", (out.get("data") or {}).get("description", ""))
        if not m:
            return ""
        return f"{m.group(1)}+{m.group(2)}" if m.group(2) else m.group(1)
    return None


# A per-space dashboard or alert shows only its own space. Documents about rules
# carry rulescope.space.id; catalog syncs record the scope that ran them. Stream stats
# and host lag come from raw logs, which aren't divided by space, so they're unfiltered.
SPACE_FILTERED_INDICES = ("rulescope-catalog-rules", "rulescope-catalog-rule-streams",
                          "rulescope-catalog-rule-history", "rulescope-lag-results", "rulescope-lag-runs")
SCOPE_FILTERED_INDICES = ("rulescope-catalog-syncs",)


def scope_query(esql: str, space: str) -> str:
    """Add a space filter after the FROM line of a query on a space-scoped index."""
    first, sep, rest = esql.partition("\n")
    m = re.match(r"\s*FROM\s+(\S+)\s*$", first)
    if m and m.group(1) in SCOPE_FILTERED_INDICES:
        return f'{first}\n| WHERE rulescope.scope == "space:{space}"{sep}{rest}'
    if m and m.group(1) in SPACE_FILTERED_INDICES:
        return f'{first}\n| WHERE rulescope.space.id == "{space}"{sep}{rest}'
    return esql


def _scope_panels(obj, space: str):
    if isinstance(obj, dict):
        if obj.get("type") == "esql" and isinstance(obj.get("query"), str):
            obj["query"] = scope_query(obj["query"], space)
        for v in obj.values():
            _scope_panels(v, space)
    elif isinstance(obj, list):
        for v in obj:
            _scope_panels(v, space)


def render_dashboard(path: str, marker: str, space: str | None = None) -> dict:
    """Dashboard body. A per-space copy's panels only show that space's rules and runs."""
    body = load_json(path)
    ver, _, h = marker.partition("+")
    body["description"] = re.sub(r"pack v[^)]*\)", f"pack v{ver}, {h})", body.get("description", ""))
    if space:
        body["title"] = f"{body['title']} ({space})"
        _scope_panels(body.get("panels", []), space)
    return body


def marker_extra(r: "Resource", spaces: list[str], space: str | None) -> str:
    """Extra input to a resource's fingerprint, for resources rendered differently per install.

    The role depends on its spaces. A per-space dashboard is filtered to its space, so its
    marker includes the space and older, unfiltered copies are updated on the next install."""
    if r.kind == "role":
        return ",".join(spaces)
    if r.kind == "dashboard" and space:
        return f"space={space}"
    return ""


def render_alert_rule(path: str, space: str | None = None) -> dict:
    """Alert rule body. A per-space copy only covers its own space's rules and workflow."""
    body = strip_notes(load_json(path))
    if space:
        body["params"]["esqlQuery"]["esql"] = scope_query(body["params"]["esqlQuery"]["esql"], space)
        body["name"] = f"{body['name']} ({space})"
    return body


LOOKUP_INDEX = "rulescope-catalog-rule-streams"
# Every lookup index the pack creates, and the index template that holds its mapping.
LOOKUP_INDICES = {
    LOOKUP_INDEX: f"{CATALOG_ROOT}/elasticsearch/index-templates/rulescope-catalog-rule-streams.json",
}


def mapped_fields(mapping: dict, prefix: str = "") -> set[str]:
    """Leaf field paths in a mappings 'properties' tree."""
    out: set[str] = set()
    for name, spec in (mapping.get("properties") or {}).items():
        path = f"{prefix}{name}"
        if "properties" in spec:
            out |= mapped_fields(spec, path + ".")
        else:
            out.add(path)
    return out


def lookup_template_fields(index: str = LOOKUP_INDEX) -> set[str]:
    return mapped_fields(load_json(LOOKUP_INDICES[index])["template"]["mappings"])
