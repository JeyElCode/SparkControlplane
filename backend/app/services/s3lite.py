"""Minimal S3 client (AWS Signature V4) — no boto3.

Supports exactly what backups need: PUT/GET/DELETE object and ListObjectsV2,
path-style URLs (`https://endpoint/bucket/key`) so MinIO and friends work out
of the box. Pure functions for signing so the algorithm is unit-testable
against AWS's published example vectors.
"""

from __future__ import annotations

import hashlib
import hmac
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _uri_encode(s: str, *, encode_slash: bool) -> str:
    safe = "-._~" + ("" if encode_slash else "/")
    return urllib.parse.quote(s, safe=safe)


def sign_request(
    *,
    method: str,
    host: str,
    path: str,
    query: dict[str, str],
    region: str,
    access_key: str,
    secret_key: str,
    payload_sha256: str,
    amz_date: str,  # 20130524T000000Z
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """Returns the headers (incl. Authorization) for a SigV4 S3 request."""
    date_stamp = amz_date[:8]
    headers = {
        "host": host,
        "x-amz-content-sha256": payload_sha256,
        "x-amz-date": amz_date,
        **{k.lower(): v for k, v in (extra_headers or {}).items()},
    }
    signed_names = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in sorted(headers))
    canonical_query = "&".join(
        f"{_uri_encode(k, encode_slash=True)}={_uri_encode(v, encode_slash=True)}"
        for k, v in sorted(query.items())
    )
    canonical_request = "\n".join([
        method,
        _uri_encode(path, encode_slash=False),
        canonical_query,
        canonical_headers,
        signed_names,
        payload_sha256,
    ])
    scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    k = _hmac(("AWS4" + secret_key).encode(), date_stamp)
    k = _hmac(k, region)
    k = _hmac(k, "s3")
    k = _hmac(k, "aws4_request")
    signature = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()
    auth = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_names}, Signature={signature}"
    )
    out = dict(headers)
    out["Authorization"] = auth
    return out


@dataclass
class S3Config:
    endpoint: str      # https://minio.example:9000 or https://s3.eu-north-1.amazonaws.com
    bucket: str
    region: str
    access_key: str
    secret_key: str


class S3Client:
    def __init__(self, cfg: S3Config, timeout: float = 30.0, transport=None) -> None:
        self.cfg = cfg
        self.timeout = timeout
        self.transport = transport  # injectable for tests (httpx transport)
        parsed = urllib.parse.urlparse(cfg.endpoint)
        self.host = parsed.netloc
        self.base = cfg.endpoint.rstrip("/")

    def _now(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    async def _request(
        self, method: str, key: str = "", query: dict[str, str] | None = None,
        body: bytes = b"", extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        path = f"/{self.cfg.bucket}" + (f"/{key}" if key else "")
        payload_hash = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
        headers = sign_request(
            method=method, host=self.host, path=path, query=query or {},
            region=self.cfg.region, access_key=self.cfg.access_key,
            secret_key=self.cfg.secret_key, payload_sha256=payload_hash,
            amz_date=self._now(), extra_headers=extra_headers,
        )
        url = self.base + path
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            resp = await client.request(
                method, url, params=query or {}, content=body, headers=headers
            )
        return resp

    async def put_object(self, key: str, body: bytes, content_type: str = "application/json") -> None:
        r = await self._request("PUT", key, body=body,
                                extra_headers={"content-type": content_type})
        if r.status_code >= 300:
            raise RuntimeError(f"S3 PUT {key} failed: HTTP {r.status_code} {r.text[:300]}")

    async def get_object(self, key: str) -> bytes:
        r = await self._request("GET", key)
        if r.status_code >= 300:
            raise RuntimeError(f"S3 GET {key} failed: HTTP {r.status_code} {r.text[:300]}")
        return r.content

    async def delete_object(self, key: str) -> None:
        r = await self._request("DELETE", key)
        if r.status_code >= 300 and r.status_code != 404:
            raise RuntimeError(f"S3 DELETE {key} failed: HTTP {r.status_code} {r.text[:300]}")

    async def list_objects(self, prefix: str) -> list[dict]:
        """[{key, size, last_modified}] under prefix (single page, 1000 max)."""
        r = await self._request("GET", query={"list-type": "2", "prefix": prefix})
        if r.status_code >= 300:
            raise RuntimeError(f"S3 LIST failed: HTTP {r.status_code} {r.text[:300]}")
        root = ET.fromstring(r.text)
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"
        out = []
        for item in root.findall(f"{ns}Contents"):
            out.append({
                "key": item.findtext(f"{ns}Key") or "",
                "size": int(item.findtext(f"{ns}Size") or 0),
                "last_modified": item.findtext(f"{ns}LastModified") or "",
            })
        return out
