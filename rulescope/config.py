"""Connection settings from a target file and the environment."""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .core import PACK_ROOT, TARGETS, ConfigError


def parse_env_text(text: str, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Parse KEY=VALUE lines. Supports comments, quotes and ${VAR} expansion."""
    environ = environ if environ is not None else {}
    out: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise ConfigError(f"line {lineno}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) >= 2:
            quoted, value = value[0], value[1:-1]
        else:
            quoted = ""
            value = re.sub(r"\s+#.*$", "", value)  # trailing comment
        if quoted != "'":
            value = re.sub(r"\$\{(\w+)\}",
                           lambda m: out.get(m.group(1), environ.get(m.group(1), "")), value)
        out[key] = value
    return out


def decode_cloud_id(cloud_id: str) -> tuple[str, str | None]:
    """Turn an Elastic Cloud ID into (elasticsearch_url, kibana_url)."""
    _, sep, encoded = cloud_id.partition(":")
    if not sep:
        encoded = cloud_id
    try:
        decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"CLOUD_ID could not be decoded: {exc}") from exc
    parts = decoded.split("$")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ConfigError("CLOUD_ID is not in the expected host$es$kibana form")
    host, port = parts[0], "443"
    if ":" in host:
        host, port = host.rsplit(":", 1)
    suffix = "" if port == "443" else f":{port}"
    es_url = f"https://{parts[1]}.{host}{suffix}"
    kb_url = f"https://{parts[2]}.{host}{suffix}" if len(parts) > 2 and parts[2] else None
    return es_url, kb_url


@dataclass
class Config:
    target: str
    es_url: str
    kibana_url: str
    api_key: str = ""
    username: str = ""
    password: str = ""
    ca_cert: str = ""
    verify_tls: bool = True
    workflow_api_key: str = ""
    # Per-module workflow owner keys: {"catalog": key}. See owner_key().
    module_api_keys: dict = field(default_factory=dict)

    def owner_key(self, module: str) -> str:
        """The API key whose user saves this module's workflows, or "".

        RULESCOPE_<MODULE>_WORKFLOW_API_KEY wins; RULESCOPE_WORKFLOW_API_KEY is the
        single-owner shortcut for every module; LAGPROF_WORKFLOW_API_KEY still
        works for the lag module."""
        return (self.module_api_keys.get(module) or self.module_api_keys.get("*")
                or (self.workflow_api_key if module == "lag" else ""))

    @property
    def auth_header(self) -> str:
        if self.api_key:
            return f"ApiKey {self.api_key}"
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return f"Basic {token}"


def load_config(target_file: str | None, environ: dict[str, str] | None = None,
                target_override: str | None = None) -> Config:
    environ = dict(os.environ if environ is None else environ)
    values: dict[str, str] = {}
    if target_file:
        path = Path(target_file)
        if not path.is_absolute() and not path.exists():
            path = PACK_ROOT / target_file
        if not path.exists():
            raise ConfigError(f"target file not found: {target_file}")
        values = parse_env_text(path.read_text(), environ)
    keys = ("LAGPROF_TARGET", "ES_URL", "KIBANA_URL", "CLOUD_ID", "ES_API_KEY",
            "ES_USERNAME", "ES_PASSWORD", "CA_CERT", "VERIFY_TLS", "LAGPROF_WORKFLOW_API_KEY")
    for k in keys:
        if environ.get(k):
            values[k] = environ[k]
    for k, v in environ.items():
        if re.fullmatch(r"RULESCOPE_(\w+_)?WORKFLOW_API_KEY", k) and v:
            values[k] = v
    module_keys = {}
    for k, v in values.items():
        m = re.fullmatch(r"RULESCOPE_(?:(\w+)_)?WORKFLOW_API_KEY", k)
        if m and v:
            module_keys[(m.group(1) or "*").lower()] = v

    target = target_override or values.get("LAGPROF_TARGET", "")
    if target not in TARGETS:
        raise ConfigError(f"LAGPROF_TARGET must be one of {', '.join(TARGETS)} (got {target!r})")

    es_url, kb_url = values.get("ES_URL", ""), values.get("KIBANA_URL", "")
    if values.get("CLOUD_ID"):
        cloud_es, cloud_kb = decode_cloud_id(values["CLOUD_ID"])
        es_url = es_url or cloud_es
        kb_url = kb_url or (cloud_kb or "")
    if not es_url or not kb_url:
        raise ConfigError("set ES_URL and KIBANA_URL (or CLOUD_ID for Elastic Cloud)")

    api_key = values.get("ES_API_KEY", "")
    user, pw = values.get("ES_USERNAME", ""), values.get("ES_PASSWORD", "")
    if not api_key and not (user and pw):
        raise ConfigError("set ES_API_KEY, or ES_USERNAME and ES_PASSWORD")
    if target in ("cloud", "serverless") and not api_key:
        raise ConfigError(f"{target} targets need ES_API_KEY")

    ca = values.get("CA_CERT", "")
    if ca and not Path(ca).is_absolute():
        ca = str(PACK_ROOT / ca)
    return Config(
        target=target, es_url=es_url.rstrip("/"), kibana_url=kb_url.rstrip("/"),
        api_key=api_key, username=user, password=pw, ca_cert=ca,
        verify_tls=values.get("VERIFY_TLS", "true").lower() not in ("false", "0", "no"),
        workflow_api_key=values.get("LAGPROF_WORKFLOW_API_KEY", ""),
        module_api_keys=module_keys,
    )
