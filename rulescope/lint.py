"""Offline checks of the pack files."""

from __future__ import annotations

import re

from . import resources
from .core import FORBIDDEN_SETTING_KEYS, PACK_ROOT, VERSION, ConfigError
from .report import Reporter
from .resources import LOOKUP_INDEX, load_json, pack_resources, render_module_workflow

# A bulk body key can name a KEEP column by its last part (rulescope.rule.id -> "id"),
# or by the end of it ("start" for window_start). These columns may also fill
# "@timestamp".
TIMESTAMP_COLUMNS = {"window_start"}


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


def keep_columns(query: str) -> list[str] | None:
    """The columns of a query's last KEEP, in order; None if it has no KEEP."""
    lines = query.splitlines()
    cols = None
    for i, line in enumerate(lines):
        m = re.match(r"\s*\|\s*KEEP\s+(.*)$", line)
        if not m:
            continue
        text = m.group(1)
        for nxt in lines[i + 1:]:
            s = nxt.strip()
            if not s or s.startswith("|") or s.startswith("//") or re.match(r"[\w-]+:", s):
                break
            text += " " + s
        cols = [c.strip() for c in text.split(",") if c.strip()]
    return cols


def _steps(text: str) -> dict[str, str]:
    """Workflow step name -> the YAML from its '- name:' line to the next one."""
    parts = re.split(r"(?m)^\s*- name:\s*(\S+)\s*$", text)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def bulk_row_problems(text: str) -> list[str]:
    """Check each bulk body that loops over a query's rows against the query's KEEP.

    A row is a list, so row[3] is the fourth KEEP column. Reordering KEEP without the
    body silently writes values into the wrong fields; this catches that."""
    steps = _steps(text)
    problems = []
    loops = re.finditer(r"\{%\s*for\s+(\w+)\s+in\s+steps\.(\w+)\.output\.values\s*%\}(.*?)\{%\s*endfor\s*%\}",
                        text, re.S)
    for m in loops:
        var, step, body = m.groups()
        q = re.search(r"query:\s*\|\n(.*)", steps.get(step, ""), re.S)
        cols = keep_columns(q.group(1)) if q else None
        if not cols:
            problems.append(f"rows of {step} are written, but {step} has no query ending in KEEP")
            continue
        used = {int(i) for i in re.findall(rf"\b{var}\[(\d+)\]", body)}
        for i in sorted(used):
            if i >= len(cols):
                problems.append(f"{var}[{i}] from {step}: the query keeps only {len(cols)} columns")
        for i in sorted(set(range(len(cols))) - used):
            problems.append(f"{step} column {cols[i]} ({var}[{i}]) is never written")
        for key, i in re.findall(rf'"([\w@]+)"\s*:\s*\{{\{{\s*{var}\[(\d+)\]', body):
            i = int(i)
            if i >= len(cols):
                continue
            last = cols[i].rsplit(".", 1)[-1]
            ok = key == last or last.endswith("_" + key) or (key == "@timestamp" and last in TIMESTAMP_COLUMNS)
            if not ok:
                problems.append(f'"{key}" is written from {var}[{i}], which is {step} column {cols[i]}')
    return problems


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
        if r.kind == "index_template" and not is_lookup and "rulescope-common" not in body.get("composed_of", []):
            bad(r.kind, r.name, "does not use the rulescope-common component template")
        if r.kind == "index_template" and is_lookup and body.get("composed_of"):
            bad(r.kind, r.name, "lookup index must have its own mapping: every mapped field enters the join")
        if r.kind == "index_template" and "data_stream" in body:
            if not ((body.get("template") or {}).get("lifecycle") or {}).get("data_retention"):
                bad(r.kind, r.name, "data stream has no lifecycle data_retention")

    # A lookup field replaces any query column of the same name in LOOKUP JOIN.
    lag_esql = PACK_ROOT / "modules" / "lag" / "esql"
    clash = prejoin_columns((lag_esql / "miss-rate.esql").read_text()) & resources.lookup_template_fields()
    if clash:
        bad("esql", "miss-rate.esql", f"columns created before the join are also mapped in {LOOKUP_INDEX} "
            f"and would be replaced: {', '.join(sorted(clash))}")

    # Module workflows rendered from templates, in both scopes.
    rendered: dict[tuple[str, str], str] = {}
    for r in pack_resources():
        if r.kind != "workflow":
            continue
        for space in (None, "lint-check"):
            scope = f"space {space}" if space else "global"
            try:
                _, text = render_module_workflow(r, space, shared_stats=True)
            except ConfigError as err:
                bad("workflow", f"{r.name} ({scope})", str(err))
                continue
            rendered[(r.name, "space" if space else "global")] = text
            for msg in bulk_row_problems(text):
                bad("workflow", f"{r.name} ({scope})", msg)
            if "[[" in text or "#%" in text:
                bad("workflow", f"{r.name} ({scope})", "unrendered template markup left in the output")

    # The lag module's esql/ files are the readable copies of the queries in its workflow.
    for qfile in sorted(lag_esql.glob("*.esql")):
        q = normalize_ws(qfile.read_text())
        for scope in ("global", "space"):
            wf = normalize_ws(rendered.get(("rulescope-lag-hourly", scope), ""))
            expected = q
            if scope == "space" and qfile.stem == "miss-rate":
                expected = q.replace("| WHERE rulescope.rule.id IS NOT NULL",
                                     "| WHERE rulescope.space.id == ?space_id AND rulescope.rule.id IS NOT NULL")
            if expected not in wf:
                bad("esql", qfile.name, f"differs from the query in the lag workflow ({scope} scope)")

    if not problems:
        rep.line("ok", "lint", "pack", f"v{VERSION}")
    return 1 if problems else 0
