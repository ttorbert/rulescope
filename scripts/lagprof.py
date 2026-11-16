#!/usr/bin/env python3
"""Compatibility wrapper: the installer now lives in the rulescope package.

    python3 -m rulescope ...        preferred
    python3 scripts/lagprof.py ...  still works, with the same commands and options

Lab scripts that `import lagprof` get the same names as before (Client, load_config,
PACK_ROOT and the rest), re-exported from the rulescope submodules. To replace a function
in a test, patch it in the submodule that defines it (for example rulescope.resources),
not here: this module only holds copies of the names.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rulescope import check, cli, client, config, core, install, lint, modules, report, resources  # noqa: E402


def _public(mod) -> dict:
    """Names a submodule defines itself: functions, classes and UPPER_CASE constants."""
    out = {}
    for name, obj in vars(mod).items():
        if name.startswith("_"):
            continue
        if getattr(obj, "__module__", None) == mod.__name__ or (name.isupper() and not callable(obj)):
            out[name] = obj
    return out


for _m in (core, config, client, report, check, modules, resources, install, lint, cli):
    globals().update(_public(_m))

if __name__ == "__main__":
    sys.exit(cli.main())
