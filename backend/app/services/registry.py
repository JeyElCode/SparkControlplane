"""Container-registry tag discovery (Docker Registry HTTP API v2).

Anonymous bearer-token flow: GET tags/list, and on 401 parse the
``WWW-Authenticate`` challenge, fetch a pull-scope token, retry. Works for
nvcr.io (NGC), ghcr.io, and Docker Hub (registry-1.docker.io with the
``library/`` prefix for official images).
"""

from __future__ import annotations

import re

import httpx


def split_image(image: str) -> tuple[str, str, str | None]:
    """'nvcr.io/nvidia/vllm:26.05-py3' -> (registry, repository, tag)."""
    ref, tag = image, None
    colon = image.rfind(":")
    if colon > image.rfind("/"):  # a ':' after the last '/' is a tag, not a port
        ref, tag = image[:colon], image[colon + 1:]
    first, _, rest = ref.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, repo = first, rest
    else:
        registry, repo = "registry-1.docker.io", ref
        if "/" not in repo:
            repo = f"library/{repo}"
    return registry, repo, tag


_CHALLENGE_RE = re.compile(r'(\w+)="([^"]*)"')


async def list_tags(image: str, limit: int = 40) -> dict:
    """Newest-first tag list for the image's repository."""
    registry, repo, current = split_image(image)
    url = f"https://{registry}/v2/{repo}/tags/list"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(url)
        if resp.status_code == 401:
            fields = dict(_CHALLENGE_RE.findall(resp.headers.get("www-authenticate", "")))
            realm = fields.get("realm")
            if not realm:
                resp.raise_for_status()
            params = {"scope": f"repository:{repo}:pull"}
            if fields.get("service"):
                params["service"] = fields["service"]
            tok = await client.get(realm, params=params)
            tok.raise_for_status()
            token = tok.json().get("token") or tok.json().get("access_token")
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        tags = resp.json().get("tags") or []
    tags.sort(key=_natural_key, reverse=True)
    return {"image": image, "repository": f"{registry}/{repo}", "current_tag": current,
            "tags": tags[:limit]}


def _natural_key(tag: str) -> list:
    """Sortable key: numeric chunks compare numerically ('26.10' > '26.5')."""
    return [
        (1, int(chunk)) if chunk.isdigit() else (0, chunk)
        for chunk in re.split(r"(\d+)", tag.lower())
        if chunk != ""
    ]
