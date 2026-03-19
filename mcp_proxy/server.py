"""Build a composed FastMCP proxy server from a ProxyConfig."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import anyio
import httpx
from fastmcp import Client, FastMCP
from fastmcp.client.auth import OAuth
from fastmcp.client.transports.http import StreamableHttpTransport
from fastmcp.client.transports.stdio import StdioTransport
from fastmcp.server import create_proxy
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from mcp.shared.auth import OAuthToken
from pydantic import ValidationError

from .config.schema import (
    FilterPluginConfig,
    HiveAccessPluginConfig,
    HttpTransportConfig,
    InventoryPluginConfig,
    LoggingPluginConfig,
    NotionAccessPluginConfig,
    OAuthConfig,
    PluginConfig,
    ProxyConfig,
    RewritePluginConfig,
    StdioTransportConfig,
    UpstreamConfig,
)
from .plugins.adapter import PluginChainMiddleware
from .plugins.base import PluginBase
from .plugins.filter_plugin import FilterPlugin
from .plugins.hive_access_plugin import HiveAccessPlugin
from .plugins.inventory_plugin import InventoryPlugin
from .plugins.logging_plugin import JsonlLoggingPlugin
from .plugins.notion_access_plugin import NotionAccessPlugin
from .plugins.rewrite_plugin import RewritePlugin


class _RefreshOnStartOAuth(OAuth):
    """Work around a fastmcp bug where stored tokens are assumed valid on load.

    fastmcp's ``OAuth._initialize`` calls ``update_token_expiry(current_tokens)``
    which recomputes expiry as ``now + expires_in``, making a stale access token
    appear fresh for another full TTL.  When the token is then rejected (401),
    the auth flow jumps straight to a full browser OAuth instead of trying the
    refresh token first.

    This subclass marks loaded tokens as already expired so the first request
    always hits the refresh-token path.  It also retries a 401 with the stored
    refresh token once before allowing FastMCP to escalate to browser auth.
    """

    async def _initialize(self) -> None:
        await super()._initialize()
        if self.context.current_tokens:
            self.context.token_expiry_time = 0

    async def _handle_refresh_response(self, response: httpx.Response) -> bool:
        """Preserve the prior refresh token if the refresh response omits it."""
        if response.status_code != 200:
            return await super()._handle_refresh_response(response)

        previous_refresh_token = (
            self.context.current_tokens.refresh_token if self.context.current_tokens else None
        )

        try:
            content = await response.aread()
            token_response = OAuthToken.model_validate_json(content)
            if not token_response.refresh_token and previous_refresh_token:
                token_response = token_response.model_copy(
                    update={"refresh_token": previous_refresh_token}
                )

            self.context.current_tokens = token_response
            self.context.update_token_expiry(token_response)
            await self.context.storage.set_tokens(token_response)
            return True
        except ValidationError:
            self.context.clear_tokens()
            return False

    @staticmethod
    def _clone_request(request: httpx.Request) -> httpx.Request:
        """Build a resendable copy of an HTTPX request."""
        headers = dict(request.headers)
        headers.pop("host", None)
        headers.pop("content-length", None)
        return httpx.Request(
            request.method,
            request.url,
            headers=headers,
            content=request.content,
            extensions=request.extensions.copy(),
        )

    async def async_auth_flow(self, request: httpx.Request):
        """Retry a 401 once with refresh-token auth before browser OAuth."""
        async with contextlib.aclosing(super().async_auth_flow(request)) as gen:
            response = None
            while True:
                try:
                    yielded_request = await gen.asend(response)  # ty: ignore[invalid-argument-type]
                except StopAsyncIteration:
                    break

                if yielded_request is not request:
                    response = yield yielded_request
                    continue

                response = yield yielded_request
                if response.status_code != 401 or not self.context.can_refresh_token():
                    continue

                refresh_request = await self._refresh_token()
                refresh_response = yield refresh_request
                if not await self._handle_refresh_response(refresh_response):
                    continue

                retry_request = self._clone_request(request)
                self._add_auth_header(retry_request)
                response = yield retry_request


def _build_oauth(cfg: OAuthConfig, upstream_name: str) -> _RefreshOnStartOAuth:
    storage_dir = (Path(".oauth2") / upstream_name).resolve()
    storage_dir.mkdir(parents=True, exist_ok=True)
    store = FileTreeStore(
        data_directory=storage_dir,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(storage_dir),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(storage_dir),
    )
    return _RefreshOnStartOAuth(
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        scopes=cfg.scopes,
        token_storage=store,
        callback_port=cfg.callback_port or 53997,
    )


def _build_plugin(config: PluginConfig) -> PluginBase:
    if isinstance(config, LoggingPluginConfig):
        return JsonlLoggingPlugin(config)
    elif isinstance(config, FilterPluginConfig):
        return FilterPlugin(config)
    elif isinstance(config, RewritePluginConfig):
        return RewritePlugin(config)
    elif isinstance(config, InventoryPluginConfig):
        return InventoryPlugin(config)
    elif isinstance(config, NotionAccessPluginConfig):
        return NotionAccessPlugin(config)
    elif isinstance(config, HiveAccessPluginConfig):
        return HiveAccessPlugin(config)
    raise ValueError(f"Unknown plugin type: {config.type}")  # type: ignore[union-attr]


def _build_transport(upstream: UpstreamConfig) -> StdioTransport | StreamableHttpTransport:
    cfg = upstream.transport
    if isinstance(cfg, StdioTransportConfig):
        return StdioTransport(
            command=cfg.command,
            args=cfg.args,
            env=cfg.env or None,
            cwd=cfg.cwd,
        )
    elif isinstance(cfg, HttpTransportConfig):
        auth = _build_oauth(cfg.oauth, upstream.name) if cfg.oauth is not None else None

        def http2_client_factory(
            headers: dict | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
            **_kwargs,
        ) -> httpx.AsyncClient:
            from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT

            return httpx.AsyncClient(
                headers=headers or {},
                timeout=timeout
                or httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
                auth=auth,
                follow_redirects=True,
                http2=True,
            )

        return StreamableHttpTransport(
            url=cfg.url,
            headers=cfg.headers or None,
            auth=auth,
            httpx_client_factory=http2_client_factory,
        )
    raise ValueError(f"Unknown transport type: {cfg.type}")  # type: ignore[union-attr]


def build_server(
    config: ProxyConfig,
    connected_clients: dict[str, Client] | None = None,
) -> FastMCP:
    """Construct the aggregating FastMCP proxy server from a ProxyConfig.

    Architecture:
    - A top-level ``FastMCP`` server acts as the aggregator.
    - Global plugins are attached to the aggregator as middleware (outermost layer).
    - Each upstream gets its own proxy sub-server (via ``create_proxy``).
    - Per-upstream plugins are attached to the sub-server before mounting.
    - Sub-servers are mounted onto the aggregator with their configured namespace.
    - After building the chain, each plugin's ``register_tools`` is called on the
      aggregator so plugins can inject synthetic tools alongside the proxied ones.

    If ``connected_clients`` is provided, HTTP upstreams with ``persistent_connection``
    enabled will use those pre-connected clients instead of creating fresh sessions per
    request.
    """
    main = FastMCP(config.proxy.name)

    if config.global_plugins:
        plugins = [_build_plugin(c) for c in config.global_plugins]
        main.add_middleware(PluginChainMiddleware(plugins))
        for p in plugins:
            p.register_tools(main)

    for upstream in config.upstreams:
        # Use a pre-connected client if available (persistent_connection=true), else transport.
        target: Any
        if connected_clients and upstream.name in connected_clients:
            target = connected_clients[upstream.name]
        else:
            target = _build_transport(upstream)
        sub = create_proxy(target, name=upstream.name)

        if upstream.plugins:
            plugins = [_build_plugin(c) for c in upstream.plugins]
            sub.add_middleware(PluginChainMiddleware(plugins))
            client = connected_clients.get(upstream.name) if connected_clients else None
            for p in plugins:
                if client is not None:
                    p.set_upstream_client(client)
                p.register_tools(main)

        main.mount(sub, namespace=upstream.namespace)

    return main


async def _run_server_async(config: ProxyConfig) -> None:
    """Async entry point: connects persistent clients, builds, and runs the server."""
    async with contextlib.AsyncExitStack() as stack:
        connected_clients: dict[str, Client] = {}
        for upstream in config.upstreams:
            if isinstance(upstream.transport, HttpTransportConfig) and (
                upstream.transport.persistent_connection
                or any(isinstance(p, NotionAccessPluginConfig) for p in upstream.plugins)
            ):
                transport = _build_transport(upstream)
                client: Client = await stack.enter_async_context(Client(transport))
                connected_clients[upstream.name] = client

        server = build_server(config, connected_clients=connected_clients or None)
        transport = config.proxy.transport
        if transport == "stdio":
            await server.run_async(transport="stdio")
        else:
            await server.run_async(
                transport=transport,
                host=config.proxy.host,
                port=config.proxy.port,
            )


def run_server(config: ProxyConfig) -> None:
    """Build and run the proxy server according to config."""
    anyio.run(_run_server_async, config)
