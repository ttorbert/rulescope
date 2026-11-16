"""The modules rulescope can install, and the order they install in.

Each module owns a set of resources (see resources.pack_resources). A module can
require others; requirements are installed first and removed last. The catalog
syncs every rule; the Ingest Lag Profiler still keeps its own rule inventory until
it moves onto the catalog (step 4 of rulescope-design.md), so for now it doesn't
require the catalog, and the default install is the lag module alone.
"""

from __future__ import annotations

from dataclasses import dataclass

from .core import ConfigError


@dataclass(frozen=True)
class Module:
    name: str
    title: str
    requires: tuple[str, ...] = ()
    # Every index the module writes matches this pattern.
    indices: str = ""
    # Workflow names start with this, in every scope.
    workflow_prefix: str = ""


MODULES: dict[str, Module] = {
    "catalog": Module("catalog", "Rule Catalog", indices="rulescope-catalog-*",
                      workflow_prefix="rulescope-catalog-"),
    "lag": Module("lag", "Ingest Lag Profiler", indices="lagprof-*", workflow_prefix="lagprof-hourly"),
}

DEFAULT_MODULES: tuple[str, ...] = ("lag",)


def parse_modules(text: str | None) -> tuple[str, ...]:
    """Turn 'lag,catalog' into module names, checked against MODULES."""
    names = [n.strip() for n in (text or "").split(",") if n.strip()]
    if not names:
        return DEFAULT_MODULES
    unknown = [n for n in names if n not in MODULES]
    if unknown:
        raise ConfigError(f"unknown module(s): {', '.join(unknown)}. Known: {', '.join(MODULES)}")
    return tuple(names)


def resolve(names: tuple[str, ...] | list[str]) -> list[Module]:
    """The modules to act on, with their requirements, in install order."""
    ordered: list[Module] = []
    seen: set[str] = set()

    def visit(name: str, chain: tuple[str, ...]) -> None:
        if name in seen:
            return
        if name in chain:
            raise ConfigError(f"module requirements loop: {' -> '.join(chain + (name,))}")
        if name not in MODULES:
            raise ConfigError(f"unknown module: {name}")
        for req in MODULES[name].requires:
            visit(req, chain + (name,))
        seen.add(name)
        ordered.append(MODULES[name])

    for n in names:
        visit(n, ())
    return ordered
