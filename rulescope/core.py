"""Shared constants and small helpers."""

from __future__ import annotations

import re
from pathlib import Path

PACK_ROOT = Path(__file__).resolve().parent.parent
VERSION = (PACK_ROOT / "VERSION").read_text().strip()

TARGETS = ("docker", "self-managed", "cloud", "serverless")
MIN_VERSION = (9, 5, 6)
TESTED_VERSION = "9.5.6"

# Workflows management API (confirmed on 9.5.6):
#   GET  /api/workflows                       list
#   POST /api/workflows                       {"workflows": [{"yaml": ...}]}; id = workflow name
#   GET|PUT|DELETE /api/workflows/workflow/<id>   PUT takes {"yaml": ...}
#   POST /api/workflows/workflow/<id>/run     {"inputs": {}}
#   GET  /api/workflows/executions/<execution id>
# A newly created scheduled workflow runs once straight away.
WORKFLOWS_API = "/api/workflows"
# Serverless Kibana requires a version header on public APIs.
SERVERLESS_API_VERSION = "2023-10-31"

# Settings that Serverless rejects. Pack templates must not use them.
FORBIDDEN_SETTING_KEYS = ("number_of_shards", "number_of_replicas", "lifecycle.name",
                          "lifecycle.rollover_alias", "routing.allocation")

# Installer workflow modes (the --workflow option): one global copy of each module,
# or one copy per space.
WORKFLOW_MODES = ("all-spaces", "per-space")
SPACE_ID_RE = re.compile(r"^[a-z0-9_-]+$")


class ConfigError(Exception):
    pass


def space_prefix(space: str | None) -> str:
    return "" if not space or space == "default" else f"/s/{space}"


def version_tuple(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v.split("-")[0])
    return tuple(int(n) for n in nums[:3])
