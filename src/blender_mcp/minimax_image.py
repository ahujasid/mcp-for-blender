"""MiniMax text-to-image generation for reference images."""

import os
from urllib.parse import urlparse

import httpx

ENDPOINTS = {'global_en': 'https://api.minimax.io/v1/image_generation', 'cn_zh': 'https://api.minimaxi.com/v1/image_generation'}
MODEL = 'image-01'


async def generate_image(prompt: str, region: str = "global_en") -> dict:
    """Generate one image without requiring a Blender connection."""
    if not prompt.strip():
        raise ValueError("A non-empty prompt is required.")
    if region not in ENDPOINTS:
        raise ValueError("region must be global_en or cn_zh.")
    api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not api_key:
        raise ValueError("Set MINIMAX_API_KEY in the MCP server environment.")
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                ENDPOINTS[region],
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": MODEL, "prompt": prompt, "response_format": "url", "n": 1},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        raise ValueError("MiniMax image generation request failed; check credentials and service availability.") from None
    if not isinstance(payload, dict):
        raise ValueError("MiniMax returned an invalid image response.")
    status = payload.get("base_resp")
    if not isinstance(status, dict) or status.get("status_code") != 0:
        raise ValueError("MiniMax image generation was unsuccessful.")
    data = payload.get("data")
    urls = data.get("image_urls") if isinstance(data, dict) else None
    if not isinstance(urls, list) or not urls or any(
        not isinstance(url, str) or urlparse(url).scheme not in ("http", "https")
        or not urlparse(url).netloc for url in urls
    ):
        raise ValueError("MiniMax returned no usable image URLs.")
    return {"image_urls": urls, "expires_in_hours": 24}
