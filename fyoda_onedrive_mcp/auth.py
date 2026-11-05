"""Sign-in for a personal Microsoft account (MSA) using the OAuth device-code flow.

No client secret is used or stored. The token cache is written with 0600 permissions.
Scope is Files.ReadWrite unless ONEDRIVE_READONLY=1 (then Files.Read).
"""
from __future__ import annotations

import os
import pathlib
import sys

import msal

AUTHORITY = "https://login.microsoftonline.com/consumers"  # personal accounts only


class AuthError(RuntimeError):
    pass


def scopes() -> list[str]:
    return ["Files.Read"] if os.environ.get("ONEDRIVE_READONLY") == "1" else ["Files.ReadWrite"]


def default_cache_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("ONEDRIVE_TOKEN_CACHE", "~/.fyoda-onedrive-mcp/token_cache.json")).expanduser()


class Auth:
    def __init__(self, client_id: str | None = None, cache_path: pathlib.Path | None = None):
        self.client_id = client_id or os.environ.get("ONEDRIVE_CLIENT_ID", "")
        if not self.client_id:
            raise AuthError("ONEDRIVE_CLIENT_ID is not set (the Application (client) ID of your Entra app registration).")
        self.cache_path = cache_path or default_cache_path()
        self.cache = msal.SerializableTokenCache()
        if self.cache_path.exists():
            self.cache.deserialize(self.cache_path.read_text())
        self.app = msal.PublicClientApplication(self.client_id, authority=AUTHORITY, token_cache=self.cache)

    def _save(self) -> None:
        if not self.cache.has_state_changed:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.cache_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(self.cache.serialize())
        os.replace(tmp, self.cache_path)

    def token(self) -> str:
        accounts = self.app.get_accounts()
        if not accounts:
            raise AuthError("Not signed in. Run: python -m fyoda_onedrive_mcp login")
        result = self.app.acquire_token_silent(scopes(), account=accounts[0])
        self._save()
        if not result or "access_token" not in result:
            raise AuthError("Sign-in expired or scope changed. Run: python -m fyoda_onedrive_mcp login")
        return result["access_token"]

    def login(self) -> str:
        flow = self.app.initiate_device_flow(scopes=scopes())
        if "user_code" not in flow:
            raise AuthError(f"Could not start device sign-in: {flow.get('error')}: {flow.get('error_description')}")
        print(flow["message"], file=sys.stderr, flush=True)
        result = self.app.acquire_token_by_device_flow(flow)
        self._save()
        if "access_token" not in result:
            raise AuthError(f"Sign-in failed: {result.get('error')}: {result.get('error_description')}")
        return (result.get("id_token_claims") or {}).get("preferred_username", "signed in")
