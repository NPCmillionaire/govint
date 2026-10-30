"""Shared HTTP client: SQLite response cache, per-host pacing, retries on 429/5xx."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

# Minimum seconds between requests to a host. SEC allows 10 req/s.
HOST_SPACING = {"www.sec.gov": 0.12, "data.sec.gov": 0.12, "efts.sec.gov": 0.12,
                "disclosures-clerk.house.gov": 0.25}
DEFAULT_TTL = 24 * 3600


class SourceError(Exception):
    """A source failed in a way the user should hear about (bad key, rate limit, outage)."""


class Cache:
    def __init__(self, path: Path | None):
        self._lock = threading.Lock()
        self._db = None
        if path is not None:
            self._db = sqlite3.connect(path, check_same_thread=False)
            self._db.execute(
                "create table if not exists http (key text primary key, at real, body text)"
            )

    def get(self, key: str, ttl: float) -> str | None:
        if self._db is None:
            return None
        with self._lock:
            row = self._db.execute("select at, body from http where key=?", (key,)).fetchone()
        if row and time.time() - row[0] < ttl:
            return row[1]
        return None

    def put(self, key: str, body: str) -> None:
        if self._db is None:
            return
        with self._lock:
            self._db.execute(
                "insert or replace into http values (?,?,?)", (key, time.time(), body)
            )
            self._db.commit()


class Http:
    def __init__(self, user_agent: str, cache_path: Path | None = None, use_cache: bool = True):
        self.client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )
        self.cache = Cache(cache_path if use_cache else None)
        self._last: dict[str, float] = {}
        self._pace_lock = threading.Lock()
        self.spacing = dict(HOST_SPACING)

    def _pace(self, host: str) -> None:
        spacing = self.spacing.get(host)
        if not spacing:
            return
        with self._pace_lock:
            wait = self._last.get(host, 0) + spacing - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: Any = None,
        ttl: float = DEFAULT_TTL,
        parse: str = "json",
        headers: dict | None = None,
    ) -> Any:
        key_src = json.dumps([method, url, params, json_body], sort_keys=True, default=str)
        # Never let API keys leak into cache keys in readable form; the hash covers it.
        key = hashlib.sha256(key_src.encode()).hexdigest()
        if (hit := self.cache.get(key, ttl)) is not None:
            return json.loads(hit) if parse == "json" else hit

        host = urlparse(url).hostname or ""
        resp = None
        for attempt in range(4):
            self._pace(host)
            try:
                resp = self.client.request(method, url, params=params, json=json_body, headers=headers)
            except httpx.HTTPError as e:
                if attempt == 3:
                    raise SourceError(f"{host}: {e.__class__.__name__}: {e}") from e
                time.sleep(1.5 * 2**attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == 3:
                    break
                retry_after = resp.headers.get("retry-after", "")
                if resp.status_code == 429 and retry_after.isdigit() and int(retry_after) > 10:
                    break  # long rate-limit window: fail now instead of hanging
                time.sleep(float(retry_after) if retry_after.isdigit() else 1.5 * 2**attempt)
                continue
            break

        assert resp is not None
        if resp.status_code == 429:
            raise SourceError(f"{host}: rate limited (HTTP 429)")
        if resp.status_code == 403 and host.endswith("sec.gov"):
            raise SourceError(
                "SEC rejected the request (HTTP 403). Set a contact email: "
                "`govint config contact you@example.com` or GOVINT_CONTACT."
            )
        if resp.status_code >= 400:
            raise SourceError(f"{host}: HTTP {resp.status_code}")

        body = resp.text
        data = resp.json() if parse == "json" else body
        self.cache.put(key, body)
        return data

    def get(self, url: str, **kw: Any) -> Any:
        return self.request("GET", url, **kw)

    def post(self, url: str, body: Any, **kw: Any) -> Any:
        return self.request("POST", url, json_body=body, **kw)

    def download(self, url: str, dest: Path) -> Path:
        """Stream a large file to disk (no caching)."""
        tmp = dest.with_suffix(dest.suffix + ".part")
        with self.client.stream("GET", url, timeout=httpx.Timeout(300.0, connect=15.0)) as r:
            if r.status_code >= 400:
                raise SourceError(f"{urlparse(url).hostname}: HTTP {r.status_code}")
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    f.write(chunk)
        tmp.replace(dest)
        return dest
