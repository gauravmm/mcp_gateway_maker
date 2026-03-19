from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace

import httpx
import pytest
from fastmcp.client.auth import OAuth
from mcp.shared.auth import OAuthToken

from mcp_proxy.server import _RefreshOnStartOAuth


class _DummyContext:
    def __init__(self, tokens: OAuthToken | None = None):
        self.current_tokens = tokens
        self.token_expiry_time = None
        self.saved_tokens: OAuthToken | None = None
        self.storage = SimpleNamespace(set_tokens=self._set_tokens)

    async def _set_tokens(self, tokens: OAuthToken) -> None:
        self.saved_tokens = tokens

    def update_token_expiry(self, token: OAuthToken) -> None:
        self.token_expiry_time = 123 if token.expires_in else None

    def can_refresh_token(self) -> bool:
        return bool(self.current_tokens and self.current_tokens.refresh_token)


@pytest.mark.asyncio
async def test_refresh_on_401_before_browser_auth(monkeypatch):
    auth = object.__new__(_RefreshOnStartOAuth)
    auth._bound = True
    auth.context = _DummyContext(
        OAuthToken(access_token="stale-token", refresh_token="refresh-token", expires_in=3600)
    )

    protected_request = httpx.Request("GET", "https://example.com/mcp")
    refresh_request = httpx.Request("POST", "https://example.com/token")
    seen = {}

    async def fake_parent_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        seen["request"] = request
        response = yield request
        seen["response_status"] = response.status_code

    async def fake_refresh_token(self) -> httpx.Request:
        return refresh_request

    async def fake_handle_refresh_response(self, response: httpx.Response) -> bool:
        assert response.status_code == 200
        self.context.current_tokens = OAuthToken(
            access_token="fresh-token",
            refresh_token="fresh-refresh-token",
            expires_in=3600,
        )
        return True

    def fake_add_auth_header(self, request: httpx.Request) -> None:
        request.headers["Authorization"] = (
            f"Bearer {self.context.current_tokens.access_token}"
        )

    monkeypatch.setattr(OAuth, "async_auth_flow", fake_parent_flow)
    monkeypatch.setattr(_RefreshOnStartOAuth, "_refresh_token", fake_refresh_token)
    monkeypatch.setattr(
        _RefreshOnStartOAuth, "_handle_refresh_response", fake_handle_refresh_response
    )
    monkeypatch.setattr(_RefreshOnStartOAuth, "_add_auth_header", fake_add_auth_header)

    gen = auth.async_auth_flow(protected_request)

    first_request = await gen.asend(None)
    assert first_request is protected_request

    yielded_refresh = await gen.asend(httpx.Response(401, request=protected_request))
    assert yielded_refresh is refresh_request

    retried_request = await gen.asend(httpx.Response(200, request=refresh_request))
    assert retried_request is not protected_request
    assert retried_request.headers["Authorization"] == "Bearer fresh-token"

    with pytest.raises(StopAsyncIteration):
        await gen.asend(httpx.Response(200, request=retried_request))

    assert seen["request"] is protected_request
    assert seen["response_status"] == 200


@pytest.mark.asyncio
async def test_handle_refresh_response_preserves_existing_refresh_token():
    auth = object.__new__(_RefreshOnStartOAuth)
    auth.context = _DummyContext(
        OAuthToken(access_token="old-token", refresh_token="keep-me", expires_in=3600)
    )

    response = httpx.Response(
        200,
        json={
            "access_token": "new-token",
            "token_type": "Bearer",
            "expires_in": 3600,
        },
    )

    ok = await auth._handle_refresh_response(response)

    assert ok is True
    assert auth.context.current_tokens is not None
    assert auth.context.current_tokens.access_token == "new-token"
    assert auth.context.current_tokens.refresh_token == "keep-me"
    assert auth.context.saved_tokens is not None
    assert auth.context.saved_tokens.refresh_token == "keep-me"
