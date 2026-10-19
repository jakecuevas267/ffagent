"""Secrets by name, from wherever the user keeps them: AWS SSM Parameter Store, the OS keyring, or
the environment / .env. Nothing in the app reads a credential any other way.

Order is set by FFAGENT_SECRETS, e.g. "ssm,keyring,env" (default "env"). The first backend that has
a value wins. Values are never logged or printed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# Every credential the app may need, with what it is for. `ffagent secrets check` walks this list.
KNOWN = {
    "ESPN_S2": "ESPN private leagues: espn_s2 cookie",
    "ESPN_SWID": "ESPN private leagues: SWID cookie (with braces)",
    "FANTASY_INFORMATION_SOURCE_URL": "expert data API base URL",
    "FANTASY_INFORMATION_SOURCE_API_KEY": "expert data API key",
    "ANTHROPIC_API_KEY": "LLM provider key (Anthropic)",
    "OPENAI_API_KEY": "LLM provider key (OpenAI), if llm.model uses it",
}
ALIASES = {"FANTASY_INFORMATION_SOURCE_API_KEY": ["FANTASY_INFORMATION_SOURCE_KEY"]}
SSM_PREFIX = "/ffagent/"
KEYRING_SERVICE = "ffagent"


class EnvBackend:
    name = "env"

    def get(self, key: str) -> str | None:
        return os.environ.get(key) or None

    def set(self, key: str, value: str) -> None:
        raise RuntimeError("the env backend is read-only: put the value in .env or export it")


class KeyringBackend:
    name = "keyring"

    def __init__(self, keyring_module=None, service: str = KEYRING_SERVICE):
        if keyring_module is None:
            import keyring as keyring_module  # optional dependency
        self._kr = keyring_module
        self._service = service

    def get(self, key: str) -> str | None:
        try:
            return self._kr.get_password(self._service, key) or None
        except Exception:  # noqa: BLE001 - a locked or missing keyring reads as "no value"
            return None

    def set(self, key: str, value: str) -> None:
        self._kr.set_password(self._service, key, value)


class SSMBackend:
    name = "ssm"

    def __init__(self, client=None, prefix: str = SSM_PREFIX, region: str | None = None):
        if client is None:
            import boto3  # optional dependency
            client = boto3.client("ssm", **({"region_name": region} if region else {}))
        self._ssm = client
        self._prefix = prefix
        self._cache: dict[str, str | None] = {}

    def get(self, key: str) -> str | None:
        if key in self._cache:
            return self._cache[key]
        try:
            r = self._ssm.get_parameter(Name=self._prefix + key, WithDecryption=True)
            val = r["Parameter"]["Value"] or None
        except Exception as e:  # noqa: BLE001 - missing parameter, no permission, no credentials
            if type(e).__name__ not in ("ParameterNotFound",):
                self.last_error = f"{type(e).__name__}"
            val = None
        self._cache[key] = val
        return val

    def set(self, key: str, value: str) -> None:
        self._ssm.put_parameter(Name=self._prefix + key, Value=value, Type="SecureString", Overwrite=True)
        self._cache[key] = value


@dataclass
class Found:
    key: str
    backend: str | None   # None = not found anywhere


class Secrets:
    def __init__(self, backends: list):
        self.backends = backends

    @classmethod
    def from_env(cls, spec: str | None = None, region: str | None = None) -> Secrets:
        spec = spec or os.environ.get("FFAGENT_SECRETS", "env")
        backends = []
        for name in [n.strip() for n in spec.split(",") if n.strip()]:
            if name == "env":
                backends.append(EnvBackend())
            elif name == "keyring":
                backends.append(KeyringBackend())
            elif name == "ssm":
                backends.append(SSMBackend(region=region or os.environ.get("FFAGENT_SSM_REGION")))
            else:
                raise ValueError(f"unknown secrets backend {name!r} (use ssm, keyring, env)")
        return cls(backends)

    def lookup(self, key: str) -> tuple[str | None, str | None]:
        for name in [key] + ALIASES.get(key, []):
            for b in self.backends:
                v = b.get(name)
                if v:
                    return v, b.name
        return None, None

    def get(self, key: str) -> str | None:
        return self.lookup(key)[0]

    def where(self, key: str) -> str | None:
        return self.lookup(key)[1]

    @property
    def writable(self):
        return next((b for b in self.backends if b.name != "env"), None)

    def set(self, key: str, value: str) -> str:
        b = self.writable
        if b is None:
            raise RuntimeError("no writable backend configured: set FFAGENT_SECRETS to include keyring or ssm")
        b.set(key, value)
        return b.name

    def export(self, keys=KNOWN) -> list[str]:
        """Put resolved values into os.environ for libraries that only read the environment
        (LLM provider SDKs). Returns the keys exported."""
        done = []
        for k in keys:
            v = self.get(k)
            if v and not os.environ.get(k):
                os.environ[k] = v
                done.append(k)
        return done

    def check(self, keys=KNOWN) -> list[Found]:
        return [Found(k, self.where(k)) for k in keys]


_default: Secrets | None = None


def secrets() -> Secrets:
    """Process-wide resolver, built from FFAGENT_SECRETS on first use."""
    global _default
    if _default is None:
        _default = Secrets.from_env()
    return _default


def get(key: str) -> str | None:
    return secrets().get(key)
