"""Image inputs to the generators can't read arbitrary files or reach the local network.

A path or URL given as a generation image is read or fetched and its bytes
sent to a generation service. Without checks, a path to a key file (#405) or
a URL on the user's own network (#406) leaks whatever is there.
"""
import base64
import socket

import pytest

from blender_mcp import generation
from blender_mcp.generation import GenerationError
from test_meshy import _load_addon

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 16
WEBP = b"RIFF\x10\0\0\0WEBPVP8 " + b"\0" * 8
SECRET = b"OPENAI_API_KEY=sk-not-an-image\n"


@pytest.mark.parametrize("data,expected", [
    (PNG, True), (JPEG, True), (WEBP, True),
    (SECRET, False), (b"", False), (b"RIFF\x10\0\0\0WAVEfmt ", False),
])
def test_image_signatures(data, expected):
    assert generation.is_image_bytes(data) is expected


# ------------------------------------------------------------ Hyper3D (server)

def test_rodin_refuses_a_file_that_is_not_an_image(tmp_path):
    secret = tmp_path / "credentials.png"
    secret.write_bytes(SECRET)
    send = lambda *_: pytest.fail("nothing may be sent")
    with pytest.raises(GenerationError, match="Not a PNG, JPEG or WebP"):
        generation.submit(send, "hyper3d", "Car", None, str(secret), None, None, True)


def test_rodin_refuses_a_folder_holding_a_non_image(tmp_path):
    (tmp_path / "front.png").write_bytes(PNG)
    (tmp_path / "side.png").write_bytes(SECRET)
    send = lambda *_: pytest.fail("nothing may be sent")
    with pytest.raises(GenerationError, match="Not a PNG, JPEG or WebP image: .*side.png"):
        generation.submit(send, "hyper3d", "Car", None, str(tmp_path), None, None, True)


def test_rodin_sends_a_real_image(tmp_path):
    image = tmp_path / "car.png"
    image.write_bytes(PNG)
    calls = []

    def send(command, params):
        calls.append((command, params))
        return {"request_id": "q1"}

    generation.submit(send, "hyper3d", "Car", None, str(image), None, None, True)
    assert calls[0][1]["images"] == [(".png", base64.b64encode(PNG).decode("ascii"))]


# ------------------------------------------------------------ addon

HUNYUAN_LOCAL = dict(
    blendermcp_use_hunyuan3d=True,
    blendermcp_hunyuan3d_api_url="http://localhost:8081",
    blendermcp_hunyuan3d_octree_resolution=256,
    blendermcp_hunyuan3d_num_inference_steps=20,
    blendermcp_hunyuan3d_guidance_scale=5.0,
    blendermcp_hunyuan3d_texture=False,
)


class _Response:
    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _resolve(monkeypatch, table):
    """Make getaddrinfo answer from `table` (host -> IP) instead of DNS."""
    def getaddrinfo(host, port, *a, **k):
        family = socket.AF_INET6 if ":" in table[host] else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (table[host], port or 0))]
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def _gets(addon, replies):
    """Serve requests.get from `replies` (url -> response), recording each URL."""
    fetched = []

    def get(url, **kwargs):
        assert kwargs.get("allow_redirects") is False, "redirects must be checked hop by hop"
        fetched.append(url)
        return replies[url]

    addon.requests.get = get
    return fetched


def _posts(addon):
    posts = []
    addon.requests.post = lambda url, **kw: posts.append((url, kw)) or _Response(500)
    return posts


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.20", "169.254.169.254",
                                "::1", "fe80::1", "::ffff:127.0.0.1", "0.0.0.0"])
def test_addon_refuses_local_and_private_image_hosts(monkeypatch, ip):
    addon, _bpy, _scene = _load_addon(monkeypatch, **HUNYUAN_LOCAL)
    _resolve(monkeypatch, {"images.example": ip})
    fetched = _gets(addon, {})
    with pytest.raises(ValueError, match="local or private"):
        addon._fetch_public_image("http://images.example/a.png")
    assert fetched == []


def test_addon_checks_every_redirect(monkeypatch):
    addon, _bpy, _scene = _load_addon(monkeypatch, **HUNYUAN_LOCAL)
    _resolve(monkeypatch, {"cdn.example": "93.184.216.34", "metadata.internal": "169.254.169.254"})
    fetched = _gets(addon, {
        "https://cdn.example/a.png": _Response(302, headers={"Location": "http://metadata.internal/latest"}),
    })
    with pytest.raises(ValueError, match="local or private"):
        addon._fetch_public_image("https://cdn.example/a.png")
    assert fetched == ["https://cdn.example/a.png"]


def test_addon_follows_public_redirects_to_an_image(monkeypatch):
    addon, _bpy, _scene = _load_addon(monkeypatch, **HUNYUAN_LOCAL)
    _resolve(monkeypatch, {"cdn.example": "93.184.216.34"})
    _gets(addon, {
        "https://cdn.example/a.png": _Response(301, headers={"Location": "/b.png"}),
        "https://cdn.example/b.png": _Response(200, PNG),
    })
    assert addon._fetch_public_image("https://cdn.example/a.png") == PNG


def test_addon_refuses_a_downloaded_non_image(monkeypatch):
    addon, _bpy, _scene = _load_addon(monkeypatch, **HUNYUAN_LOCAL)
    _resolve(monkeypatch, {"cdn.example": "93.184.216.34"})
    _gets(addon, {"https://cdn.example/a.png": _Response(200, SECRET)})
    with pytest.raises(ValueError, match="Not a PNG"):
        addon._fetch_public_image("https://cdn.example/a.png")


def test_addon_stops_redirect_loops(monkeypatch):
    addon, _bpy, _scene = _load_addon(monkeypatch, **HUNYUAN_LOCAL)
    _resolve(monkeypatch, {"cdn.example": "93.184.216.34"})
    _gets(addon, {"https://cdn.example/a.png": _Response(302, headers={"Location": "/a.png"})})
    with pytest.raises(ValueError, match="Too many redirects"):
        addon._fetch_public_image("https://cdn.example/a.png")


def test_hunyuan_local_never_fetches_a_loopback_url(monkeypatch):
    addon, _bpy, _scene = _load_addon(monkeypatch, **HUNYUAN_LOCAL)
    _resolve(monkeypatch, {"127.0.0.1": "127.0.0.1"})
    fetched = _gets(addon, {})
    posts = _posts(addon)

    result = addon.BlenderMCPServer().create_hunyuan_job_local_site(
        image="http://127.0.0.1:58527/internal-only.png")

    assert "local or private" in result["error"]
    assert fetched == [] and posts == []


def test_hunyuan_local_refuses_a_file_that_is_not_an_image(monkeypatch, tmp_path):
    addon, _bpy, _scene = _load_addon(monkeypatch, **HUNYUAN_LOCAL)
    secret = tmp_path / "id_rsa"
    secret.write_bytes(SECRET)
    posts = _posts(addon)

    result = addon.BlenderMCPServer().create_hunyuan_job_local_site(image=str(secret))

    assert "Not a PNG, JPEG or WebP" in result["error"]
    assert posts == []


def test_premium_refuses_a_file_that_is_not_an_image(monkeypatch, tmp_path):
    addon, _bpy, _scene = _load_addon(monkeypatch)
    secret = tmp_path / "config.png"
    secret.write_bytes(SECRET)
    with pytest.raises(ValueError, match="Not a PNG, JPEG or WebP"):
        addon._premium_image_payload(str(secret))


def test_premium_reads_a_real_image(monkeypatch, tmp_path):
    addon, _bpy, _scene = _load_addon(monkeypatch)
    image = tmp_path / "chair.jpg"
    image.write_bytes(JPEG)
    fields, size = addon._premium_image_payload(str(image))
    assert fields == {"image_base64": base64.b64encode(JPEG).decode("ascii"), "image_mime": "image/jpeg"}
    assert size == len(JPEG)
