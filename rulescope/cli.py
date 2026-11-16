"""Command line: check, install, status, uninstall, lint, render-workflow."""

from __future__ import annotations

import argparse
import sys

from .check import run_check
from .client import Client, HttpError
from .config import load_config
from .core import PACK_ROOT, TARGETS, VERSION, WORKFLOW_FILES, ConfigError
from .install import InstallOptions, install, status, uninstall
from .lint import lint
from .modules import DEFAULT_MODULES, MODULES, parse_modules, resolve
from .report import Reporter
from . import render
from .resources import pack_resources, render_module_workflow, render_workflow


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="rulescope", description="rulescope installer: a rule catalog and report "
                                 f"modules for Elastic Security. Modules: {', '.join(MODULES)}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def conn(p):
        p.add_argument("--target-file", help="KEY=VALUE file with connection settings")
        p.add_argument("--target", choices=TARGETS, help="override LAGPROF_TARGET")

    def mods(p):
        p.add_argument("--modules", default=",".join(DEFAULT_MODULES),
                       help=f"comma-separated modules (default {','.join(DEFAULT_MODULES)}; known: {', '.join(MODULES)})")

    def wf(p):
        p.add_argument("--workflow", choices=tuple(WORKFLOW_FILES), default="all-spaces")
        p.add_argument("--space", help="space ID (per-space workflow)")
        p.add_argument("--shared-stats", action="store_true",
                       help="this per-space copy writes stream stats and host lag")

    p = sub.add_parser("check", help="check the target can run the profiler")
    conn(p)
    p.add_argument("--fix-license", action="store_true", help="start a trial (docker only)")

    p = sub.add_parser("install", help="install or update the pack")
    conn(p)
    mods(p)
    wf(p)
    p.add_argument("--no-import-workflow", dest="import_workflow", action="store_false",
                   help="only write the workflow YAML to build/ for a manual import")
    p.add_argument("--force", action="store_true", help="reinstall even if versions match")

    p = sub.add_parser("status", help="show installed versions")
    conn(p)
    mods(p)
    p.add_argument("--space")

    p = sub.add_parser("uninstall", help="remove the pack")
    conn(p)
    mods(p)
    p.add_argument("--space")
    p.add_argument("--delete-data", action="store_true", help="also delete the modules' data (lagprof-*, rulescope-catalog-*)")
    p.add_argument("--yes", action="store_true", help="don't ask before deleting data")

    sub.add_parser("lint", help="offline checks of the pack files")

    p = sub.add_parser("render", help="regenerate kibana/workflows/*.yaml from the workflow template")
    p.add_argument("--check", action="store_true", help="only report files that are out of date")

    p = sub.add_parser("render-workflow", help="write ready-to-import workflow YAML to build/")
    mods(p)
    wf(p)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rep = Reporter()
    try:
        if args.cmd == "lint":
            return lint(rep)
        if args.cmd == "render":
            stale = render.stale_workflows() if args.check else render.write_workflows()
            for path in stale:
                rep.line("stale" if args.check else "written", "workflow", path)
            if not stale:
                rep.line("ok", "render", "workflows", "match the template")
            return 1 if (args.check and stale) else 0
        if args.cmd == "render-workflow":
            space = args.space if args.workflow == "per-space" else None
            if args.workflow == "per-space" and not space:
                raise ConfigError("per-space mode needs --space")
            outputs = []
            for r in pack_resources(space, parse_modules(args.modules)):
                if r.kind == "workflow":
                    outputs.append(render_workflow(args.workflow, args.space, args.shared_stats) if r.module == "lag"
                                   else render_module_workflow(r, space))
            for fname, text in outputs:
                out = PACK_ROOT / "build" / fname
                out.parent.mkdir(exist_ok=True)
                out.write_text(text)
                rep.line("written", "workflow", f"build/{fname}")
            return 0

        # Checked before connecting, so a typo fails fast.
        modules = parse_modules(getattr(args, "modules", None))
        titles = ", ".join(m.title for m in resolve(modules))
        cfg = load_config(args.target_file, target_override=args.target)
        client = Client(cfg)
        rep.note(f"rulescope v{VERSION} [{titles}] -> {cfg.target} ({cfg.es_url})")

        if args.cmd == "check":
            res = run_check(client, rep, fix_license=args.fix_license)
            return 1 if res.blocking else 0
        if args.cmd == "install":
            if args.workflow == "per-space" and not args.space:
                raise ConfigError("per-space mode needs --space")
            res = run_check(client, rep, fix_license=cfg.target == "docker")
            if res.blocking:
                rep.note("Not installing: fix the BLOCKED items above first.")
                return 1
            rep.note("")
            return install(client, rep, InstallOptions(
                workflow=args.workflow, space=args.space, shared_stats=args.shared_stats,
                import_workflow=args.import_workflow, force=args.force, modules=modules))
        if args.cmd == "status":
            return status(client, rep, args.space, modules)
        if args.cmd == "uninstall":
            if args.delete_data and not args.yes:
                answer = input("Delete lagprof-* data, including history? If other profiler workflows "
                               "are installed, only this space's documents are deleted. Type 'delete': ")
                if answer.strip() != "delete":
                    rep.note("Cancelled.")
                    return 1
            return uninstall(client, rep, args.delete_data, args.space, modules)
    except ConfigError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    except HttpError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0
