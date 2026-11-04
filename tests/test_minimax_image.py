"""Exercise the image API contract without network access or credentials."""

import asyncio
import json

import httpx
import pytest

from blender_mcp import minimax_image, server


def install_transport(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    monkeypatch.setattr(minimax_image.httpx, "AsyncClient", lambda **kwargs: client(
        transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.parametrize("region,host", [("global_en", "api.minimax.io"), ("cn_zh", "api.minimaxi.com")])
def test_tool_posts_prompt_and_returns_urls(monkeypatch, region, host):
    def handler(request):
        assert request.method == "POST"
        assert request.url.host == host
        assert request.url.path == "/v1/image_generation"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.content) == {
            "model": "image-01", "prompt": "A ceramic vase", "response_format": "url", "n": 1,
        }
        return httpx.Response(200, json={"base_resp": {"status_code": 0},
                                         "data": {"image_urls": ["https://example.com/image.png"]}})
    install_transport(monkeypatch, handler)
    assert asyncio.run(server.generate_image("A ceramic vase", region)) == {
        "image_urls": ["https://example.com/image.png"], "expires_in_hours": 24,
    }


@pytest.mark.parametrize("payload", [None, {}, {"base_resp": {"status_code": 1004}},
    {"base_resp": {"status_code": 0}, "data": {"image_urls": []}},
    {"base_resp": {"status_code": 0}, "data": {"image_urls": ["file:///tmp/image.png"]}},
    {"base_resp": {"status_code": 0}, "data": {"image_urls": [None]}}])
def test_bad_response_rejected(monkeypatch, payload):
    install_transport(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ValueError):
        asyncio.run(minimax_image.generate_image("A vase"))


@pytest.mark.parametrize("status", [401, 429, 500])
def test_http_errors_do_not_expose_response_body(monkeypatch, status):
    install_transport(monkeypatch, lambda request: httpx.Response(status, text="private-service-details"))
    with pytest.raises(ValueError) as error:
        asyncio.run(minimax_image.generate_image("A vase"))
    assert "private-service-details" not in str(error.value)
    assert "test-key" not in str(error.value)


def test_timeout_not_retried(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("private-service-details")
    install_transport(monkeypatch, handler)
    with pytest.raises(ValueError):
        asyncio.run(minimax_image.generate_image("A vase"))
    assert len(calls) == 1


@pytest.mark.parametrize("prompt,region", [(" ", "global_en"), ("A vase", "unknown")])
def test_invalid_input_before_network(monkeypatch, prompt, region):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    with pytest.raises(ValueError):
        asyncio.run(minimax_image.generate_image(prompt, region))


def test_missing_key(monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    with pytest.raises(ValueError, match="MINIMAX_API_KEY"):
        asyncio.run(minimax_image.generate_image("A vase"))
