"""Minimal Home Assistant REST client."""

from __future__ import annotations

import math
import os
import time

import requests


# requests ships its own CA list (certifi), which doesn't know private CAs installed on the system.
SYSTEM_CA_BUNDLES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt")


class HAError(RuntimeError):
    pass


class HAClient:
    def __init__(self, url: str, token: str, timeout: float = 10.0):
        self.url = url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        self.timeout = timeout
        if not os.environ.get("REQUESTS_CA_BUNDLE"):
            bundle = next((b for b in SYSTEM_CA_BUNDLES if os.path.exists(b)), None)
            if bundle:
                self.session.verify = bundle

    def _request(self, method: str, path: str, **kw):
        try:
            r = self.session.request(method, f"{self.url}{path}", timeout=self.timeout, **kw)
        except requests.RequestException as e:
            raise HAError(f"cannot reach Home Assistant: {e}") from e
        if r.status_code == 401:
            raise HAError("unauthorized: check the long-lived access token")
        if r.status_code == 404:
            raise HAError(f"not found: {path}")
        if not r.ok:
            raise HAError(f"HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json() if r.content else None
        except ValueError as e:
            raise HAError(f"unexpected (non-JSON) answer from Home Assistant: {e}") from e

    def ping(self) -> str:
        return self._request("GET", "/api/").get("message", "")

    def get_state(self, entity_id: str) -> dict:
        return self._request("GET", f"/api/states/{entity_id}")

    def get_float(self, entity_id: str) -> float | None:
        """Numeric state, or None when unavailable/unknown."""
        try:
            v = float(self.get_state(entity_id)["state"])
        except (ValueError, TypeError, KeyError):
            return None
        return None if math.isnan(v) else v

    def set_number(self, entity_id: str, value: float) -> None:
        self._request("POST", "/api/services/number/set_value", json={"entity_id": entity_id, "value": value})

    def list_entities(self, domain: str) -> list[tuple[str, str]]:
        states = self._request("GET", "/api/states")
        out = [
            (s["entity_id"], s.get("attributes", {}).get("friendly_name", s["entity_id"]))
            for s in states
            if s["entity_id"].startswith(f"{domain}.")
        ]
        return sorted(out)


class RealClock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
