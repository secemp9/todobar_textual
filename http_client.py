from __future__ import annotations

import json
import urllib.error
import urllib.request

from .net_models import ServerInfo


def format_server_url(raw_url: str, *, default_url: str) -> str:
    """Mimic the TS getFormattedServerUrl behavior."""
    raw_url = raw_url or ""
    if raw_url == "":
        raw_url = default_url
    if not raw_url.endswith("/"):
        raw_url += "/"
    return raw_url


def fetch_server_info(server_api_url: str) -> ServerInfo:
    url = f"{server_api_url}info"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode("utf-8")
            if resp.status != 200:
                raise RuntimeError(f"{resp.status}: {body}")
            data = json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e

    return ServerInfo.from_dict(data)


def create_api_key(info: ServerInfo, *, email: str, password: str) -> str:
    url = f"{info.auth_pub_api_href}api_key/new_with_email"
    body = json.dumps(
        {
            "email": email,
            "password": password,
            "duration": 7 * 24 * 60 * 60 * 1000,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            body_text = resp.read().decode("utf-8")
            if resp.status != 200:
                raise RuntimeError(f"{resp.status}: {body_text}")
            data = json.loads(body_text)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{e.code}: {body_text}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e

    key = data.get("key")
    if not isinstance(key, str) or not key:
        raise RuntimeError("No API key returned")
    return key
