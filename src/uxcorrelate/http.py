"""Thin httpx wrapper with retries and readable errors."""

from __future__ import annotations

import time

import httpx

RETRY_STATUS = {429, 500, 502, 503, 504}


class ApiError(RuntimeError):
    pass


class RestClient:
    def __init__(
        self,
        name: str,
        base_url: str,
        username: str = "",
        password: str = "",
        timeout: float = 60.0,
        retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ):
        if not base_url:
            raise ApiError(f"{name}: base_url is not set; add {name.lower()}.base_url to config.yaml "
                           "(see config.example.yaml)")
        self.name = name
        self.retries = retries
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=(username, password) if username else None,
            verify=False,  # Riverbed hosts often use self-signed certificates
            timeout=timeout,
            headers={"Accept": "application/json"},
            transport=transport,
        )

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        for attempt in range(self.retries + 1):
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt == self.retries:
                    raise ApiError(f"{self.name}: {method} {url} failed: {exc}") from exc
                time.sleep(2**attempt)
                continue
            if response.status_code in RETRY_STATUS and attempt < self.retries:
                time.sleep(2**attempt)
                continue
            if response.is_error:
                raise ApiError(
                    f"{self.name}: {method} {response.request.url} -> "
                    f"HTTP {response.status_code}: {response.text[:300]}"
                )
            return response
        raise AssertionError("unreachable")

    def get_json(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs).json()

    def post_json(self, url: str, body, **kwargs):
        return self.request("POST", url, json=body, **kwargs).json()

    def close(self) -> None:
        self._client.close()
