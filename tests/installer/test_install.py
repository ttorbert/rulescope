"""Unit tests for the rulescope installer, through the scripts/lagprof.py compatibility wrapper. No cluster needed: a small fake Elasticsearch
and Kibana server answers the calls the installer makes."""

from __future__ import annotations

import base64
import io
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import lagprof  # noqa: E402
from rulescope import modules, resources  # noqa: E402


# --------------------------------------------------------------------------------------
# Fake cluster
# --------------------------------------------------------------------------------------

class FakeCluster:
    def __init__(self, version="9.5.6", flavor="default", license_type="trial"):
        self.version, self.flavor, self.license_type = version, flavor, license_type
        self.store: dict[str, dict] = {}       # path -> stored body
        self.data_streams_in_use = {"rulescope-lag-results"}
        self.requests: list[tuple[str, str]] = []
        self.workflows_api = True
        self.workflows: dict[str, dict] = {}
        self.spaces = ["default", "soc", "team-b"]


def make_handler(fake: FakeCluster):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                return json.loads(raw) if raw else None
            except ValueError:
                return raw

        def _send(self, code, obj=None):
            payload = json.dumps(obj if obj is not None else {}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def handle_any(self, method):
            path = self.path.split("?")[0]
            body = self._body() if method in ("PUT", "POST") else None
            fake.requests.append((method, self.path))
            req_space = "default"
            if path.startswith("/s/"):  # Kibana space prefix
                req_space = path.split("/", 3)[2]
                path = "/" + path.split("/", 3)[3]
            if path == "/api/spaces/space":
                return self._send(200, [{"id": s} for s in fake.spaces])

            if path == "/" and method == "GET":
                return self._send(200, {"version": {"number": fake.version, "build_flavor": fake.flavor}})
            if path == "/_license":
                return self._send(200, {"license": {"type": fake.license_type, "status": "active"}})
            if path == "/_license/start_trial":
                fake.license_type = "trial"
                return self._send(200, {"acknowledged": True, "trial_was_started": True})
            if path == "/_security/_authenticate":
                auth = self.headers.get("Authorization", "")
                return self._send(200, {"username": "lagprof-owner" if auth.startswith("ApiKey owner") else "elastic"})
            if path.startswith("/api/workflows") and method in ("POST", "PUT"):
                fake.workflow_auth = self.headers.get("Authorization", "")
            if path == "/api/status":
                return self._send(200, {"status": {"overall": {"level": "available"}}})
            if path == "/api/workflows":
                if not fake.workflows_api:
                    return self._send(404, {})
                if method == "POST":
                    import re as _re
                    created = []
                    for w in body["workflows"]:
                        name = _re.search(r"(?m)^name:\s*(\S+)", w["yaml"]).group(1)
                        # Like Kibana: IDs of deleted workflows aren't reused.
                        fake.id_counter = getattr(fake, "id_counter", 0) + 1
                        wid = f"{name}-{fake.id_counter}"
                        fake.workflows[wid] = {"id": wid, "name": name, "yaml": w["yaml"], "valid": True,
                                               "space": req_space}
                        created.append(fake.workflows[wid])
                    return self._send(200, {"created": created})
                return self._send(200, {"results": [w for w in fake.workflows.values()
                                                    if w.get("space", "default") == req_space]})
            if path.startswith("/api/workflows/workflow/"):
                wid = path.rsplit("/", 1)[1]
                if wid not in fake.workflows:
                    return self._send(404, {})
                if method == "PUT":
                    fake.workflows[wid].update(yaml=body["yaml"])
                elif method == "DELETE":
                    del fake.workflows[wid]
                    return self._send(200, {})
                return self._send(200, fake.workflows[wid])
            if path.endswith("/_delete_by_query") and method == "POST":
                fake.delete_by_query = getattr(fake, "delete_by_query", []) + [(path, body)]
                return self._send(200, {"deleted": 0})
            if path == "/_query":
                return self._send(200, {"columns": [], "values": [[1000, 990]]})
            if path == "/rulescope-lag-runs/_search":
                return self._send(404, {"error": "index_not_found_exception"})
            if path.startswith("/_data_stream/") and "*" not in path:
                if method == "PUT":
                    fake.store[path] = {}
                    return self._send(200, {"acknowledged": True})
                if method == "GET":
                    return self._send(200, {"data_streams": [{}]}) if path in fake.store else self._send(404, {})
            if path.startswith(("/rulescope-lag-*", "/rulescope-catalog-*")) and path.endswith("/_mapping") and method == "PUT":
                fake.store[path] = body  # mapping update on a module's existing indices
                return self._send(200, {"acknowledged": True})
            if path.endswith("/_mapping") and path[:-len("/_mapping")] in fake.store:
                idx = path[:-len("/_mapping")]
                return self._send(200, {idx.lstrip("/"): {"mappings": fake.store[idx]}})
            if path in ("/rulescope-catalog-rules", "/rulescope-catalog-rule-streams"):
                if method == "PUT":
                    # Like Elasticsearch, a new index takes its mapping from the index template.
                    lookup = resources.LOOKUP_INDICES.get(path.lstrip("/"))
                    fake.store[path] = lagprof.load_json(lookup)["template"]["mappings"] if lookup else {}
                    fake.index_creates = getattr(fake, "index_creates", 0) + 1
                    return self._send(200, {"acknowledged": True})
                if method == "DELETE":
                    fake.store.pop(path, None)
                    return self._send(200, {"acknowledged": True})
                return self._send(200, {}) if path in fake.store else self._send(404, {})

            if path.startswith("/api/dashboards/"):
                key = path
                if method == "PUT":
                    fake.store[key] = body
                    return self._send(200, {"id": path.rsplit("/", 1)[1], "data": body})
                if method == "DELETE":
                    return self._send(204 if fake.store.pop(key, None) is not None else 404, {})
                return self._send(200, {"data": fake.store[key]}) if key in fake.store else self._send(404, {})
            for prefix, wrap in (("/_component_template/", "component_templates"),
                                 ("/_index_template/", "index_templates"),
                                 ("/_ingest/pipeline/", None),
                                 ("/api/security/role/", None),
                                 ("/api/alerting/rule/", None)):
                if path.startswith(prefix):
                    name = path[len(prefix):]
                    key = prefix + name
                    if method in ("PUT", "POST"):
                        fake.store[key] = body
                        return self._send(200, {"acknowledged": True})
                    if method == "DELETE":
                        if key not in fake.store:
                            return self._send(404, {})
                        if prefix == "/_index_template/" and name in fake.data_streams_in_use:
                            return self._send(400, {"error": "in use"})
                        del fake.store[key]
                        return self._send(200, {"acknowledged": True})
                    if key not in fake.store:
                        return self._send(404, {})
                    stored = fake.store[key]
                    if wrap == "component_templates":
                        return self._send(200, {wrap: [{"name": name, "component_template": stored}]})
                    if wrap == "index_templates":
                        return self._send(200, {wrap: [{"name": name, "index_template": stored}]})
                    if prefix == "/_ingest/pipeline/":
                        return self._send(200, {name: stored})
                    return self._send(200, stored)
            return self._send(404, {"error": f"fake: no route {method} {path}"})

        def do_GET(self):
            self.handle_any("GET")

        def do_PUT(self):
            self.handle_any("PUT")

        def do_POST(self):
            self.handle_any("POST")

        def do_DELETE(self):
            self.handle_any("DELETE")

    return Handler


class FakeServerCase(unittest.TestCase):
    fake_kwargs: dict = {}
    target = "docker"

    def setUp(self):
        self.fake = FakeCluster(**self.fake_kwargs)
        self.server = HTTPServer(("127.0.0.1", 0), make_handler(self.fake))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        url = f"http://127.0.0.1:{self.server.server_port}"
        self.cfg = lagprof.Config(target=self.target, es_url=url, kibana_url=url,
                                  username="elastic", password="pw", api_key="k" if self.target in ("cloud", "serverless") else "")
        self.client = lagprof.Client(self.cfg)
        self.out = io.StringIO()
        self.rep = lagprof.Reporter(self.out)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def statuses(self, kind=None):
        return [(s, k, n) for s, k, n, _ in self.rep.lines if kind is None or k == kind]


# --------------------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------------------

class ConfigTests(unittest.TestCase):
    def test_env_parsing_comments_quotes_and_expansion(self):
        text = """
        # comment
        LAGPROF_TARGET=docker
        export ES_URL="https://localhost:9200"
        ES_PASSWORD=${ELASTIC_PASSWORD}   # from the lab .env
        LITERAL='${NOT_EXPANDED}'
        CA_CERT=scripts/lab/certs/ca/ca.crt
        """
        vals = lagprof.parse_env_text(text, {"ELASTIC_PASSWORD": "s3cret"})
        self.assertEqual(vals["ES_URL"], "https://localhost:9200")
        self.assertEqual(vals["ES_PASSWORD"], "s3cret")
        self.assertEqual(vals["LITERAL"], "${NOT_EXPANDED}")

    def test_cloud_id_decoding(self):
        encoded = base64.b64encode(b"us-east-1.aws.found.io$abc123$def456").decode()
        es, kb = lagprof.decode_cloud_id(f"my-deployment:{encoded}")
        self.assertEqual(es, "https://abc123.us-east-1.aws.found.io")
        self.assertEqual(kb, "https://def456.us-east-1.aws.found.io")

    def test_scope_query_adds_space_filter_after_from(self):
        q = "FROM rulescope-lag-results\n| STATS n = COUNT(*)"
        self.assertEqual(lagprof.scope_query(q, "soc"),
                         'FROM rulescope-lag-results\n| WHERE rulescope.space.id == "soc"\n| STATS n = COUNT(*)')
        q = "FROM rulescope-catalog-syncs\n| STATS n = COUNT(*)"
        self.assertEqual(lagprof.scope_query(q, "soc"),
                         'FROM rulescope-catalog-syncs\n| WHERE rulescope.scope == "space:soc"\n| STATS n = COUNT(*)')
        shared = "FROM rulescope-lag-host-lag\n| STATS n = COUNT(*)"
        self.assertEqual(lagprof.scope_query(shared, "soc"), shared)

    def test_cloud_id_with_port(self):
        encoded = base64.b64encode(b"example.com:9243$es$kb").decode()
        es, kb = lagprof.decode_cloud_id(f"x:{encoded}")
        self.assertEqual(es, "https://es.example.com:9243")
        self.assertEqual(kb, "https://kb.example.com:9243")

    def test_bad_cloud_id(self):
        with self.assertRaises(lagprof.ConfigError):
            lagprof.decode_cloud_id("x:not-base64-$$$")

    def test_cloud_target_requires_api_key(self):
        env = {"LAGPROF_TARGET": "cloud", "ES_URL": "https://e", "KIBANA_URL": "https://k",
               "ES_USERNAME": "u", "ES_PASSWORD": "p"}
        with self.assertRaises(lagprof.ConfigError):
            lagprof.load_config(None, env)

    def test_cloud_id_fills_urls(self):
        encoded = base64.b64encode(b"host.io$es1$kb1").decode()
        cfg = lagprof.load_config(None, {"LAGPROF_TARGET": "cloud", "CLOUD_ID": f"d:{encoded}",
                                         "ES_API_KEY": "abc"})
        self.assertEqual(cfg.es_url, "https://es1.host.io")
        self.assertEqual(cfg.kibana_url, "https://kb1.host.io")
        self.assertEqual(cfg.auth_header, "ApiKey abc")

    def test_invalid_target(self):
        with self.assertRaises(lagprof.ConfigError):
            lagprof.load_config(None, {"LAGPROF_TARGET": "laptop"})

    def test_docker_target_file_loads(self):
        cfg = lagprof.load_config("targets/docker.env", {"ELASTIC_PASSWORD": "pw"})
        self.assertEqual(cfg.target, "docker")
        self.assertEqual(cfg.password, "pw")
        self.assertTrue(cfg.ca_cert.endswith("scripts/lab/certs/ca/ca.crt"))




def lag_workflow() -> "resources.Resource":
    return next(r for r in resources.module_resources("lag", None) if r.kind == "workflow")


class WorkflowRenderTests(unittest.TestCase):
    def test_lag_global(self):
        name, text = resources.render_module_workflow(lag_workflow(), None)
        self.assertEqual(name, "rulescope-lag-hourly.yaml")
        self.assertIn("name: rulescope-lag-hourly\n", text)
        self.assertIn("scope: global", text)
        self.assertNotIn("write_shared_stats", text)
        # Rules come from the catalog: the lag workflow never calls the rules API.
        self.assertNotIn("detection_engine", text)
        self.assertIn("LOOKUP JOIN rulescope-catalog-rule-streams", text)

    def test_lag_per_space_fills_settings(self):
        name, text = resources.render_module_workflow(lag_workflow(), "soc", shared_stats=False)
        self.assertEqual(name, "rulescope-lag-hourly-space-soc.yaml")
        self.assertIn("name: rulescope-lag-hourly-space-soc", text)
        self.assertRegex(text, r"(?m)^\s*space_id: soc\b")
        self.assertRegex(text, r"(?m)^\s*scope: space:soc\b")
        self.assertRegex(text, r"(?m)^\s*write_shared_stats: false\b")
        _, text = resources.render_module_workflow(lag_workflow(), "soc", shared_stats=True)
        self.assertRegex(text, r"(?m)^\s*write_shared_stats: true\b")

    def test_lag_checks_catalog_freshness_first(self):
        for space in (None, "soc"):
            _, text = resources.render_module_workflow(lag_workflow(), space)
            steps = lint_steps(text)
            self.assertEqual(steps[0], "catalog")
            self.assertIn("record_catalog_stale", steps)
            self.assertIn("status: catalog_stale", text)
        # The catalog in the same scope.
        self.assertIn('- term: { rulescope.scope: "{{ consts.scope }}" }', text)

    def test_needs_valid_space(self):
        for bad in ("Bad Space", "../x"):
            with self.assertRaises(lagprof.ConfigError):
                resources.render_module_workflow(lag_workflow(), bad)


def lint_steps(text: str) -> list[str]:
    import re
    return re.findall(r"(?m)^\s*- name:\s*(\S+)\s*$", text.split("\nsteps:\n", 1)[1])


class LintTests(unittest.TestCase):
    def test_pack_passes_lint(self):
        rep = lagprof.Reporter(io.StringIO())
        self.assertEqual(lagprof.lint(rep), 0, rep.stream.getvalue())

    def test_lint_catches_lookup_column_clash(self):
        real = resources.lookup_template_fields
        try:
            resources.lookup_template_fields = lambda index=resources.LOOKUP_INDEX: real(index) | {"stream_p99"}
            rep = lagprof.Reporter(io.StringIO())
            self.assertEqual(lagprof.lint(rep), 1)
            self.assertIn("stream_p99", rep.stream.getvalue())
        finally:
            resources.lookup_template_fields = real

    def test_prejoin_columns(self):
        q = "FROM x | EVAL a = 1, b == 2 | STATS n = COUNT(*) BY c // d = e\n| LOOKUP JOIN y ON c | EVAL late = 1"
        self.assertEqual(lagprof.prejoin_columns(q), {"a", "n"})

    def test_role_rendering(self):
        role = lagprof.render_role(["soc"], None, "modules/lag/elasticsearch/security/rulescope-lag-role.json")
        self.assertEqual(role["kibana"][0]["spaces"], ["soc"])
        self.assertNotIn("_rulescope_notes", role)
        self.assertEqual(role["metadata"]["rulescope_version"], lagprof.VERSION)
        # The lag module reads the catalog and never the rules API.
        names = {n for i in role["elasticsearch"]["indices"] for n in i["names"]}
        self.assertEqual(names, {"logs-*", "rulescope-catalog-*", "rulescope-lag-*"})
        self.assertNotIn("securitySolutionRulesV4", role["kibana"][0]["feature"])

    def test_version_tuple(self):
        self.assertEqual(lagprof.version_tuple("9.5.6"), (9, 5, 6))
        self.assertEqual(lagprof.version_tuple("9.6.0-SNAPSHOT"), (9, 6, 0))
        self.assertLess(lagprof.version_tuple("9.4.2"), lagprof.MIN_VERSION)


class CheckTests(FakeServerCase):
    def test_docker_basic_license_starts_trial_when_fixing(self):
        self.fake.license_type = "basic"
        res = lagprof.run_check(self.client, self.rep, fix_license=True)
        self.assertEqual(res.blocking, [])
        self.assertIn(("fixed", "license", "trial"), self.statuses())

    def test_basic_license_blocks_without_fix(self):
        self.fake.license_type = "basic"
        res = lagprof.run_check(self.client, self.rep)
        self.assertTrue(any("license" in b for b in res.blocking))

    def test_old_version_blocks(self):
        self.fake.version = "9.4.1"
        res = lagprof.run_check(self.client, self.rep)
        self.assertTrue(any("older" in b for b in res.blocking))

    def test_missing_workflows_blocks(self):
        self.fake.workflows_api = False
        res = lagprof.run_check(self.client, self.rep)
        self.assertTrue(any("Workflows" in b for b in res.blocking))


class ServerlessCheckTests(FakeServerCase):
    fake_kwargs = {"version": "8.11.0", "flavor": "serverless", "license_type": "basic"}
    target = "serverless"

    def test_serverless_skips_version_and_license(self):
        res = lagprof.run_check(self.client, self.rep)
        self.assertEqual(res.blocking, [])
        self.assertIn(("skipped", "license", "serverless"), self.statuses())
        self.assertFalse(any(p == "/_license" for _, p in self.fake.requests))


class InstallTests(FakeServerCase):
    """The default install: the catalog and the lag module."""

    def test_install_is_idempotent(self):
        opts = lagprof.InstallOptions()
        self.assertEqual(lagprof.install(self.client, self.rep, opts), 0, self.out.getvalue())
        first = {s for s, k, n in self.statuses() if s != "WARN"}
        self.assertEqual(first, {"created"})
        self.assertEqual(sorted(w["name"] for w in self.fake.workflows.values()),
                         ["rulescope-catalog-snapshot", "rulescope-catalog-sync", "rulescope-lag-hourly"])

        self.rep.lines.clear()
        self.assertEqual(lagprof.install(self.client, self.rep, opts), 0)
        second = {s for s, k, n in self.statuses() if s != "WARN"}
        self.assertEqual(second, {"unchanged"})

    def test_lag_alone_installs_the_catalog_first(self):
        opts = lagprof.InstallOptions(modules=("lag",))
        self.assertEqual(lagprof.install(self.client, self.rep, opts), 0, self.out.getvalue())
        names = [n for s, k, n in self.statuses() if k == "workflow" and s != "WARN"]
        self.assertEqual(names, ["rulescope-catalog-sync", "rulescope-catalog-snapshot", "rulescope-lag-hourly"])

    def test_templates_get_version_meta_and_no_notes(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        tmpl = self.fake.store["/_index_template/rulescope-lag-results"]
        self.assertEqual(tmpl["_meta"]["rulescope_version"], lagprof.VERSION)
        self.assertEqual(tmpl["composed_of"], ["rulescope-common", "rulescope-lag"])
        role = self.fake.store["/api/security/role/rulescope-lag"]
        self.assertEqual(role["kibana"][0]["spaces"], ["*"])
        rule = self.fake.store["/api/alerting/rule/rulescope-lag-miss-rate"]
        self.assertNotIn("_rulescope_notes", rule)
        self.assertNotIn("id", rule)
        self.assertTrue(any(tag.startswith(f"rulescope-version:{lagprof.VERSION}+") for tag in rule["tags"]))

    def test_component_templates_installed_before_index_templates(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        puts = [p for m, p in self.fake.requests if m == "PUT"]
        lag_index = puts.index("/_index_template/rulescope-lag-results")
        self.assertLess(puts.index("/_component_template/rulescope-lag"), lag_index)
        self.assertLess(puts.index("/_component_template/rulescope-common"), lag_index)

    def test_component_fields_added_to_existing_indices(self):
        # Pack updates can add fields; existing indices need them too, or ES|QL panels fail.
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        puts = [p.split("?")[0] for m, p in self.fake.requests if m == "PUT"]
        self.assertIn("/rulescope-lag-*/_mapping", puts)
        self.assertIn("/rulescope-catalog-*,-rulescope-catalog-rule-streams/_mapping", puts)
        self.assertGreater(puts.index("/rulescope-lag-*/_mapping"), puts.index("/_component_template/rulescope-lag"))
        self.assertIn("reason", self.fake.store["/rulescope-lag-*/_mapping"]["properties"])

    def test_data_streams_created_empty_after_templates(self):
        # Dashboard panels fail with "Unknown index" until each data stream exists.
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        puts = [p for m, p in self.fake.requests if m == "PUT"]
        made = sorted(p for p in puts if p.startswith("/_data_stream/rulescope-lag-"))
        self.assertEqual(made, ["/_data_stream/rulescope-lag-host-lag", "/_data_stream/rulescope-lag-results",
                                "/_data_stream/rulescope-lag-runs", "/_data_stream/rulescope-lag-stream-stats"])
        last_template = max(i for i, p in enumerate(puts) if p.startswith("/_index_template/rulescope-lag-"))
        self.assertTrue(all(puts.index(p) > last_template for p in made))

    def test_per_space_role_and_space_prefixed_rules(self):
        opts = lagprof.InstallOptions(workflow="per-space", space="soc", shared_stats=True)
        self.assertEqual(lagprof.install(self.client, self.rep, opts), 0, self.out.getvalue())
        self.assertEqual(self.fake.store["/api/security/role/rulescope-lag-soc"]["kibana"][0]["spaces"], ["soc"])
        self.assertTrue(any(p.startswith("/s/soc/api/alerting/rule/") for _, p in self.fake.requests))
        self.assertTrue((ROOT / "build/rulescope-lag-hourly-space-soc.yaml").exists())

    def test_per_space_copies_do_not_share_role_dashboard_or_alerts(self):
        # Kibana dashboard and alert IDs are unique across spaces; one role would be rewritten.
        for space, shared in (("soc", True), ("team-b", False)):
            opts = lagprof.InstallOptions(workflow="per-space", space=space, shared_stats=shared)
            self.assertEqual(lagprof.install(self.client, self.rep, opts), 0, self.out.getvalue())
        store = self.fake.store
        self.assertEqual(store["/api/security/role/rulescope-lag-soc"]["kibana"][0]["spaces"], ["soc"])
        self.assertEqual(store["/api/security/role/rulescope-lag-team-b"]["kibana"][0]["spaces"], ["team-b"])
        self.assertNotIn("/api/security/role/rulescope-lag", store)
        posted = [p for m, p in self.fake.requests if m in ("POST", "PUT")]
        for space in ("soc", "team-b"):
            self.assertIn(f"/s/{space}/api/dashboards/rulescope-lag-overview-{space}", posted)
            self.assertIn(f"/s/{space}/api/alerting/rule/rulescope-lag-miss-rate-{space}", posted)
        rule = store["/api/alerting/rule/rulescope-lag-miss-rate-team-b"]
        self.assertIn('| WHERE rulescope.space.id == "team-b"', rule["params"]["esqlQuery"]["esql"])
        self.assertTrue(rule["name"].endswith("(team-b)"))

    def test_status_finds_per_space_workflow_in_its_space(self):
        opts = lagprof.InstallOptions(workflow="per-space", space="soc", shared_stats=True)
        self.assertEqual(lagprof.install(self.client, self.rep, opts), 0)
        self.rep.lines.clear()
        lagprof.status(self.client, self.rep, "soc")
        found = [n for s, k, n in self.statuses() if k == "workflow" and s == "present"]
        self.assertEqual(len(found), 3, self.statuses())

    def test_mixing_scopes_is_blocked(self):
        self.assertEqual(lagprof.install(self.client, self.rep, lagprof.InstallOptions()), 0)
        opts = lagprof.InstallOptions(workflow="per-space", space="soc", shared_stats=True)
        self.assertEqual(lagprof.install(self.client, self.rep, opts), 1)
        self.assertIn(("BLOCKED", "workflow", "per-space"), self.statuses())
        opts.force = True
        self.assertEqual(lagprof.install(self.client, self.rep, opts), 0, self.out.getvalue())

    def test_global_blocked_when_per_space_installed(self):
        opts = lagprof.InstallOptions(workflow="per-space", space="soc", shared_stats=True)
        self.assertEqual(lagprof.install(self.client, self.rep, opts), 0)
        self.assertEqual(lagprof.install(self.client, self.rep, lagprof.InstallOptions()), 1)
        self.assertIn(("BLOCKED", "workflow", "all-spaces"), self.statuses())

    def test_global_catalog_with_per_space_lag(self):
        # The lag module reads the catalog of its own scope, so both share one scope.
        cat = lagprof.InstallOptions(modules=("catalog",))
        self.assertEqual(lagprof.install(self.client, self.rep, cat), 0)
        lag = lagprof.InstallOptions(workflow="per-space", space="soc", shared_stats=True, modules=("lag",))
        # Installing lag installs its requirement, the catalog, in the same scope: refused.
        self.assertEqual(lagprof.install(self.client, self.rep, lag), 1)
        self.assertIn("global Rule Catalog is installed", self.out.getvalue())

    def test_shared_stats_check_uses_installed_copies(self):
        soc = lagprof.InstallOptions(workflow="per-space", space="soc", shared_stats=True)
        team_b = lagprof.InstallOptions(workflow="per-space", space="team-b", shared_stats=True)
        self.assertEqual(lagprof.install(self.client, self.rep, soc), 0)
        self.assertEqual(lagprof.install(self.client, self.rep, team_b), 1)
        self.assertIn(("BLOCKED", "workflow", "team-b"), self.statuses())
        # Once soc's copy is removed, team-b may take over, whatever the run history says.
        lagprof.uninstall(self.client, self.rep, False, "soc")
        self.assertEqual(lagprof.install(self.client, self.rep, team_b), 0, self.out.getvalue())

    def _dashboard_queries(self, key):
        found = []

        def walk(o):
            if isinstance(o, dict):
                if o.get("type") == "esql":
                    found.append(o["query"])
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(self.fake.store[key])
        return found

    def test_per_space_dashboard_is_filtered_to_its_space(self):
        opts = lagprof.InstallOptions(workflow="per-space", space="soc", shared_stats=True)
        self.assertEqual(lagprof.install(self.client, self.rep, opts), 0, self.out.getvalue())
        queries = self._dashboard_queries("/api/dashboards/rulescope-lag-overview-soc")
        self.assertEqual(len(queries), 9)
        for q in queries:
            index = q.split("\n", 1)[0].split()[1]
            scoped = '| WHERE rulescope.space.id == "soc"' in q
            # Stream stats and host lag aren't divided by space.
            self.assertEqual(scoped, index in lagprof.SPACE_FILTERED_INDICES, q)
        # The marker includes the space, so status agrees with what install wrote.
        self.rep.lines.clear()
        lagprof.status(self.client, self.rep, "soc")
        self.assertIn(("current", "dashboard", "rulescope-lag-overview-soc"), self.statuses())

    def test_global_dashboard_is_not_filtered(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        for q in self._dashboard_queries("/api/dashboards/rulescope-lag-overview"):
            self.assertNotIn("rulescope.space.id ==", q)

    def _install_two_copies(self):
        for space, shared in (("default", True), ("soc", False)):
            opts = lagprof.InstallOptions(workflow="per-space", space=space, shared_stats=shared)
            self.assertEqual(lagprof.install(self.client, self.rep, opts), 0, self.out.getvalue())
        self.rep.lines.clear()

    def test_install_two_copies(self):
        self._install_two_copies()
        self.assertIn("/api/dashboards/rulescope-lag-overview-default", self.fake.store)
        self.assertIn("/api/dashboards/rulescope-lag-overview-soc", self.fake.store)

    def test_uninstall_one_copy_keeps_shared_resources(self):
        self._install_two_copies()
        self.assertEqual(lagprof.uninstall(self.client, self.rep, False, "default"), 0, self.out.getvalue())
        # soc's runs still need the pipelines, templates and indices.
        self.assertIn("/_ingest/pipeline/rulescope-stamp", self.fake.store)
        self.assertIn(("kept", "pipeline", "rulescope-stamp"), self.statuses())
        self.assertIn("/_component_template/rulescope-lag", self.fake.store)
        self.assertIn("/_index_template/rulescope-lag-results", self.fake.store)
        # default's own resources go.
        self.assertIn(("deleted", "workflow", "rulescope-lag-hourly-space-default"), self.statuses())
        self.assertIn(("deleted", "role", "rulescope-lag-default"), self.statuses())
        self.assertNotIn("/api/security/role/rulescope-lag-default", self.fake.store)
        self.assertIn("/api/security/role/rulescope-lag-soc", self.fake.store)
        self.assertEqual(sorted(w["name"] for w in self.fake.workflows.values()),
                         ["rulescope-catalog-snapshot-space-soc", "rulescope-catalog-sync-space-soc",
                          "rulescope-lag-hourly-space-soc"])

    def test_uninstall_last_copy_removes_shared_resources(self):
        self._install_two_copies()
        lagprof.uninstall(self.client, self.rep, False, "default")
        self.rep.lines.clear()
        lagprof.uninstall(self.client, self.rep, False, "soc")
        self.assertIn(("deleted", "component_template", "rulescope-lag"), self.statuses())
        self.assertNotIn("/_ingest/pipeline/rulescope-catalog-rules-parse", self.fake.store)
        self.assertFalse(self.fake.workflows)

    def test_uninstall_one_copy_delete_data_only_touches_its_space(self):
        self._install_two_copies()
        lagprof.uninstall(self.client, self.rep, True, "default")
        self.assertNotIn(("DELETE", "/_data_stream/rulescope-lag-*"), self.fake.requests)
        self.assertNotIn(("DELETE", "/_data_stream/rulescope-catalog-*"), self.fake.requests)
        paths = sorted(p.split("/_delete_by_query")[0] for p, b in self.fake.delete_by_query)
        self.assertEqual(paths, ["/rulescope-catalog-*", "/rulescope-lag-*"])
        for _, body in self.fake.delete_by_query:
            self.assertEqual(body, {"query": {"term": {"rulescope.scope": "space:default"}}})

    def test_uninstall_last_copy_delete_data_deletes_everything(self):
        opts = lagprof.InstallOptions(workflow="per-space", space="soc", shared_stats=True)
        lagprof.install(self.client, self.rep, opts)
        lagprof.uninstall(self.client, self.rep, True, "soc")
        self.assertIn(("DELETE", "/_data_stream/rulescope-lag-*"), self.fake.requests)
        self.assertIn(("DELETE", "/_data_stream/rulescope-catalog-*"), self.fake.requests)
        # The lag module has no plain indices, so no index delete with an empty name.
        self.assertNotIn("DELETE", [m for m, p in self.fake.requests if p.startswith("/?")])
        self.assertFalse(getattr(self.fake, "delete_by_query", []))

    def test_catalog_cannot_be_removed_while_lag_needs_it(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.rep.lines.clear()
        self.assertEqual(lagprof.uninstall(self.client, self.rep, False, modules=("catalog",)), 1)
        self.assertIn(("BLOCKED", "module", "catalog"), self.statuses())
        self.assertEqual(len(self.fake.workflows), 3)
        # Removing lag alone leaves the catalog.
        self.assertEqual(lagprof.uninstall(self.client, self.rep, False, modules=("lag",)), 0)
        self.assertEqual(sorted(w["name"] for w in self.fake.workflows.values()),
                         ["rulescope-catalog-snapshot", "rulescope-catalog-sync"])
        self.assertIn("/_ingest/pipeline/rulescope-stamp", self.fake.store)
        self.assertEqual(lagprof.uninstall(self.client, self.rep, False, modules=("catalog",)), 0)
        self.assertFalse(self.fake.workflows)

    def test_force_removes_catalog_under_lag(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.assertEqual(lagprof.uninstall(self.client, self.rep, False, modules=("catalog",), force=True), 0)
        self.assertEqual([w["name"] for w in self.fake.workflows.values()], ["rulescope-lag-hourly"])

    def test_workflow_imported_then_unchanged_then_updated(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.assertIn(("created", "workflow", "rulescope-lag-hourly"), self.statuses())
        self.rep.lines.clear()
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.assertIn(("unchanged", "workflow", "rulescope-lag-hourly"), self.statuses())
        wid = next(w for w, v in self.fake.workflows.items() if v["name"] == "rulescope-lag-hourly")
        self.fake.workflows[wid]["yaml"] = "name: rulescope-lag-hourly\n# edited"
        self.rep.lines.clear()
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.assertIn(("updated", "workflow", "rulescope-lag-hourly"), self.statuses())

    def test_reinstall_after_uninstall_does_not_duplicate(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        lagprof.uninstall(self.client, self.rep, delete_data=False)
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.assertEqual(len(self.fake.workflows), 3, self.fake.workflows.keys())

    def test_workflow_saved_with_owner_key(self):
        self.cfg.module_api_keys = {"lag": "owner-key"}
        self.client = lagprof.Client(self.cfg)
        lagprof.install(self.client, self.rep, lagprof.InstallOptions(modules=("lag",)))
        self.assertEqual(self.fake.workflow_auth, "ApiKey owner-key")
        lag_warns = [n for s, k, n in self.statuses() if s == "WARN" and k == "workflow" and "lag" in n]
        self.assertEqual(lag_warns, [])

    def test_old_lag_key_still_owns_the_lag_workflow(self):
        self.cfg.workflow_api_key = "old-key"
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.assertEqual(self.fake.workflow_auth, "ApiKey old-key")

    def test_warns_without_owner_key(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.assertIn("RULESCOPE_LAG_WORKFLOW_API_KEY not set", self.out.getvalue())

    def test_manual_workflow_when_import_disabled(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions(import_workflow=False))
        self.assertIn(("manual", "workflow", "rulescope-lag-hourly.yaml"), self.statuses())

    def test_uninstall_deletes_workflows(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.rep.lines.clear()
        lagprof.uninstall(self.client, self.rep, delete_data=False)
        self.assertIn(("deleted", "workflow", "rulescope-lag-hourly"), self.statuses())
        self.assertEqual(self.fake.workflows, {})

    def test_outdated_resource_is_updated(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.fake.store["/_component_template/rulescope-lag"]["_meta"]["rulescope_fingerprint"] = "0.0.1+0000000000"
        self.rep.lines.clear()
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.assertIn(("updated", "component_template", "rulescope-lag"), self.statuses())

    def test_resource_with_old_lagprof_marker_reads_as_its_version(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        meta = self.fake.store["/_component_template/rulescope-lag"]["_meta"]
        marker = meta.pop("rulescope_fingerprint")
        meta.pop("rulescope_version")
        meta["lagprof_fingerprint"] = marker
        self.rep.lines.clear()
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.assertIn(("unchanged", "component_template", "rulescope-lag"), self.statuses())

    def test_changed_file_is_updated_without_version_bump(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        real = resources.fingerprint
        try:
            resources.fingerprint = lambda path, extra="": real(path, extra) + (
                "x" if path and path.endswith("rulescope-lag.json") else "")
            self.rep.lines.clear()
            lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        finally:
            resources.fingerprint = real
        self.assertIn(("updated", "component_template", "rulescope-lag"), self.statuses())
        self.assertIn(("unchanged", "pipeline", "rulescope-stamp"), self.statuses())

    def test_outdated_lookup_index_is_recreated(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        # An index built from another mapping, with result fields in it.
        self.fake.store["/rulescope-catalog-rule-streams"] = {"properties": {
            "lag_p99_s": {"type": "double"}, "data_stream": {"properties": {"dataset": {"type": "keyword"}}}}}
        self.rep.lines.clear()
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.assertIn(("recreated", "index", "rulescope-catalog-rule-streams"), self.statuses())
        self.rep.lines.clear()
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.assertIn(("unchanged", "index", "rulescope-catalog-rule-streams"), self.statuses())

    def test_lookup_index_missing_a_field_is_recreated(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        mapping = json.loads(json.dumps(self.fake.store["/rulescope-catalog-rule-streams"]))
        del mapping["properties"]["@timestamp"]
        self.fake.store["/rulescope-catalog-rule-streams"] = mapping
        self.rep.lines.clear()
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.assertIn(("recreated", "index", "rulescope-catalog-rule-streams"), self.statuses())
        self.assertIn("1 missing", self.out.getvalue())
        self.assertIn("@timestamp", self.fake.store["/rulescope-catalog-rule-streams"]["properties"])

    def test_uninstall_keeps_templates_in_use(self):
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.rep.lines.clear()
        lagprof.uninstall(self.client, self.rep, delete_data=False)
        self.assertIn(("kept", "index_template", "rulescope-lag-results"), self.statuses())
        self.assertIn(("deleted", "component_template", "rulescope-lag"), self.statuses())
        self.assertIn(("deleted", "role", "rulescope-lag"), self.statuses())

    def test_status_reports_missing_then_current(self):
        lagprof.status(self.client, self.rep)
        self.assertIn(("missing", "component_template", "rulescope-lag"), self.statuses())
        lagprof.install(self.client, self.rep, lagprof.InstallOptions())
        self.rep.lines.clear()
        lagprof.status(self.client, self.rep)
        self.assertIn(("current", "component_template", "rulescope-lag"), self.statuses())


class CatalogInstallTests(FakeServerCase):
    def install(self, **kw):
        opts = lagprof.InstallOptions(modules=("catalog",), **kw)
        return lagprof.install(self.client, self.rep, opts)

    def test_global_install_is_idempotent(self):
        self.assertEqual(self.install(), 0, self.out.getvalue())
        self.assertEqual({s for s, k, n in self.statuses() if s != "WARN"}, {"created"})
        names = sorted(w["name"] for w in self.fake.workflows.values())
        self.assertEqual(names, ["rulescope-catalog-snapshot", "rulescope-catalog-sync"])
        self.assertEqual(self.fake.store["/api/security/role/rulescope-catalog"]["kibana"][0]["spaces"], ["*"])
        self.assertNotIn("_rulescope_notes", self.fake.store["/_index_template/rulescope-catalog-rules"])
        self.assertIn("/_data_stream/rulescope-catalog-syncs", self.fake.store)
        self.assertIn("/rulescope-catalog-rule-streams", self.fake.store)
        # Nothing of the lag module.
        self.assertFalse([k for k in self.fake.store if "rulescope-lag" in k])
        self.rep.lines.clear()
        self.assertEqual(self.install(), 0)
        self.assertEqual({s for s, k, n in self.statuses() if s != "WARN"}, {"unchanged"})

    def test_per_space_copy(self):
        self.assertEqual(self.install(workflow="per-space", space="soc"), 0, self.out.getvalue())
        wf = {w["name"]: w for w in self.fake.workflows.values()}
        self.assertEqual(sorted(wf), ["rulescope-catalog-snapshot-space-soc", "rulescope-catalog-sync-space-soc"])
        self.assertEqual({w["space"] for w in wf.values()}, {"soc"})
        self.assertIn("scope: space:soc", wf["rulescope-catalog-sync-space-soc"]["yaml"])
        self.assertEqual(self.fake.store["/api/security/role/rulescope-catalog-soc"]["kibana"][0]["spaces"], ["soc"])
        # Dashboard and alert cover only this space.
        dash = self.fake.store["/api/dashboards/rulescope-catalog-health-soc"]
        queries = [json.dumps(p) for p in dash["panels"] if p["type"] == "vis"]
        self.assertTrue(queries)
        for q in queries:
            self.assertTrue('WHERE rulescope.scope == \\"space:soc\\"' in q
                            or 'WHERE rulescope.space.id == \\"soc\\"' in q, q)
        self.assertEqual(dash["title"], "Rule Catalog (soc)")
        alert = self.fake.store["/api/alerting/rule/rulescope-catalog-stale-soc"]
        self.assertIn('rulescope.scope == "space:soc"', alert["params"]["esqlQuery"]["esql"])

    def test_one_scope_per_cluster(self):
        self.install(workflow="per-space", space="soc")
        self.rep.lines.clear()
        self.assertEqual(self.install(), 1)
        self.assertIn(("BLOCKED", "workflow", "all-spaces"), self.statuses())
        self.assertEqual(self.install(force=True), 0)

    def test_global_blocks_per_space(self):
        self.install()
        self.rep.lines.clear()
        self.assertEqual(self.install(workflow="per-space", space="soc"), 1)
        self.assertIn("global Rule Catalog is installed", self.out.getvalue())

    def test_uninstall_space_copy_keeps_shared_until_last(self):
        self.install(workflow="per-space", space="soc")
        self.install(workflow="per-space", space="team-b")
        self.rep.lines.clear()
        lagprof.uninstall(self.client, self.rep, True, "soc", modules=("catalog",))
        self.assertIn("/_ingest/pipeline/rulescope-catalog-rules-parse", self.fake.store)
        path, body = self.fake.delete_by_query[-1]
        self.assertTrue(path.startswith("/rulescope-catalog-*/"))
        self.assertEqual(body, {"query": {"term": {"rulescope.scope": "space:soc"}}})
        self.assertEqual(sorted(w["name"] for w in self.fake.workflows.values()),
                         ["rulescope-catalog-snapshot-space-team-b", "rulescope-catalog-sync-space-team-b"])
        lagprof.uninstall(self.client, self.rep, True, "team-b", modules=("catalog",))
        self.assertNotIn("/_ingest/pipeline/rulescope-catalog-rules-parse", self.fake.store)
        self.assertIn(("DELETE", "/_data_stream/rulescope-catalog-*"), self.fake.requests)
        self.assertFalse(self.fake.workflows)

    def test_catalog_owner_key(self):
        self.cfg.module_api_keys = {"catalog": "owner-cat"}
        self.client = lagprof.Client(self.cfg)
        self.install()
        self.assertEqual(self.fake.workflow_auth, "ApiKey owner-cat")
        self.assertNotIn("WORKFLOW_API_KEY not set", self.out.getvalue())

    def test_warns_without_catalog_owner_key(self):
        self.install()
        self.assertIn("RULESCOPE_CATALOG_WORKFLOW_API_KEY not set", self.out.getvalue())

    def test_status_finds_catalog(self):
        self.install()
        self.rep.lines.clear()
        lagprof.status(self.client, self.rep, modules=("catalog",))
        st = self.statuses()
        self.assertIn(("current", "pipeline", "rulescope-stamp"), st)
        self.assertTrue(any(s == "present" and k == "workflow" for s, k, n in st))


class CatalogRenderTests(unittest.TestCase):
    def test_both_scopes_render(self):
        for r in resources.module_resources("catalog", None):
            if r.kind != "workflow":
                continue
            name, text = resources.render_module_workflow(r, None)
            self.assertEqual(name, f"{r.name}.yaml")
            self.assertIn("scope: global", text)
            self.assertNotIn("space_id", text)
            name, text = resources.render_module_workflow(r, "soc")
            self.assertEqual(name, f"{r.name}-space-soc.yaml")
            self.assertIn(f"name: {r.name}-space-soc", text)
            self.assertIn("space_id: soc", text)
            self.assertNotIn("[[", text)
            self.assertNotIn("#%", text)

    def test_bad_space_refused(self):
        r = next(r for r in resources.module_resources("catalog", None) if r.kind == "workflow")
        with self.assertRaises(lagprof.ConfigError):
            resources.render_module_workflow(r, "Bad Space")

    def test_owner_key_settings(self):
        env = {"LAGPROF_TARGET": "docker", "ES_URL": "x", "KIBANA_URL": "y", "ES_API_KEY": "k"}
        cfg = lagprof.load_config(None, {**env, "RULESCOPE_CATALOG_WORKFLOW_API_KEY": "c",
                                         "LAGPROF_WORKFLOW_API_KEY": "l"})
        self.assertEqual((cfg.owner_key("catalog"), cfg.owner_key("lag")), ("c", "l"))
        cfg = lagprof.load_config(None, {**env, "RULESCOPE_WORKFLOW_API_KEY": "all"})
        self.assertEqual((cfg.owner_key("catalog"), cfg.owner_key("lag")), ("all", "all"))


class ModuleTests(unittest.TestCase):
    def test_default_is_catalog_and_lag(self):
        self.assertEqual(modules.parse_modules(None), ("catalog", "lag"))
        self.assertEqual(modules.parse_modules(" lag , "), ("lag",))
        self.assertEqual([m.name for m in modules.resolve(["lag"])], ["catalog", "lag"])

    def test_unknown_module_is_refused(self):
        with self.assertRaises(lagprof.ConfigError):
            modules.parse_modules("lag,nope")

    def test_requirements_come_first_and_loops_are_refused(self):
        saved = dict(modules.MODULES)
        try:
            modules.MODULES["catalog"] = modules.Module("catalog", "Rule Catalog")
            modules.MODULES["lag"] = modules.Module("lag", "Ingest Lag Profiler", ("catalog",))
            self.assertEqual([m.name for m in modules.resolve(["lag"])], ["catalog", "lag"])
            modules.MODULES["catalog"] = modules.Module("catalog", "Rule Catalog", ("lag",))
            with self.assertRaises(lagprof.ConfigError):
                modules.resolve(["lag"])
        finally:
            modules.MODULES.clear()
            modules.MODULES.update(saved)

    def test_every_resource_belongs_to_one_module(self):
        everything = resources.pack_resources()
        cat, lag = resources.module_resources("catalog", None), resources.module_resources("lag", None)
        self.assertEqual(everything, cat + lag)
        self.assertEqual(resources.pack_resources(modules=["lag"]), cat + lag)
        self.assertEqual({r.module for r in cat}, {"catalog"})
        self.assertEqual({r.module for r in lag}, {"lag"})
        keys = [(r.kind, r.name) for r in everything]
        self.assertEqual(len(keys), len(set(keys)))

    def test_catalog_pipelines_come_before_templates(self):
        kinds = [r.kind for r in resources.module_resources("catalog", None)]
        self.assertEqual(kinds[0], "pipeline")
        self.assertLess(max(i for i, k in enumerate(kinds) if k == "pipeline"),
                        min(i for i, k in enumerate(kinds) if k.endswith("template")))
        self.assertEqual(resources.module_resources("catalog", None)[0].name, "rulescope-stamp")

    def test_cli_rejects_unknown_module(self):
        err = io.StringIO()
        old = sys.stderr
        try:
            sys.stderr = err
            code = lagprof.main(["status", "--target-file", "targets/docker.env", "--modules", "nope"])
        finally:
            sys.stderr = old
        self.assertEqual(code, 2)
        self.assertIn("unknown module", err.getvalue())


if __name__ == "__main__":
    unittest.main()
