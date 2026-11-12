"""MCP Apps and OpenAI extensions: asset pickers, @-mentions, the Viewport app.

The contract that matters most is that clients advertising none of these
capabilities see exactly what they saw before, so most tests run both ways.
"""
import asyncio
import json
import time
import types

import anyio
import pytest
from mcp.shared.message import SessionMessage
from mcp.types import ClientCapabilities, JSONRPCMessage, JSONRPCNotification, JSONRPCRequest, JSONRPCResponse

from blender_mcp import openai_apps, server
from blender_mcp.openai_apps import PickerOption, pick_asset, picker_schema

OPENAI_FORMS = {"extensions": {"openai/elicitation": {"form": {}}}}
MCP_APPS = {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}}


def _session(capabilities=None, reply=None):
    """A stand-in ServerSession: advertises `capabilities` and answers requests with `reply`."""
    session = types.SimpleNamespace(
        client_params=types.SimpleNamespace(capabilities=ClientCapabilities.model_validate(capabilities or {})),
        sent=[],
    )

    async def send_request(request, result_type, **_kwargs):
        session.sent.append(request.model_dump(by_alias=True, mode="json", exclude_none=True))
        if isinstance(reply, Exception):
            raise reply
        return result_type.model_validate(reply)

    session.send_request = send_request
    return session


def _ctx(capabilities=None, reply=None):
    return types.SimpleNamespace(session=_session(capabilities, reply), request_id=7)


# ---------------------------------------------------------------- capabilities

def test_openai_forms_are_detected_from_the_extensions_capability():
    assert openai_apps.supports_openai_forms(_session(OPENAI_FORMS))
    assert not openai_apps.supports_openai_forms(_session({}))
    assert not openai_apps.supports_openai_forms(_session({"elicitation": {"form": {}}}))
    assert not openai_apps.supports_openai_forms(None)


def test_apps_support_is_detected_and_can_be_forced(monkeypatch):
    monkeypatch.delenv(openai_apps.APPS_ENV, raising=False)
    assert openai_apps.supports_apps(_session(MCP_APPS))
    assert openai_apps.supports_apps(_session(OPENAI_FORMS))
    assert not openai_apps.supports_apps(_session({}))

    monkeypatch.setenv(openai_apps.APPS_ENV, "1")
    assert openai_apps.supports_apps(_session({}))
    monkeypatch.setenv(openai_apps.APPS_ENV, "0")
    assert not openai_apps.supports_apps(_session(MCP_APPS))


# ---------------------------------------------------------------- picker

OPTIONS = [
    PickerOption("chair", "Chair", "CC0", "https://example.com/chair.png"),
    PickerOption("sofa", "Sofa"),
]


def test_picker_schema_uses_titled_options_with_thumbnails():
    field = picker_schema("Model", OPTIONS)["properties"]["asset"]
    assert field["oneOf"][0] == {
        "const": "chair",
        "title": "Chair",
        "description": "CC0",
        "x-openai-thumbnail": {"src": "https://example.com/chair.png"},
    }
    assert field["oneOf"][1] == {"const": "sofa", "title": "Sofa"}


def test_no_picker_without_openai_forms():
    ctx = _ctx({}, {"action": "accept", "content": {"asset": "chair"}})
    assert asyncio.run(pick_asset(ctx, "Pick", "Model", OPTIONS)) is None
    assert ctx.session.sent == []


def test_picker_sends_openai_elicitation_and_returns_the_choice():
    ctx = _ctx(OPENAI_FORMS, {"action": "accept", "content": {"asset": "sofa"}})
    picked = asyncio.run(pick_asset(ctx, "Pick a chair", "Model", OPTIONS))
    assert (picked.action, picked.asset_id) == ("accept", "sofa")
    [request] = ctx.session.sent
    assert request["method"] == "openai/elicitation/create"
    assert request["params"]["mode"] == "form"
    assert request["params"]["message"] == "Pick a chair"


@pytest.mark.parametrize("action", ["decline", "cancel"])
def test_closing_the_picker_is_reported(action):
    picked = asyncio.run(pick_asset(_ctx(OPENAI_FORMS, {"action": action}), "Pick", "Model", OPTIONS))
    assert (picked.action, picked.asset_id) == (action, None)


@pytest.mark.parametrize("reply", [
    {"action": "accept", "content": {"asset": "not-offered"}},
    RuntimeError("client went away"),
])
def test_a_bad_or_failed_picker_falls_back_to_the_listing(reply):
    assert asyncio.run(pick_asset(_ctx(OPENAI_FORMS, reply), "Pick", "Model", OPTIONS)) is None


POLYPIZZA_RESULTS = {
    "total": 2,
    "results": [
        {"ID": "a", "Title": "Oak chair", "Creator": "Quaternius", "Licence": "CC0 1.0",
         "Tri Count": 216, "Thumbnail": "https://static.poly.pizza/a.webp"},
        {"ID": "b", "Title": "Pine chair", "Creator": "Someone", "Licence": "CC-BY 3.0",
         "Tri Count": 400, "Thumbnail": "https://static.poly.pizza/b.webp"},
    ],
}


def _search_polypizza(monkeypatch, ctx):
    monkeypatch.setattr(server, "get_blender_connection",
                        lambda: types.SimpleNamespace(send_command=lambda *_a, **_k: POLYPIZZA_RESULTS))
    return asyncio.run(server._search_polypizza(ctx, query="chair", user_prompt=""))


def test_search_returns_only_the_picked_asset(monkeypatch):
    ctx = _ctx(OPENAI_FORMS, {"action": "accept", "content": {"asset": "b"}})
    out = _search_polypizza(monkeypatch, ctx)
    assert "picked this Poly Pizza result" in out
    assert "Pine chair (ID: b)" in out
    assert "Oak chair" not in out
    assert "CC-BY models must be credited" in out
    options = ctx.session.sent[0]["params"]["requestedSchema"]["properties"]["asset"]["oneOf"]
    assert options[0]["x-openai-thumbnail"] == {"src": "https://static.poly.pizza/a.webp"}
    assert options[0]["description"] == "Quaternius · CC0 1.0 · 216 tris"


def test_search_after_a_dismissed_picker_lists_everything_and_says_so(monkeypatch):
    out = _search_polypizza(monkeypatch, _ctx(OPENAI_FORMS, {"action": "cancel"}))
    assert "closed it without choosing" in out
    assert "Oak chair" in out and "Pine chair" in out


def test_search_without_openai_forms_is_unchanged(monkeypatch):
    out = _search_polypizza(monkeypatch, _ctx({}))
    assert out.startswith("Found 2 models (of 2 total) matching 'chair'")
    assert "picker" not in out


def test_polyhaven_thumbnail_prefers_the_addon_url():
    assert server._polyhaven_thumbnail({"id": "x", "thumbnail_url": "https://cdn/x?v=1"}) == "https://cdn/x?v=1"
    assert server._polyhaven_thumbnail({"id": "rock_01"}).startswith(
        "https://cdn.polyhaven.com/asset_img/thumbs/rock_01.png"
    )


def test_sketchfab_thumbnail_is_the_smallest_one_big_enough():
    model = {"thumbnails": {"images": [
        {"url": "https://s/100", "width": 100},
        {"url": "https://s/1024", "width": 1024},
        {"url": "https://s/256", "width": 256},
        {"url": "http://s/insecure", "width": 300},
    ]}}
    assert server._sketchfab_thumbnail(model) == "https://s/256"
    assert server._sketchfab_thumbnail({}) is None


# ---------------------------------------------------------------- mentions

def _fake_blender(monkeypatch, protocol, replies):
    calls = []

    def send_command(command, params=None):
        calls.append((command, params))
        return replies[command]

    monkeypatch.setattr(server, "get_blender_connection", lambda: types.SimpleNamespace(send_command=send_command))
    monkeypatch.setattr(server, "_addon_protocol", lambda: protocol)
    return calls


def test_mentions_are_resource_links_to_scene_items(monkeypatch):
    _fake_blender(monkeypatch, 12, {"list_scene_items": {"items": [
        {"kind": "object", "name": "Desk Lamp", "detail": "Mesh object"},
        {"kind": "material", "name": "Brass", "detail": "Material on Desk Lamp"},
    ]}})
    result = asyncio.run(server.search_mentions("desk"))
    assert result.content == []
    assert result.structuredContent["items"] == [
        {"type": "resource_link", "uri": "blender://object/Desk%20Lamp", "name": "Desk Lamp",
         "title": "Desk Lamp", "description": "Mesh object", "mimeType": "application/json"},
        {"type": "resource_link", "uri": "blender://material/Brass", "name": "Brass",
         "title": "Brass", "description": "Material on Desk Lamp", "mimeType": "application/json"},
    ]


def test_mentions_fall_back_to_scene_info_on_older_addons(monkeypatch):
    calls = _fake_blender(monkeypatch, 11, {"get_scene_info": {"objects": [
        {"name": "Cube", "type": "MESH"}, {"name": "Camera", "type": "CAMERA"},
    ]}})
    result = asyncio.run(server.search_mentions("cu"))
    assert [i["name"] for i in result.structuredContent["items"]] == ["Cube"]
    assert calls == [("get_scene_info", None)]


def test_mentions_are_empty_when_blender_is_not_running(monkeypatch):
    def refuse():
        raise Exception("Could not connect to Blender")
    monkeypatch.setattr(server, "get_blender_connection", refuse)
    assert asyncio.run(server.search_mentions("x")).structuredContent == {"items": []}


def test_mentioned_names_round_trip_through_the_uri(monkeypatch):
    _fake_blender(monkeypatch, 12, {"list_scene_items": {"items": [
        {"kind": "material", "name": "Wood/Oak 50%", "detail": "Material on Table"},
    ]}})
    uri = server._scene_item_uri("material", "Wood/Oak 50%")
    name = uri.removeprefix("blender://material/")
    assert "/" not in name
    assert json.loads(server.material_resource(name))["name"] == "Wood/Oak 50%"


def test_addon_lists_and_ranks_scene_items(monkeypatch):
    from test_polypizza import _load_addon, _scene

    def obj(name, kind, collection, *materials):
        return types.SimpleNamespace(
            name=name, type=kind,
            users_collection=[types.SimpleNamespace(name=collection)],
            material_slots=[types.SimpleNamespace(material=m) for m in materials],
        )

    brass = types.SimpleNamespace(name="Brass")
    lampshade = types.SimpleNamespace(name="Lampshade")
    scene = _scene()
    scene.objects = [obj("Desk Lamp", "MESH", "Props", brass, lampshade), obj("Floor Lamp", "MESH", "Props", brass)]
    addon = _load_addon(monkeypatch, scene)
    addon.bpy.data = types.SimpleNamespace(
        materials=[brass, lampshade, types.SimpleNamespace(name="Unused")],
        collections=[types.SimpleNamespace(name="Props", all_objects=scene.objects)],
    )
    list_items = addon.BlenderMCPServer().list_scene_items

    names = [i["name"] for i in list_items("lamp")["items"]]
    assert names == ["Lampshade", "Desk Lamp", "Floor Lamp"]  # prefix match ranks first

    by_name = {i["name"]: i for i in list_items("")["items"]}
    assert by_name["Desk Lamp"] == {"kind": "object", "name": "Desk Lamp", "detail": "Mesh object in collection 'Props'"}
    assert by_name["Brass"]["detail"] == "Material on Desk Lamp, Floor Lamp"
    assert by_name["Unused"]["detail"] == "Material not used by any object in this scene"
    assert by_name["Props"]["detail"] == "Collection with 2 objects"
    assert len(list_items("", limit=2)["items"]) == 2


# ---------------------------------------------------------------- viewport app

def test_viewport_result_only_sends_the_image_when_it_changed(monkeypatch):
    store = openai_apps.ViewportStore()
    monkeypatch.setattr(server, "viewport_store", store)

    empty = server.viewport_latest(since=0)
    assert empty.content == [] and empty.structuredContent["seq"] == 0

    store.put(b"\x89PNG", "model")
    first = server.viewport_latest(since=0)
    assert first.content[0].type == "image"
    assert first.structuredContent["seq"] == 1
    assert first.structuredContent["source"] == "model"
    assert server.viewport_latest(since=1).content == []


VIEW = {"view_matrix": [[1, 0, 0, 0]] * 4, "window_matrix": [[1, 0, 0, 0]] * 4, "width": 800, "height": 600}


def _fresh_store(monkeypatch, view=None, **origin):
    """A clean store whose captures come back with `view` and `origin` (file, scene)."""
    store = openai_apps.ViewportStore()
    monkeypatch.setattr(server, "viewport_store", store)
    info = {**origin, **({"view": view} if view else {})}
    monkeypatch.setattr(server, "_capture_viewport", lambda _size: (b"\x89PNG", info))
    return store


def test_the_viewport_app_is_fullscreen_only():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert tools["open_viewport"].meta["ui"]["resourceUri"] == openai_apps.VIEWPORT_URI
    assert tools["look"].meta["ui"]["resourceUri"] == openai_apps.VIEWPORT_URI
    resources = {str(r.uri): r for r in asyncio.run(server.mcp.list_resources())}
    assert resources[openai_apps.VIEWPORT_URI].meta["openai/ui"] == {
        "preferredDisplayMode": "fullscreen", "availableDisplayModes": ["fullscreen"],
    }


def test_viewport_state_says_when_the_addon_is_too_old_to_pick(monkeypatch):
    _fresh_store(monkeypatch)
    monkeypatch.setattr(server, "_addon_handshake", None)
    assert server.viewport_capture().structuredContent["addon_outdated"] is False
    monkeypatch.setattr(server, "_addon_handshake", types.SimpleNamespace(up_to_date=False))
    assert server.viewport_capture().structuredContent["addon_outdated"] is True


def test_model_screenshots_reach_the_viewport_app(monkeypatch):
    store = _fresh_store(monkeypatch)
    server._viewport_screenshot(None, max_size=500)
    state, png = store.snapshot()
    assert (state["seq"], state["source"], png) == (1, "model", b"\x89PNG")


def test_model_screenshot_state_is_for_the_app_only(monkeypatch):
    _fresh_store(monkeypatch, view=VIEW)
    result = server._viewport_screenshot(None, max_size=500)
    assert [c.type for c in result.content] == ["image"]
    assert result.structuredContent is None  # would reach the model
    wire = result.model_dump(by_alias=True, exclude_none=True)
    assert wire["_meta"][openai_apps.VIEWPORT_STATE_META]["seq"] == 1
    assert wire["_meta"][openai_apps.VIEWPORT_STATE_META]["pickable"] is True


def test_scene_changing_commands_make_the_image_stale_until_blender_is_quiet(monkeypatch):
    store = _fresh_store(monkeypatch)
    server.viewport_capture()
    assert store.snapshot()[0]["stale"] is False

    store.command_started()
    store.command_finished("get_scene_info")
    assert store.snapshot()[0]["stale"] is False  # reads don't count

    store.command_started()
    state = store.snapshot()[0]
    assert state["blender_quiet"] is False  # a command is running
    store.command_finished("execute_code")
    state = store.snapshot()[0]
    assert state["stale"] is True and state["blender_quiet"] is False  # just finished

    later = time.time() + openai_apps.AUTO_CAPTURE_QUIET_S + 1
    monkeypatch.setattr(openai_apps.time, "time", lambda: later)
    assert store.snapshot()[0]["blender_quiet"] is True

    result = server.viewport_capture(auto=True)
    assert result.structuredContent["source"] == "auto"
    assert result.structuredContent["stale"] is False


def test_an_edit_during_a_capture_leaves_the_image_stale(monkeypatch):
    store = openai_apps.ViewportStore()
    monkeypatch.setattr(server, "viewport_store", store)

    def capture_while_editing(_size):
        store.command_started()
        store.command_finished("execute_code")
        return b"\x89PNG", {}

    monkeypatch.setattr(server, "_capture_viewport", capture_while_editing)
    assert server.viewport_capture(auto=True).structuredContent["stale"] is True


def test_every_blender_command_is_counted_even_when_it_fails(monkeypatch):
    store = openai_apps.ViewportStore()
    monkeypatch.setattr(server, "viewport_store", store)
    blender = server.BlenderConnection(host="localhost", port=1)

    def fail(*_args):
        assert store.snapshot()[0]["blender_quiet"] is False
        raise Exception("boom")

    monkeypatch.setattr(blender, "_send_command_locked", fail)
    with pytest.raises(Exception):
        blender.send_command("execute_code", {"code": "x"})
    assert store.scene_version == 1


def test_clicking_the_viewport_returns_a_link_to_the_object(monkeypatch):
    _fresh_store(monkeypatch, view=VIEW)
    server.viewport_capture()
    sent = []

    def send_command(name, params):
        sent.append((name, params))
        return {"object": {"name": "Desk Lamp", "type": "MESH", "detail": "Mesh object"}}

    monkeypatch.setattr(server, "get_blender_connection", lambda: types.SimpleNamespace(send_command=send_command))
    result = server.viewport_pick(seq=1, x=0.25, y=0.5)
    assert sent == [("pick_viewport_object", {**VIEW, "x": 0.25, "y": 0.5})]
    link = result.structuredContent["object"]["link"]
    assert link["type"] == "resource_link" and link["uri"] == "blender://object/Desk%20Lamp"


def test_clicking_empty_space_or_an_unpickable_capture(monkeypatch):
    _fresh_store(monkeypatch)  # an addon that reports no camera
    server.viewport_capture()
    assert server.viewport_pick(seq=1, x=0.5, y=0.5).isError

    _fresh_store(monkeypatch, view=VIEW)
    server.viewport_capture()
    monkeypatch.setattr(server, "get_blender_connection",
                        lambda: types.SimpleNamespace(send_command=lambda *_a: {"object": None}))
    assert server.viewport_pick(seq=1, x=0.5, y=0.5).structuredContent == {"object": None}


def test_captures_say_which_file_and_scene_they_show(monkeypatch):
    _fresh_store(monkeypatch, file="/work/robot.blend", scene="Scene", scene_count=2)
    state = server.viewport_capture().structuredContent
    assert (state["file"], state["scene"], state["scene_count"]) == ("/work/robot.blend", "Scene", 2)

    _fresh_store(monkeypatch)  # older addons report neither
    state = server.viewport_capture().structuredContent
    assert state["file"] is None and state["scene"] is None


@pytest.mark.parametrize("hit, expected", [
    ({"object": None, "mismatch": "file", "current": "/work/other.blend"}, "robot.blend, which isn't open"),
    ({"object": None, "mismatch": "scene", "current": "Scene.001"}, "'Scene', but Blender is showing 'Scene.001'"),
])
def test_clicks_on_a_capture_of_another_file_or_scene_are_refused(monkeypatch, hit, expected):
    view = {**VIEW, "file": "/work/robot.blend", "scene": "Scene"}
    _fresh_store(monkeypatch, view=view)
    server.viewport_capture()
    monkeypatch.setattr(server, "get_blender_connection",
                        lambda: types.SimpleNamespace(send_command=lambda *_a: hit))
    result = server.viewport_pick(seq=1, x=0.5, y=0.5)
    assert result.isError and expected in result.content[0].text


def test_viewport_capture_reports_blender_errors_to_the_app(monkeypatch):
    def fail(_size):
        raise Exception("No 3D viewport found")
    monkeypatch.setattr(server, "_capture_viewport", fail)
    result = server.viewport_capture()
    assert result.isError
    assert "No 3D viewport found" in result.content[0].text


def test_viewport_html_ships_with_the_package():
    html = openai_apps.viewport_html()
    assert "ui/initialize" in html and "viewport_latest" in html and "viewport_capture" in html
    assert "viewport_pick" in html and openai_apps.VIEWPORT_STATE_META in html


# ---------------------------------------------------------------- over the wire

async def _exchange(monkeypatch, capabilities, steps):
    """Run the real server over in-memory streams: initialize with `capabilities`,
    then feed each (message, on_reply) step, returning every message the server sent."""
    monkeypatch.setattr(server, "record_startup", lambda: None)
    monkeypatch.setattr(server, "check_addon_status_on_startup",
                        lambda: types.SimpleNamespace(needs_action=False, message=None))
    monkeypatch.setattr(server, "get_blender_connection",
                        lambda: types.SimpleNamespace(send_command=lambda *_a, **_k: POLYPIZZA_RESULTS))
    monkeypatch.setenv("DISABLE_TELEMETRY", "1")

    to_server_send, to_server_recv = anyio.create_memory_object_stream(16)
    from_server_send, from_server_recv = anyio.create_memory_object_stream(16)
    lowlevel = server.mcp._mcp_server
    received = []

    async def send(message):
        await to_server_send.send(SessionMessage(JSONRPCMessage(message)))

    async def reply_to(request_id):
        while True:
            message = (await from_server_recv.receive()).message.root
            received.append(message)
            if getattr(message, "id", None) == request_id and isinstance(message, JSONRPCResponse):
                return message

    async with anyio.create_task_group() as tg:
        tg.start_soon(lowlevel.run, to_server_recv, from_server_send, lowlevel.create_initialization_options())
        await send(JSONRPCRequest(jsonrpc="2.0", id=1, method="initialize", params={
            "protocolVersion": "2025-11-25",
            "capabilities": capabilities,
            "clientInfo": {"name": "test", "version": "1"},
        }))
        await reply_to(1)
        await send(JSONRPCNotification(jsonrpc="2.0", method="notifications/initialized"))
        for step in steps:
            await step(send, from_server_recv, received, reply_to)
        tg.cancel_scope.cancel()
    return received


def _list_tools(monkeypatch, capabilities):
    names = []

    async def step(send, _recv, _received, reply_to):
        await send(JSONRPCRequest(jsonrpc="2.0", id=2, method="tools/list"))
        names.extend(t["name"] for t in (await reply_to(2)).result["tools"])

    asyncio.run(_exchange(monkeypatch, capabilities, [step]))
    return names


APP_TOOLS = {"search_mentions", "open_viewport", "viewport_latest", "viewport_capture"}


def test_app_only_tools_are_hidden_from_clients_without_mcp_apps(monkeypatch):
    monkeypatch.delenv(openai_apps.APPS_ENV, raising=False)
    names = _list_tools(monkeypatch, {})
    assert "execute_blender_code" in names
    assert not APP_TOOLS & set(names)


def test_app_only_tools_are_listed_for_mcp_apps_clients(monkeypatch):
    monkeypatch.delenv(openai_apps.APPS_ENV, raising=False)
    assert APP_TOOLS <= set(_list_tools(monkeypatch, MCP_APPS))


def test_picker_round_trip_over_the_wire(monkeypatch):
    """The elicitation goes out as openai/elicitation/create, and the reply to it
    decides the tool result."""
    result = {}

    async def step(send, recv, received, reply_to):
        await send(JSONRPCRequest(jsonrpc="2.0", id=2, method="tools/call", params={
            "name": "search_assets", "arguments": {"source": "polypizza", "query": "chair"},
        }))
        elicit = (await recv.receive()).message.root
        received.append(elicit)
        assert elicit.method == "openai/elicitation/create"
        assert [o["const"] for o in elicit.params["requestedSchema"]["properties"]["asset"]["oneOf"]] == ["a", "b"]
        await send(JSONRPCResponse(jsonrpc="2.0", id=elicit.id, result={"action": "accept", "content": {"asset": "a"}}))
        result["text"] = (await reply_to(2)).result["content"][0]["text"]

    asyncio.run(_exchange(monkeypatch, OPENAI_FORMS, [step]))
    assert "Oak chair (ID: a)" in result["text"]
    assert "Pine chair" not in result["text"]


# ---------------------------------------------------------------- Codex quirks

def test_forms_can_be_forced_on_for_clients_that_dont_advertise_them(monkeypatch):
    """Codex's MCP client advertises no extensions; its plugin sets the env var."""
    monkeypatch.setenv(openai_apps.OPENAI_FORMS_ENV, "1")
    assert openai_apps.supports_openai_forms(_session({}))
    monkeypatch.setenv(openai_apps.OPENAI_FORMS_ENV, "0")
    assert not openai_apps.supports_openai_forms(_session(OPENAI_FORMS))


def test_a_refused_picker_is_not_offered_again_in_that_session(monkeypatch):
    monkeypatch.setenv(openai_apps.OPENAI_FORMS_ENV, "1")
    ctx = _ctx({}, RuntimeError("Method not found"))
    assert asyncio.run(pick_asset(ctx, "Pick", "Model", OPTIONS)) is None
    assert asyncio.run(pick_asset(ctx, "Pick", "Model", OPTIONS)) is None
    assert len(ctx.session.sent) == 1
    assert openai_apps.supports_openai_forms(_session({}))  # other sessions still try


def test_picker_schema_has_only_the_top_level_keys_codex_accepts():
    assert set(picker_schema("Model", OPTIONS)) <= {"$schema", "type", "properties", "required"}


def test_consent_prompt_schema_has_only_the_top_level_keys_codex_accepts():
    from blender_mcp.consent_prompt import CONSENT_SCHEMA

    assert set(CONSENT_SCHEMA) <= {"$schema", "type", "properties", "required"}
    assert CONSENT_SCHEMA["properties"]["consent"]["type"] == "boolean"


@pytest.mark.parametrize("content, granted", [({"consent": True}, True), ({"consent": False}, False), ({}, False)])
def test_consent_prompt_only_counts_an_explicit_yes(monkeypatch, content, granted):
    from blender_mcp import consent_prompt

    consent_prompt.reset_for_tests()
    written, applied = {}, []
    monkeypatch.setattr(consent_prompt, "_already_answered", lambda: False)
    monkeypatch.setattr(consent_prompt, "_current_consent", lambda: False)
    monkeypatch.setattr(consent_prompt, "_client_supports_elicitation", lambda ctx: True)
    monkeypatch.setattr(consent_prompt, "_write_state", lambda **f: written.update(f))
    monkeypatch.setattr(consent_prompt, "_apply_consent", lambda c: applied.append(c) or True)

    sent = {}

    async def elicit_form(message, requestedSchema, related_request_id=None):
        sent["schema"] = requestedSchema
        from mcp.types import ElicitResult
        return ElicitResult(action="accept", content=content)

    session = types.SimpleNamespace(elicit_form=elicit_form)
    ctx = types.SimpleNamespace(request_context=types.SimpleNamespace(session=session), request_id=3)
    asyncio.run(consent_prompt.maybe_prompt_for_consent(ctx))
    assert sent["schema"] is consent_prompt.CONSENT_SCHEMA
    assert written["consent"] is granted
    assert applied == ([True] if granted else [])
