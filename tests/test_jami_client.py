"""Tests for jami_client module — JSON-RPC helpers and client."""

import json
from unittest.mock import MagicMock, patch

from jami_client import JamiStdioClient, is_notification, is_response, jsonrpc_request


class TestJsonrpcRequest:
    def test_basic_request(self):
        req = jsonrpc_request("ping", id=1)
        assert req["jsonrpc"] == "2.0"
        assert req["method"] == "ping"
        assert req["id"] == 1
        assert "params" not in req

    def test_request_with_params(self):
        req = jsonrpc_request("sendMessage", {"body": "hi"}, id=2)
        assert req["params"] == {"body": "hi"}

    def test_notification(self):
        req = jsonrpc_request("onMessageReceived", {"from": "alice"})
        assert "id" not in req


class TestIsResponse:
    def test_result(self):
        assert is_response({"result": {"status": "ok"}, "id": 1}) is True

    def test_error(self):
        assert is_response({"error": {"code": -1}, "id": 1}) is True

    def test_notification(self):
        assert is_response({"method": "onReady"}) is False


class TestIsNotification:
    def test_notification(self):
        assert is_notification({"method": "onReady"}) is True

    def test_response(self):
        assert is_notification({"result": {}, "id": 1}) is False


class TestJamiStdioClient:
    def test_init_defaults(self):
        client = JamiStdioClient()
        assert client.jami_binary == "jami-bridge"
        assert client.bridge_args == []
        assert client.verbose_bridge is False
        assert client.next_id == 1

    def test_init_custom(self):
        client = JamiStdioClient(
            jami_binary="/usr/local/bin/jami-bridge",
            bridge_args=["--auto-accept"],
            verbose_bridge=True,
        )
        assert client.jami_binary == "/usr/local/bin/jami-bridge"
        assert client.bridge_args == ["--auto-accept"]

    def test_dispatch_response(self):
        client = JamiStdioClient()
        import threading
        event = threading.Event()
        client.pending[1] = event
        client.pending_results[1] = None
        client._dispatch({"jsonrpc": "2.0", "id": 1, "result": {"accounts": []}})
        assert client.pending_results[1]["result"] == {"accounts": []}

    def test_dispatch_notification(self):
        client = JamiStdioClient()
        client._dispatch({"jsonrpc": "2.0", "method": "onReady"})
        result = client.notifications.get_nowait()
        assert result["method"] == "onReady"