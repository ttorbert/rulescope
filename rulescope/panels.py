"""Helpers for writing dashboards as Kibana Dashboards API bodies (9.5+).

Every panel reads rulescope indices with ES|QL. A module's dashboards/build.py lists
its queries and panels with these helpers and writes the JSON the installer sends.
(The lag dashboard's builder predates this module and keeps its own copy until the
lag module moves to modules/lag/.)
"""

from __future__ import annotations


def esql(query: str) -> dict:
    return {"type": "esql", "query": query}


def g(x: int, y: int, w: int, h: int) -> dict:
    return {"x": x, "y": y, "w": w, "h": h}


class Panels:
    """Panel builders that share one set of column labels."""

    def __init__(self, labels: dict[str, str]):
        self.labels = labels

    def col(self, name: str, width: int | None = None) -> dict:
        c = {"column": name, "label": self.labels.get(name, name)}
        if width:
            c["width"] = width  # pixels; keeps long values from being cut off
        return c

    def table(self, title: str, query: str, rows: list[str], metrics: list[str], grid: dict,
              description: str = "", widths: dict[str, int] | None = None) -> dict:
        widths = widths or {}
        cfg = {"type": "data_table", "title": title, "data_source": esql(query),
               "rows": [self.col(c, widths.get(c)) for c in rows],
               "metrics": [self.col(c, widths.get(c)) for c in metrics]}
        if description:
            cfg["description"] = description
        return {"type": "vis", "grid": grid, "config": cfg}

    def line(self, title: str, query: str, x: str, y: str, breakdown: str, grid: dict,
             description: str = "") -> dict:
        cfg = {"type": "xy", "title": title, "layers": [{
            "type": "line", "data_source": esql(query), "x": self.col(x),
            "y": [self.col(y)], "breakdown_by": self.col(breakdown)}]}
        if description:
            cfg["description"] = description
        return {"type": "vis", "grid": grid, "config": cfg}


def markdown(content: str, grid: dict) -> dict:
    return {"type": "markdown", "grid": grid, "config": {"content": content}}


def check_queries(queries: dict[str, str], target_file: str, untimed: tuple[str, ...] = ()) -> int:
    """Run each query against a cluster, the way a panel would over the last 7 days.

    Queries whose FROM names an index in `untimed` run without the time filter.
    Returns the number of failures."""
    from .client import Client
    from .config import load_config
    c = Client(load_config(target_file))
    bad = 0
    for name, q in queries.items():
        first = q.split("\n", 1)[0]
        timed = q if any(i in first for i in untimed) else q.replace(
            "\n", "\n| WHERE @timestamp >= NOW() - 7 days\n", 1)
        code, out = c.es("POST", "/_query", {"query": timed}, allow=(400,))
        if code >= 400:
            bad += 1
            print(f"FAIL {name}: {(out or {}).get('error', {}).get('reason', out)}")
        else:
            print(f"ok   {name}: {len(out['values'])} rows, columns {[col['name'] for col in out['columns']]}")
    return bad
