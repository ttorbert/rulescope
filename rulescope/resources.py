"""The resources each module installs, and how each is rendered and versioned."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .client import Client
from .core import PACK_ROOT, ROLE_NAME, SPACE_ID_RE, VERSION, WORKFLOW_FILES, ConfigError, space_prefix
from .modules import MODULES, resolve


def load_json(rel: str | Path) -> dict:
    return json.loads((PACK_ROOT / rel).read_text())


def strip_notes(body: dict) -> dict:
    return {k: v for k, v in body.items() if not k.startswith("_lagprof")}


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
    meta.update(lagprof_version=VERSION, lagprof_fingerprint=marker, managed_by="lagprof")
    body["_meta"] = meta
    return body


def role_spaces(opts_workflow: str, space: str | None) -> list[str]:
    return ["*"] if opts_workflow == "all-spaces" or not space else [space]


def render_role(spaces: list[str], marker: str | None = None) -> dict:
    body = strip_notes(load_json("elasticsearch/security/lagprof-workflow-role.json"))
    for entry in body.get("kibana", []):
        if entry.get("spaces") == "__SPACES__":
            entry["spaces"] = spaces
    body["metadata"] = {"lagprof_version": VERSION, "managed_by": "lagprof"}
    if marker:
        body["metadata"]["lagprof_fingerprint"] = marker
    return body


def render_workflow(mode: str, space: str | None = None, shared_stats: bool = False) -> tuple[str, str]:
    """Return (output file name, YAML text) with per-space settings filled in."""
    if mode not in WORKFLOW_FILES:
        raise ConfigError(f"--workflow must be one of {', '.join(WORKFLOW_FILES)}")
    text = (PACK_ROOT / WORKFLOW_FILES[mode]).read_text()
    if mode == "all-spaces":
        return "lagprof-hourly-all-spaces.yaml", text
    if not space or not SPACE_ID_RE.match(space):
        raise ConfigError("per-space mode needs --space with a valid space ID (a-z, 0-9, _ and -)")
    text = text.replace("name: lagprof-hourly-space-CHANGEME", f"name: lagprof-hourly-space-{space}", 1)
    text, n1 = re.subn(r"(?m)^(\s*space_id:\s*)\S+", rf"\g<1>{space}", text, count=1)
    text, n2 = re.subn(r"(?m)^(\s*write_shared_stats:\s*)\S+",
                       rf"\g<1>{'true' if shared_stats else 'false'}", text, count=1)
    if not (n1 and n2):
        raise ConfigError("per-space workflow file is missing space_id or write_shared_stats")
    return f"lagprof-hourly-space-{space}.yaml", text


@dataclass
class Resource:
    kind: str
    name: str
    path: str | None = None
    module: str = "lag"


def scoped(name: str, space: str | None) -> str:
    """Per-space installs get their own role, dashboard and alert rules.

    Kibana dashboard and alert rule IDs are unique across all spaces, and one shared
    role would be rewritten to the last space installed, so per-space copies must not
    share them."""
    return f"{name}-{space}" if space else name


def pack_resources(space: str | None = None, modules: tuple[str, ...] | list[str] | None = None) -> list[Resource]:
    """The resources of the given modules (all of them when None), in install order.

    space is set for a per-space install (see scoped())."""
    wanted = [m.name for m in resolve(modules)] if modules is not None else list(MODULES)
    out: list[Resource] = []
    for name in wanted:
        out.extend(r for r in _lag_resources(space) if r.module == name)
    return out


def _lag_resources(space: str | None) -> list[Resource]:
    """The Ingest Lag Profiler's resources."""
    res: list[Resource] = []
    for kind, folder in (("component_template", "elasticsearch/component-templates"),
                         ("index_template", "elasticsearch/index-templates"),
                         ("pipeline", "elasticsearch/ingest-pipelines")):
        for p in sorted((PACK_ROOT / folder).glob("*.json")):
            res.append(Resource(kind, p.stem, str(p.relative_to(PACK_ROOT))))
    # Created up front so the miss-rate query and dashboard work before the first run.
    res.append(Resource("index", "lagprof-rules"))
    res.append(Resource("index", "lagprof-rule-streams"))
    # Data streams are otherwise created by the first write. Until then ES|QL reports
    # "Unknown index", so every dashboard panel would show an error on a new install.
    for p in sorted((PACK_ROOT / "elasticsearch/index-templates").glob("*.json")):
        body = load_json(p.relative_to(PACK_ROOT))
        if "data_stream" in body:
            res.append(Resource("data_stream", body["index_patterns"][0]))
    res.append(Resource("role", scoped(ROLE_NAME, space), "elasticsearch/security/lagprof-workflow-role.json"))
    res.append(Resource("workflow", "lagprof-hourly"))
    res.append(Resource("dashboard", scoped("lagprof-overview", space), "kibana/dashboards/lagprof-overview.json"))
    for p in sorted((PACK_ROOT / "kibana/alerting").glob("*.json")):
        res.append(Resource("alert_rule", scoped(load_json(p.relative_to(PACK_ROOT))["id"], space),
                            str(p.relative_to(PACK_ROOT))))
    return res


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
        return None if meta is None else meta.get("lagprof_fingerprint", meta.get("lagprof_version", ""))
    if r.kind == "role":
        code, out = client.kb("GET", f"/api/security/role/{r.name}", allow=(404,))
        if code == 404:
            return None
        md = out.get("metadata") or {}
        return md.get("lagprof_fingerprint", md.get("lagprof_version", ""))
    if r.kind == "alert_rule":
        code, out = client.kb("GET", f"{space_prefix(space)}/api/alerting/rule/{r.name}", allow=(404,))
        if code == 404:
            return None
        tags = [t for t in out.get("tags", []) if t.startswith("lagprof-version:")]
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


# Indices whose documents carry lagprof.space.id. Stream stats and host lag come from
# raw logs, which aren't divided by space, so a per-space dashboard shows them unfiltered.
SPACE_SCOPED_INDICES = ("lagprof-results", "lagprof-rules", "lagprof-runs")


def scope_query(esql: str, space: str) -> str:
    """Add a space filter after the FROM line of a query on a space-scoped index."""
    first, sep, rest = esql.partition("\n")
    m = re.match(r"\s*FROM\s+(\S+)\s*$", first)
    if not m or m.group(1) not in SPACE_SCOPED_INDICES:
        return esql
    return f'{first}\n| WHERE lagprof.space.id == "{space}"{sep}{rest}'


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
        esql = body["params"]["esqlQuery"]["esql"]
        first, _, rest = esql.partition("\n")
        body["params"]["esqlQuery"]["esql"] = f'{first}\n| WHERE lagprof.space.id == "{space}"\n{rest}'
        body["name"] = f"{body['name']} ({space})"
    return body



LOOKUP_INDEX = "lagprof-rule-streams"
LOOKUP_TEMPLATE = "elasticsearch/index-templates/lagprof-rule-streams.json"


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


def lookup_template_fields() -> set[str]:
    return mapped_fields(load_json(LOOKUP_TEMPLATE)["template"]["mappings"])
