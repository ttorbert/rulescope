"""The modules rulescope can install, and the order they install in.

Each module owns a set of resources (see resources.pack_resources). A module can
require others; requirements are installed first and removed last. Today the only
module is the Ingest Lag Profiler. The catalog, coverage and schedule modules come
next (see rulescope-design.md).
"""

from __future__ import annotations

from dataclasses import dataclass

from .core import ConfigError


@dataclass(frozen=True)
class Module:
    name: str
    title: str
    requires: tuple[str, ...] = ()


MODULES: dict[str, Module] = {
    "lag": Module("lag", "Ingest Lag Profiler"),
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
