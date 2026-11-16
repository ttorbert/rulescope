"""Offline checks of the pack files."""

from __future__ import annotations

import re

from . import resources
from .core import FORBIDDEN_SETTING_KEYS, PACK_ROOT, VERSION, WORKFLOW_FILES, ConfigError
from .report import Reporter
from .resources import LOOKUP_INDEX, load_json, pack_resources, render_workflow


def prejoin_columns(query: str) -> set[str]:
    """Column names a query creates (name = ...) before its LOOKUP JOIN."""
    head = query.split("LOOKUP JOIN", 1)[0]
    head = re.sub(r"//[^\n]*", "", head)
    return set(re.findall(r"(?<![=!<>\w.])([A-Za-z_][\w.]*)\s*=(?!=)", head))


def _walk_keys(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            yield path
            yield from _walk_keys(v, path)


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def lint(rep: Reporter) -> int:
    problems = 0

    def bad(kind, name, msg):
        nonlocal problems
        problems += 1
        rep.line("PROBLEM", kind, name, msg)

    for r in pack_resources():
        if not r.path or not r.path.endswith(".json"):
            continue
        try:
            body = load_json(r.path)
        except (OSError, ValueError) as err:
            bad(r.kind, r.name, f"invalid JSON: {err}")
            continue
        if r.kind in ("component_template", "index_template"):
            settings = (body.get("template") or {}).get("settings") or {}
            for key in _walk_keys(settings):
                if any(key.endswith(f) or f"{f}." in key for f in FORBIDDEN_SETTING_KEYS) or "lifecycle.name" in key:
                    bad(r.kind, r.name, f"setting {key} is rejected on Serverless")
        is_lookup = (((body.get("template") or {}).get("settings") or {}).get("index") or {}).get("mode") == "lookup"
        if r.kind == "index_template" and not is_lookup and "lagprof-common" not in body.get("composed_of", []):
            bad(r.kind, r.name, "does not use the lagprof-common component template")
        if r.kind == "index_template" and is_lookup and body.get("composed_of"):
            bad(r.kind, r.name, "lookup index must have its own mapping: every mapped field enters the join")
        if r.kind == "index_template" and "data_stream" in body:
            if not ((body.get("template") or {}).get("lifecycle") or {}).get("data_retention"):
                bad(r.kind, r.name, "data stream has no lifecycle data_retention")

    # A lookup field replaces any query column of the same name in LOOKUP JOIN.
    clash = prejoin_columns((PACK_ROOT / "esql" / "miss-rate.esql").read_text()) & resources.lookup_template_fields()
    if clash:
        bad("esql", "miss-rate.esql", f"columns created before the join are also mapped in {LOOKUP_INDEX} "
            f"and would be replaced: {', '.join(sorted(clash))}")

    # The esql/ files are the readable copies of the queries inside the workflows.
    workflows = {m: normalize_ws((PACK_ROOT / p).read_text()) for m, p in WORKFLOW_FILES.items()}
    for qfile in sorted((PACK_ROOT / "esql").glob("*.esql")):
        q = normalize_ws(qfile.read_text())
        for mode, wf in workflows.items():
            expected = q
            if mode == "per-space" and qfile.stem == "miss-rate":
                expected = q.replace("| WHERE lagprof.rule.id IS NOT NULL",
                                     "| WHERE lagprof.space.id == ?space_id AND lagprof.rule.id IS NOT NULL")
            if expected not in wf:
                bad("esql", qfile.name, f"differs from the query in the {mode} workflow")

    for mode in WORKFLOW_FILES:
        try:
            render_workflow(mode, "lint-check", True)
        except ConfigError as err:
            bad("workflow", mode, str(err))

    if not problems:
        rep.line("ok", "lint", "pack", f"v{VERSION}")
    return 1 if problems else 0
