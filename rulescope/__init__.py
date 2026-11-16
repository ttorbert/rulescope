"""rulescope: a detection rule catalog and report modules for Elastic Security.

Everything runs inside Elastic (Workflows, ES|QL, dashboards, alerts). This package is
the installer, standard library only:

    python3 -m rulescope check     --target-file targets/docker.env
    python3 -m rulescope install   --target-file targets/docker.env [--modules lag]
                                   [--workflow per-space --space soc --shared-stats]
    python3 -m rulescope status    --target-file targets/docker.env
    python3 -m rulescope uninstall --target-file targets/docker.env [--space soc] [--delete-data --yes]
    python3 -m rulescope lint
    python3 -m rulescope render-workflow --workflow per-space --space soc

Connection settings come from a target file (KEY=VALUE lines) and the environment.
Environment variables win over the file. Values may use ${VAR}.

    LAGPROF_TARGET  docker | self-managed | cloud | serverless
    ES_URL, KIBANA_URL   or   CLOUD_ID (cloud)
    ES_API_KEY           or   ES_USERNAME + ES_PASSWORD
    CA_CERT              path to a CA certificate (optional)
    VERIFY_TLS           true (default) | false
    LAGPROF_WORKFLOW_API_KEY   API key of a dedicated user with the workflow role.
                         The workflow is saved with it, so scheduled runs use that
                         user's (limited) privileges. Without it they use the installer's.

The lab scripts import the scripts/lagprof.py compatibility wrapper, which re-exports
the submodules' public names in one namespace.
"""

from __future__ import annotations

from .core import VERSION  # noqa: F401
