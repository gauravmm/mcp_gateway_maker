"""Tests for config loading and schema validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mcp_proxy.config.loader import _expand_env_vars, load_config
from mcp_proxy.config.schema import (
    FilterPluginConfig,
    HttpTransportConfig,
    LoggingPluginConfig,
    RewritePluginConfig,
    StdioTransportConfig,
)

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "proxy.yaml"
    p.write_text(textwrap.dedent(content))
    return p


# ---------------------------------------------------------------------------
# Env var expansion
# ---------------------------------------------------------------------------


def test_expand_env_vars_simple(monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    assert _expand_env_vars("hello ${FOO}") == "hello bar"


def test_expand_env_vars_nested(monkeypatch):
    monkeypatch.setenv("TOKEN", "secret")
    data = {"headers": {"Authorization": "Bearer ${TOKEN}"}}
    result = _expand_env_vars(data)
    assert result == {"headers": {"Authorization": "Bearer secret"}}


def test_expand_env_vars_missing_raises():
    with pytest.raises(ValueError, match="DOES_NOT_EXIST"):
        _expand_env_vars("${DOES_NOT_EXIST}")


# ---------------------------------------------------------------------------
# Basic proxy config
# ---------------------------------------------------------------------------


def test_basic_proxy_config(tmp_path):
    p = write_yaml(
        tmp_path,
        """
        proxy:
          name: test-proxy
          transport: stdio
        upstreams:
          - name: backend
            transport:
              type: stdio
              command: python
              args: ["-m", "some_server"]
        """,
    )
    config = load_config(p)
    assert config.proxy.name == "test-proxy"
    assert config.proxy.transport == "stdio"
    assert len(config.upstreams) == 1
    assert config.upstreams[0].name == "backend"
    assert isinstance(config.upstreams[0].transport, StdioTransportConfig)
    assert config.upstreams[0].transport.command == "python"
    assert config.upstreams[0].transport.args == ["-m", "some_server"]


def test_http_upstream(tmp_path):
    p = write_yaml(
        tmp_path,
        """
        upstreams:
          - name: remote
            transport:
              type: http
              url: "http://localhost:9000/mcp"
              headers:
                X-Custom: value
        """,
    )
    config = load_config(p)
    t = config.upstreams[0].transport
    assert isinstance(t, HttpTransportConfig)
    assert t.url == "http://localhost:9000/mcp"
    assert t.headers == {"X-Custom": "value"}


# ---------------------------------------------------------------------------
# Plugin configs
# ---------------------------------------------------------------------------


def test_logging_plugin_config(tmp_path):
    p = write_yaml(
        tmp_path,
        """
        upstreams:
          - name: x
            transport:
              type: stdio
              command: cat
              args: []
            plugins:
              - type: logging
                log_file: /tmp/test.jsonl
                include_payloads: false
                methods: ["tools/call"]
        """,
    )
    config = load_config(p)
    plugin = config.upstreams[0].plugins[0]
    assert isinstance(plugin, LoggingPluginConfig)
    assert plugin.log_file == "/tmp/test.jsonl"
    assert plugin.include_payloads is False
    assert plugin.methods == ["tools/call"]


def test_filter_plugin_allow(tmp_path):
    p = write_yaml(
        tmp_path,
        """
        upstreams:
          - name: x
            transport:
              type: stdio
              command: cat
              args: []
            plugins:
              - type: filter
                allow_tools: ["read_*", "list_*"]
        """,
    )
    config = load_config(p)
    plugin = config.upstreams[0].plugins[0]
    assert isinstance(plugin, FilterPluginConfig)
    assert plugin.allow_tools == ["read_*", "list_*"]
    assert plugin.block_tools is None


def test_filter_plugin_allow_block_exclusive(tmp_path):
    p = write_yaml(
        tmp_path,
        """
        upstreams:
          - name: x
            transport:
              type: stdio
              command: cat
              args: []
            plugins:
              - type: filter
                allow_tools: ["read_*"]
                block_tools: ["write_*"]
        """,
    )
    with pytest.raises(Exception, match="mutually exclusive"):
        load_config(p)


def test_rewrite_plugin_config(tmp_path):
    p = write_yaml(
        tmp_path,
        """
        upstreams:
          - name: x
            transport:
              type: stdio
              command: cat
              args: []
            plugins:
              - type: rewrite
                tool_renames:
                  read_file: read_document
                argument_overrides:
                  read_file:
                    encoding: utf-8
                response_prefix: "Result: "
        """,
    )
    config = load_config(p)
    plugin = config.upstreams[0].plugins[0]
    assert isinstance(plugin, RewritePluginConfig)
    assert plugin.tool_renames == {"read_file": "read_document"}
    assert plugin.argument_overrides == {"read_file": {"encoding": "utf-8"}}
    assert plugin.response_prefix == "Result: "


def test_global_plugins(tmp_path):
    p = write_yaml(
        tmp_path,
        """
        global_plugins:
          - type: logging
            log_file: /tmp/global.jsonl
        upstreams:
          - name: x
            transport:
              type: stdio
              command: cat
              args: []
        """,
    )
    config = load_config(p)
    assert len(config.global_plugins) == 1
    assert isinstance(config.global_plugins[0], LoggingPluginConfig)


def test_namespace(tmp_path):
    p = write_yaml(
        tmp_path,
        """
        upstreams:
          - name: fs
            namespace: filesystem
            transport:
              type: stdio
              command: cat
              args: []
        """,
    )
    config = load_config(p)
    assert config.upstreams[0].namespace == "filesystem"
