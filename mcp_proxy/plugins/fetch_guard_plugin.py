"""FetchGuard plugin: block dangerous URLs before they reach the upstream fetch tool.

Enforces two critical mitigations on any tool that accepts a URL argument:

1. **Scheme allowlist** — only ``http`` and ``https`` are permitted; ``file://``,
   ``ftp://``, ``data:``, etc. are rejected immediately.
2. **SSRF protection** — the target hostname is resolved to IP(s) and any
   loopback, private, link-local, or unspecified address is rejected.
"""

from __future__ import annotations

import asyncio
import ipaddress
from urllib.parse import urlparse

import mcp.types as mt
from mcp import McpError
from mcp.types import ErrorData

from ..config.schema import FetchGuardPluginConfig
from .base import PluginBase

_ERR_BLOCKED = -32601
_ALLOWED_SCHEMES = {"http", "https"}

# Hostnames that are always dangerous but may not resolve via normal DNS.
_BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {
        "metadata.google.internal",
        "metadata.internal",
    }
)


async def _resolve_host(hostname: str) -> list[tuple]:
    """Resolve *hostname* to a list of addrinfo tuples.

    Extracted as a module-level coroutine so tests can patch it easily.
    """
    loop = asyncio.get_running_loop()
    return await loop.getaddrinfo(hostname, None)


def _check_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    """Raise McpError if *ip* is an internal/reserved address."""
    if ip.is_loopback:
        raise McpError(ErrorData(code=_ERR_BLOCKED, message=f"SSRF blocked: loopback address {ip}"))
    if ip.is_private:
        raise McpError(
            ErrorData(code=_ERR_BLOCKED, message=f"SSRF blocked: private network address {ip}")
        )
    if ip.is_link_local:
        raise McpError(
            ErrorData(code=_ERR_BLOCKED, message=f"SSRF blocked: link-local address {ip}")
        )
    if ip.is_unspecified:
        raise McpError(
            ErrorData(code=_ERR_BLOCKED, message=f"SSRF blocked: unspecified address {ip}")
        )


async def _check_url(url: str) -> None:
    """Check *url* for scheme and SSRF safety. Raises McpError if unsafe."""
    parsed = urlparse(url)

    # 1. Scheme allowlist
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise McpError(
            ErrorData(
                code=_ERR_BLOCKED,
                message=(
                    f"URL scheme '{parsed.scheme}://' is not allowed; "
                    "only http and https are permitted"
                ),
            )
        )

    hostname = parsed.hostname
    if not hostname:
        raise McpError(ErrorData(code=_ERR_BLOCKED, message="URL has no hostname"))

    # 2. Block known-dangerous hostnames before DNS
    if hostname.lower() in _BLOCKED_HOSTNAMES:
        raise McpError(
            ErrorData(code=_ERR_BLOCKED, message=f"SSRF blocked: '{hostname}' is not allowed")
        )

    # 3. Resolve and check each returned IP
    try:
        addr_infos = await _resolve_host(hostname)
    except OSError:
        # DNS failure — the upstream will also fail; no need to block here
        return

    for *_, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        _check_ip(ip)


class FetchGuardPlugin(PluginBase):
    """Enforce URL scheme allowlist and SSRF protection on fetch-style tools.

    Inspects the ``url`` argument (configurable) of the named tools
    (default: ``["fetch"]``) on every ``tools/call`` request.
    """

    def __init__(self, config: FetchGuardPluginConfig) -> None:
        self._tool_names: frozenset[str] = frozenset(config.tool_names)
        self._url_arg: str = config.url_arg

    async def on_call_tool_request(
        self, params: mt.CallToolRequestParams
    ) -> mt.CallToolRequestParams:
        if params.name not in self._tool_names:
            return params
        arguments = params.arguments or {}
        url = arguments.get(self._url_arg)
        if url is not None:
            await _check_url(str(url))
        return params
