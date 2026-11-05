"""Minimal Microsoft Graph client (read-only calls)."""
from __future__ import annotations

import time
from typing import Callable

import httpx

BASE = "https://graph.microsoft.com/v1.0"


class GraphError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"Graph {status} {code}: {message}")
        self.status = status
        self.code = code


class GraphClient:
    def __init__(self, token_provider: Callable[[], str], transport: httpx.BaseTransport | None = None):
        self._token = token_provider
        self._http = httpx.Client(timeout=30, follow_redirects=True, transport=transport)

    def _request(self, url: str, params: dict | None = None) -> httpx.Response:
        for attempt in (0, 1):
            resp = self._http.get(url, params=params, headers={"Authorization": f"Bearer {self._token()}"})
            if resp.status_code in (429, 503) and attempt == 0:
                time.sleep(min(float(resp.headers.get("Retry-After", "2")), 10))
                continue
            break
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", {})
                raise GraphError(resp.status_code, err.get("code", "error"), err.get("message", resp.reason_phrase))
            except ValueError:
                raise GraphError(resp.status_code, "error", resp.reason_phrase) from None
        return resp

    def get_json(self, path: str, params: dict | None = None) -> dict:
        return self._request(path if path.startswith("http") else BASE + path, params).json()

    def get_bytes(self, path: str, max_bytes: int) -> bytes:
        # Stream so an oversized file is refused without downloading it all.
        url = BASE + path
        with self._http.stream("GET", url, headers={"Authorization": f"Bearer {self._token()}"}) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise GraphError(resp.status_code, "error", resp.reason_phrase)
            chunks, total = [], 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise GraphError(413, "tooLarge", f"File exceeds the {max_bytes}-byte limit")
                chunks.append(chunk)
            return b"".join(chunks)

    def _send(self, method: str, path: str, *, params=None, headers=None, content=None, json=None) -> dict:
        h = {"Authorization": f"Bearer {self._token()}", **(headers or {})}
        resp = self._http.request(method, BASE + path, params=params, headers=h, content=content, json=json)
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", {})
                raise GraphError(resp.status_code, err.get("code", "error"), err.get("message", resp.reason_phrase))
            except ValueError:
                raise GraphError(resp.status_code, "error", resp.reason_phrase) from None
        return resp.json() if resp.content else {}

    def put_bytes(self, path: str, data: bytes, params: dict | None = None, headers: dict | None = None) -> dict:
        return self._send("PUT", path, params=params, headers={"Content-Type": "application/octet-stream", **(headers or {})}, content=data)

    def post_json(self, path: str, body: dict) -> dict:
        return self._send("POST", path, json=body)
