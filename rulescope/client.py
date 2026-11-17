"""HTTP client for Elasticsearch and Kibana. Standard library only."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request

from .config import Config
from .core import SERVERLESS_API_VERSION


class HttpError(Exception):
    def __init__(self, status: int, method: str, url: str, body: object):
        self.status, self.method, self.url, self.body = status, method, url, body
        super().__init__(f"{method} {url} -> {status}: {str(body)[:300]}")


class Client:
    def __init__(self, cfg: Config, serverless: bool | None = None):
        self.cfg = cfg
        self.serverless = cfg.target == "serverless" if serverless is None else serverless
        self.timeout = 60  # seconds per request
        if cfg.ca_cert:
            self.ctx = ssl.create_default_context(cafile=cfg.ca_cert)
        else:
            self.ctx = ssl.create_default_context()
        if not cfg.verify_tls:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def _send(self, base: str, method: str, path: str, body=None, headers=None,
              raw: bytes | None = None, allow: tuple[int, ...] = ()) -> tuple[int, object]:
        url = base + path
        hdrs = {"Authorization": self.cfg.auth_header, "Accept": "application/json"}
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=self.timeout) as resp:
                status, payload = resp.status, resp.read()
        except urllib.error.HTTPError as err:
            status, payload = err.code, err.read()
        except urllib.error.URLError as err:
            raise HttpError(0, method, url, f"connection failed: {err.reason}") from err
        try:
            parsed = json.loads(payload) if payload else None
        except ValueError:
            parsed = payload.decode(errors="replace")
        if status >= 400 and status not in allow:
            raise HttpError(status, method, url, parsed)
        return status, parsed

    def as_workflow_owner(self, module: str = "lag") -> "Client":
        """A client that authenticates with the module's workflow owner API key, if one is set."""
        key = self.cfg.owner_key(module) if hasattr(self.cfg, "owner_key") else self.cfg.workflow_api_key
        if not key:
            return self
        import copy
        cfg = copy.copy(self.cfg)
        cfg.api_key, cfg.username, cfg.password = key, "", ""
        return Client(cfg, serverless=self.serverless)

    def es(self, method: str, path: str, body=None, allow: tuple[int, ...] = ()):
        return self._send(self.cfg.es_url, method, path, body, allow=allow)

    def kb(self, method: str, path: str, body=None, allow: tuple[int, ...] = (),
           raw: bytes | None = None, content_type: str | None = None):
        headers = {"kbn-xsrf": "rulescope"}
        if self.serverless:
            headers["elastic-api-version"] = SERVERLESS_API_VERSION
        if content_type:
            headers["Content-Type"] = content_type
        return self._send(self.cfg.kibana_url, method, path, body, headers, raw, allow)
