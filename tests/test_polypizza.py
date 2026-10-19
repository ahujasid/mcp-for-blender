"""Regression coverage for the Poly Pizza integration.

The traps this file guards against were all found against the live service:
filter parameters are ignored unless they are Capitalized and numeric, the
response uses PascalCase field names (one with a space in it), and the CDN is
behind Cloudflare bot management and answers with an HTML challenge rather than
a GLB when it does not like the caller's IP.

Every request here is mocked; the suite never touches the network.
"""
import importlib.util
import sys
import types

from conftest import ROOT_ADDON as ADDON

API_KEY = "test-key-not-a-real-one"

CLOUDFLARE_CHALLENGE_BODY = (
    b"<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
    b"<body>Checking your browser before accessing static.poly.pizza</body></html>"
)

# A trimmed record in the shape the live API returns, PascalCase and all.
CHAIR = {
    "ID": "iMNqRzPwwe",
    "Title": "Chair",
    "Description": None,
    "Attribution": (
        '"Chair" by Quaternius, https://poly.pizza/m/iMNqRzPwwe. '
        "Licence at https://creativecommons.org/publicdomain/zero/1.0/"
    ),
    "Thumbnail": "https://static.poly.pizza/thumb.webp",
    "Download": "https://static.poly.pizza/model.glb",
    "Tri Count": 216,
    "Creator": {"Username": "Quaternius", "DPURL": "https://static.poly.pizza/dp.jpg"},
    "Uploaded": "2021-10-03T10:07:22.863Z",
    "Category": "Furniture & Decor",
    "Tags": ["Chair", "Furniture"],
    "Licence": "CC0 1.0",
    "Animated": False,
    "Orbit": {},
}


class FakeObject:
    """Just enough of a bpy object for the post-import block."""

    def __init__(self, name):
        self.name = name
        self.parent = None
        self.type = "EMPTY"
        self.children = ()
        self.custom_properties = {}

    def __setitem__(self, key, value):
        self.custom_properties[key] = value

    def __getitem__(self, key):
        return self.custom_properties[key]


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b"", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self._payload


def _load_addon(monkeypatch, scene, selected_objects=()):
    bpy = types.ModuleType("bpy")
    bpy.context = types.SimpleNamespace(
        scene=scene,
        selected_objects=list(selected_objects),
        view_layer=types.SimpleNamespace(update=lambda: None),
    )
    bpy.ops = types.SimpleNamespace(
        import_scene=types.SimpleNamespace(gltf=lambda **_kwargs: None)
    )
    bpy.types = types.SimpleNamespace(
        AddonPreferences=object,
        Operator=object,
        Panel=object,
        Scene=type("Scene", (), {}),
    )

    props = types.ModuleType("bpy.props")
    for name in ("BoolProperty", "EnumProperty", "FloatProperty", "IntProperty", "StringProperty"):
        setattr(props, name, lambda **_kwargs: None)
    bpy.props = props

    handlers = types.ModuleType("bpy.app.handlers")
    handlers.persistent = lambda fn: fn
    handlers.undo_post = []
    handlers.redo_post = []
    handlers.depsgraph_update_post = []

    app = types.ModuleType("bpy.app")
    app.version = (4, 2, 0)
    app.version_string = "4.2.0"
    app.background = False
    app.handlers = handlers
    app.timers = types.SimpleNamespace(
        is_registered=lambda *_a, **_k: False,
        register=lambda *_a, **_k: None,
        unregister=lambda *_a, **_k: None,
    )
    bpy.app = app

    monkeypatch.setitem(sys.modules, "bpy", bpy)
    monkeypatch.setitem(sys.modules, "bpy.props", props)
    monkeypatch.setitem(sys.modules, "bpy.app", app)
    monkeypatch.setitem(sys.modules, "bpy.app.handlers", handlers)
    monkeypatch.setitem(sys.modules, "mathutils", types.ModuleType("mathutils"))

    requests = types.ModuleType("requests")
    requests.utils = types.SimpleNamespace(default_headers=dict)
    requests.exceptions = types.SimpleNamespace(Timeout=TimeoutError)
    monkeypatch.setitem(sys.modules, "requests", requests)

    spec = importlib.util.spec_from_file_location("blender_mcp_polypizza_test", ADDON)
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    return addon


def _scene(polypizza_enabled=True):
    return types.SimpleNamespace(
        blendermcp_use_polyhaven=False,
        blendermcp_use_hyper3d=False,
        blendermcp_use_hunyuan3d=False,
        blendermcp_use_sketchfab=False,
        blendermcp_use_polypizza=polypizza_enabled,
    )


def _server(monkeypatch, selected_objects=(), polypizza_enabled=True):
    addon = _load_addon(monkeypatch, _scene(polypizza_enabled), selected_objects)
    server = addon.BlenderMCPServer()
    monkeypatch.setattr(server, "_get_polypizza_api_key", lambda: API_KEY)
    return addon, server


def _record_requests(monkeypatch, addon, responses):
    """Install a requests.get that hands back `responses` in order."""
    calls = []
    queue = list(responses)

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append({"url": url, "headers": dict(headers or {}), "params": dict(params or {})})
        return queue.pop(0)

    monkeypatch.setattr(addon.requests, "get", fake_get, raising=False)
    return calls


# --- filter building ---------------------------------------------------------

def test_filters_are_capitalized_and_numeric(monkeypatch):
    addon, _ = _server(monkeypatch)

    assert addon._polypizza_filter_params(category=7, licence=1, animated=True) == {
        "Category": 7,
        "License": 1,
        "Animated": 1,
    }
    assert addon._polypizza_filter_params(category=3) == {"Category": 3}
    # Ids that went through JSON as strings still count.
    assert addon._polypizza_filter_params(category="4") == {"Category": 4}
    assert addon._polypizza_filter_params(licence=0) == {"License": 0}


def test_animated_is_omitted_unless_animated_only_was_asked_for(monkeypatch):
    addon, _ = _server(monkeypatch)

    # Animated=0 is falsy server-side and does not filter, so sending it would
    # only be misleading noise.
    assert "Animated" not in addon._polypizza_filter_params(category=7, animated=False)
    assert addon._polypizza_filter_params(animated=False) == {}
    assert addon._polypizza_filter_params(animated=True) == {"Animated": 1}


def test_unknown_filter_values_are_rejected(monkeypatch):
    addon, _ = _server(monkeypatch)

    for bad in ("spaceships", 12, -1):
        try:
            addon._polypizza_filter_params(category=bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"category {bad!r} should have been rejected")


def test_search_sends_capitalized_numeric_filters_over_the_wire(monkeypatch):
    addon, server = _server(monkeypatch)
    calls = _record_requests(
        monkeypatch, addon, [FakeResponse(payload={"total": 1, "results": [CHAIR]})]
    )

    server.search_polypizza_models(query="chair", category=4, licence=1)

    assert calls[0]["url"].endswith("/search/chair")
    assert calls[0]["params"] == {"Category": 4, "License": 1, "Limit": 20}
    assert calls[0]["headers"]["x-auth-token"] == API_KEY


def test_limit_and_page_are_capitalized_and_limit_clamped(monkeypatch):
    """Lowercase limit/page are silently ignored by the API, which then serves
    its default page of 32. The spec caps Limit at 32 and Page is 0-indexed."""
    addon, server = _server(monkeypatch)
    empty = lambda: FakeResponse(payload={"total": 0, "results": []})
    calls = _record_requests(monkeypatch, addon, [empty(), empty(), empty()])

    server.search_polypizza_models(query="chair", limit=100, page=0)
    server.search_polypizza_models(query="chair", limit=-3)
    server.search_polypizza_models(query="chair")

    assert calls[0]["params"] == {"Limit": 32, "Page": 0}
    assert calls[1]["params"] == {"Limit": 1}
    assert calls[2]["params"] == {"Limit": 20}
    for call in calls:
        assert "limit" not in call["params"] and "page" not in call["params"]


# --- unfiltered search -------------------------------------------------------

def test_bare_search_without_filters_is_rejected_before_the_network(monkeypatch):
    addon, server = _server(monkeypatch)

    def request_should_not_run(*_args, **_kwargs):
        raise AssertionError("an unfiltered /search must not reach the network")

    monkeypatch.setattr(addon.requests, "get", request_should_not_run, raising=False)

    result = server.search_polypizza_models()

    assert "error" in result
    assert "keyword" in result["error"]


def test_filter_only_search_uses_the_bare_endpoint(monkeypatch):
    addon, server = _server(monkeypatch)
    calls = _record_requests(
        monkeypatch, addon, [FakeResponse(payload={"total": 296, "results": []})]
    )

    server.search_polypizza_models(animated=True, limit=5)

    assert calls[0]["url"].endswith("/v1.1/search")
    assert calls[0]["params"] == {"Animated": 1, "Limit": 5}


# --- response parsing --------------------------------------------------------

def test_parser_reads_tri_count_and_licence(monkeypatch):
    addon, server = _server(monkeypatch)
    _record_requests(monkeypatch, addon, [FakeResponse(payload={"total": 262, "results": [CHAIR]})])

    result = server.search_polypizza_models(query="chair")

    assert result["total"] == 262
    row = result["results"][0]
    # "Tri Count" has a space in the key and "Licence" is the British spelling.
    assert row["Tri Count"] == 216
    assert row["Licence"] == "CC0 1.0"
    assert row["ID"] == "iMNqRzPwwe"
    assert row["Creator"] == "Quaternius"
    assert row["Animated"] is False
    assert row["Category"] == "Furniture & Decor"


def test_parser_survives_missing_optional_fields(monkeypatch):
    addon, server = _server(monkeypatch)
    sparse = {"ID": "abc", "Title": "Thing"}
    _record_requests(monkeypatch, addon, [FakeResponse(payload={"total": 1, "results": [sparse]})])

    row = server.search_polypizza_models(query="thing")["results"][0]

    assert row["Tri Count"] is None
    assert row["Licence"] is None
    assert row["Creator"] is None
    assert row["Tags"] == []


def test_zero_tri_count_is_reported_as_unknown_not_zero():
    """The API reports "Tri Count": 0 for models that plainly have geometry.

    Printing a bare 0 would invite picking it as the lowest-poly option, so the
    search formatter shows it as Unknown instead.
    """
    import asyncio

    from blender_mcp import server

    sent = {}

    class FakeBlender:
        def send_command(self, _command, params=None):
            sent["params"] = params
            return {
                "total": 2,
                "results": [
                    {"ID": "a", "Title": "Counted", "Tri Count": 216, "Licence": "CC0 1.0"},
                    {"ID": "b", "Title": "Uncounted", "Tri Count": 0, "Licence": "CC0 1.0"},
                ],
            }

    original = server.get_blender_connection
    server.get_blender_connection = lambda: FakeBlender()
    try:
        out = asyncio.run(server._search_polypizza(None, query="thing", user_prompt=""))
    finally:
        server.get_blender_connection = original

    assert "Tri count: 216" in out
    assert "Tri count: Unknown" in out
    assert "Tri count: 0" not in out


def test_tool_boundary_converts_names_to_numeric_ids():
    """The MCP server is the single source of truth for name-to-id conversion.

    It ships with the pip package and updates without an addon reinstall, so
    the mapping lives there; the addon only ever sees numeric ids.
    """
    import asyncio

    from blender_mcp import server

    sent = {}

    class FakeBlender:
        def send_command(self, _command, params=None):
            sent.update(params or {})
            return {"total": 0, "results": []}

    original = server.get_blender_connection
    server.get_blender_connection = lambda: FakeBlender()
    try:
        asyncio.run(
            server._search_polypizza(
                None, query="wolf", category="Animals", licence="CC0", user_prompt=""
            )
        )
    finally:
        server.get_blender_connection = original

    assert sent["category"] == 7
    assert sent["licence"] == 1


def test_server_resolves_names_aliases_and_ids():
    """Any spelling a caller plausibly uses resolves to the API's numeric id."""
    from blender_mcp import server

    assert server._polypizza_category_id("Animals") == 7
    assert server._polypizza_category_id("furniture & decor") == 4
    assert server._polypizza_category_id("buildings/architecture") == 8
    assert server._polypizza_category_id("person") == 9
    assert server._polypizza_category_id("plants") == 6
    assert server._polypizza_category_id("3") == 3
    assert server._polypizza_category_id(11) == 11
    assert server._polypizza_category_id(None) is None

    assert server._polypizza_licence_id("CC-BY 3.0") == 0
    assert server._polypizza_licence_id("cc0") == 1
    assert server._polypizza_licence_id("Public Domain") == 1
    assert server._polypizza_licence_id(0) == 0
    assert server._polypizza_licence_id("") is None


def test_server_rejects_unknown_filter_values():
    from blender_mcp import server

    for bad in ("spaceships", 12, -1, True):
        try:
            server._polypizza_category_id(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"category {bad!r} should have been rejected")

    for bad in ("GPL", 2, True):
        try:
            server._polypizza_licence_id(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"licence {bad!r} should have been rejected")


# --- the CDN -----------------------------------------------------------------

def test_cloudflare_challenge_gets_its_own_error(monkeypatch):
    addon, server = _server(monkeypatch)
    _record_requests(
        monkeypatch,
        addon,
        [
            FakeResponse(payload=CHAIR),
            FakeResponse(
                status_code=403,
                content=CLOUDFLARE_CHALLENGE_BODY,
                headers={"cf-mitigated": "challenge", "Content-Type": "text/html; charset=UTF-8"},
            ),
        ],
    )

    result = server.download_polypizza_model("iMNqRzPwwe")

    assert "error" in result
    error = result["error"]
    assert "Cloudflare" in error
    # The failure must not read as a bad key or a missing model.
    assert "not an API key problem" in error
    assert "404" not in error
    assert "401" not in error


def test_non_glb_body_without_cloudflare_headers_is_still_flagged(monkeypatch):
    addon, server = _server(monkeypatch)
    _record_requests(
        monkeypatch,
        addon,
        [FakeResponse(payload=CHAIR), FakeResponse(status_code=200, content=b"not a glb at all")],
    )

    result = server.download_polypizza_model("iMNqRzPwwe")

    assert "glTF magic bytes" in result["error"]


def test_api_key_is_never_sent_to_the_cdn(monkeypatch):
    root = FakeObject("Chair")
    addon, server = _server(monkeypatch, selected_objects=[root])
    calls = _record_requests(
        monkeypatch,
        addon,
        [
            FakeResponse(payload=CHAIR),
            FakeResponse(status_code=200, content=b"glTF" + b"\x00" * 64),
        ],
    )

    result = server.download_polypizza_model("iMNqRzPwwe")

    assert result["success"] is True

    api_call, cdn_call = calls
    assert api_call["url"].startswith("https://api.poly.pizza/")
    assert api_call["headers"]["x-auth-token"] == API_KEY

    assert cdn_call["url"].startswith("https://static.poly.pizza/")
    assert "x-auth-token" not in {key.lower() for key in cdn_call["headers"]}
    assert API_KEY not in repr(cdn_call)


# --- attribution -------------------------------------------------------------

def test_attribution_is_written_onto_the_imported_object(monkeypatch):
    root = FakeObject("Chair")
    addon, server = _server(monkeypatch, selected_objects=[root])
    _record_requests(
        monkeypatch,
        addon,
        [
            FakeResponse(payload=CHAIR),
            FakeResponse(status_code=200, content=b"glTF" + b"\x00" * 64),
        ],
    )

    result = server.download_polypizza_model("iMNqRzPwwe")

    assert root["polypizza_attribution"] == CHAIR["Attribution"]
    assert root["polypizza_id"] == "iMNqRzPwwe"
    assert root["polypizza_licence"] == "CC0 1.0"
    assert result["attribution"] == CHAIR["Attribution"]
    assert result["licence"] == "CC0 1.0"


# --- wiring ------------------------------------------------------------------

def test_disabled_polypizza_hides_the_commands_but_keeps_status(monkeypatch):
    addon, server = _server(monkeypatch, polypizza_enabled=False)

    def request_should_not_run(*_args, **_kwargs):
        raise AssertionError("must not call out for a disabled integration")

    monkeypatch.setattr(addon.requests, "get", request_should_not_run, raising=False)

    status = server.get_polypizza_status()
    search = server._execute_command_internal({"type": "search_polypizza_models"})
    status_command = server._execute_command_internal({"type": "get_polypizza_status"})

    assert status["enabled"] is False
    assert "currently disabled" in status["message"]
    assert search == {"status": "error", "message": "Unknown command type: search_polypizza_models"}
    assert status_command["status"] == "success"


def test_enabled_polypizza_with_a_key_reports_ready(monkeypatch):
    _, server = _server(monkeypatch)

    assert server.get_polypizza_status() == {
        "enabled": True,
        "message": "Poly Pizza integration is enabled and ready to use.",
    }


def test_enabled_polypizza_without_a_key_is_not_ready(monkeypatch):
    addon, server = _server(monkeypatch)
    monkeypatch.setattr(server, "_get_polypizza_api_key", lambda: "")

    status = server.get_polypizza_status()

    assert status["enabled"] is False
    assert "API key is not given" in status["message"]
    assert server.search_polypizza_models(query="chair") == {
        "error": "Poly Pizza API key is not configured"
    }
