#!/usr/bin/env python3
"""Create a lab workflow owner: a user with only one module's workflow role, and an
API key created as that user.

    lab-owner.py                     lagprof-runner, role lagprof-workflow, build/lab-owner-key
    lab-owner.py --module catalog    rulescope-catalog-runner, role rulescope-catalog,
                                     build/lab-owner-key-catalog
    lab-owner.py --module catalog --role rulescope-catalog-soc   (a per-space copy's role)

Then `LAGPROF_WORKFLOW_API_KEY=$(cat build/lab-owner-key) python3 -m rulescope install ...`
(or RULESCOPE_CATALOG_WORKFLOW_API_KEY for the catalog) saves the workflows as that
user, so their scheduled runs use only that role's privileges. The role must exist,
so install the module once first. `make lab-owner` does it for the lag module. Lab only.
"""
import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lagprof  # noqa: E402

OWNERS = {"lag": ("lagprof-runner", "lagprof-workflow", "lab-owner-key"),
          "catalog": ("rulescope-catalog-runner", "rulescope-catalog", "lab-owner-key-catalog")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", choices=tuple(OWNERS), default="lag")
    ap.add_argument("--role", help="role to give the user (default: the module's global role)")
    args = ap.parse_args()
    user, role, key_name = OWNERS[args.module]
    role = args.role or role
    KEY_FILE = lagprof.PACK_ROOT / "build" / key_name
    cfg = lagprof.load_config("targets/docker.env")
    if cfg.target != "docker":
        print("Lab only.", file=sys.stderr)
        return 2
    admin = lagprof.Client(cfg)
    if KEY_FILE.exists():
        print(f"Owner key already exists: {KEY_FILE.relative_to(lagprof.PACK_ROOT)}")
        return 0
    pw = secrets.token_urlsafe(18)
    code, _ = admin.es("GET", f"/_security/user/{user}", allow=(404,))
    if code == 404:
        admin.es("PUT", f"/_security/user/{user}",
                 {"password": pw, "roles": [role], "full_name": f"rulescope {args.module} runner (lab)"})
    else:
        admin.es("PUT", f"/_security/user/{user}", {"password": pw, "roles": [role]})
    runner_cfg = lagprof.load_config("targets/docker.env")
    runner_cfg.username, runner_cfg.password = user, pw
    _, key = lagprof.Client(runner_cfg).es("POST", "/_security/api_key", {"name": f"{user}-owner-lab"})
    KEY_FILE.parent.mkdir(exist_ok=True)
    KEY_FILE.write_text(key["encoded"])
    KEY_FILE.chmod(0o600)
    print(f"Created {user} (role {role}) and its API key: {KEY_FILE.relative_to(lagprof.PACK_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
