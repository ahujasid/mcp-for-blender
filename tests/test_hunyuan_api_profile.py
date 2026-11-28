"""Hunyuan3D OFFICIAL_API: the Tencent Cloud service must follow the account type.

Mainland accounts use the AI3D 3.0 API; Tencent Cloud International accounts use the
"Hunyuan-to-3D (Professional)" service, which lives on the older ``hunyuan`` API in
``ap-singapore`` and takes ``EnablePBR`` instead of the mainland body fields. Sending
International credentials to the mainland endpoint fails with AuthFailure.SignatureFailure.
"""
import importlib.util
import json
import sys
import types

from conftest import ROOT_ADDON as ADDON


def _load_addon(monkeypatch, scene):
    bpy = types.ModuleType("bpy")
    bpy.context = types.SimpleNamespace(scene=scene)
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

    spec = importlib.util.spec_from_file_location("blender_mcp_addon_test", ADDON)
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    return addon


def _scene(international_pro):
    return types.SimpleNamespace(
        blendermcp_use_polyhaven=False,
        blendermcp_use_hyper3d=False,
        blendermcp_use_sketchfab=False,
        blendermcp_use_polypizza=False,
        blendermcp_use_hunyuan3d=True,
        blendermcp_hunyuan3d_mode="OFFICIAL_API",
        blendermcp_hunyuan3d_intl_pro=international_pro,
    )


class _Response:
    status_code = 200

    @staticmethod
    def json():
        return {"Response": {"JobId": "job-1"}}


def _capture_post(monkeypatch, addon):
    calls = []

    def fake_post(url, headers=None, data=None, **_kwargs):
        calls.append({"url": url, "headers": headers or {}, "body": json.loads(data or "{}")})
        return _Response()

    monkeypatch.setattr(addon.requests, "post", fake_post, raising=False)
    return calls


def _server_with_credentials(monkeypatch, addon):
    server = addon.BlenderMCPServer()
    monkeypatch.setattr(server, "_get_hunyuan3d_secret_id", lambda: "AKIDtest")
    monkeypatch.setattr(server, "_get_hunyuan3d_secret_key", lambda: "secret")
    return server


def test_profile_defaults_to_the_mainland_ai3d_service(monkeypatch):
    addon = _load_addon(monkeypatch, _scene(False))
    profile = addon.hunyuan_api_profile(False)
    assert profile["service"] == "ai3d"
    assert profile["version"] == "2025-05-13"
    assert profile["region"] == "ap-guangzhou"
    assert profile["submit_action"] == "SubmitHunyuanTo3DProJob"
    assert profile["query_action"] == "QueryHunyuanTo3DProJob"
    assert profile["submit_body"] == {}


def test_profile_switches_to_the_international_pro_service(monkeypatch):
    addon = _load_addon(monkeypatch, _scene(True))
    profile = addon.hunyuan_api_profile(True)
    assert profile["service"] == "hunyuan"
    assert profile["version"] == "2023-09-01"
    assert profile["region"] == "ap-singapore"
    assert profile["submit_action"] == "SubmitHunyuanTo3DProJob"
    assert profile["query_action"] == "QueryHunyuanTo3DProJob"
    assert profile["submit_body"] == {"EnablePBR": True}


def test_profile_returns_a_copy_so_callers_cannot_mutate_the_table(monkeypatch):
    addon = _load_addon(monkeypatch, _scene(True))
    addon.hunyuan_api_profile(True)["submit_body"]["Prompt"] = "leak"
    assert addon.hunyuan_api_profile(True)["submit_body"] == {"EnablePBR": True}


def test_submit_uses_the_mainland_endpoint_when_the_toggle_is_off(monkeypatch):
    addon = _load_addon(monkeypatch, _scene(False))
    server = _server_with_credentials(monkeypatch, addon)
    calls = _capture_post(monkeypatch, addon)

    assert server.create_hunyuan_job_main_site(text_prompt="a wooden stool") == {"Response": {"JobId": "job-1"}}

    call = calls[0]
    assert call["url"] == "https://ai3d.tencentcloudapi.com"
    header_values = set(call["headers"].values())
    assert {"SubmitHunyuanTo3DProJob", "2025-05-13", "ap-guangzhou"} <= header_values
    assert call["body"] == {"Prompt": "a wooden stool"}


def test_submit_uses_the_international_pro_endpoint_when_toggled(monkeypatch):
    addon = _load_addon(monkeypatch, _scene(True))
    server = _server_with_credentials(monkeypatch, addon)
    calls = _capture_post(monkeypatch, addon)

    server.create_hunyuan_job_main_site(text_prompt="a wooden stool")

    call = calls[0]
    assert call["url"] == "https://hunyuan.tencentcloudapi.com"
    header_values = set(call["headers"].values())
    assert {"SubmitHunyuanTo3DProJob", "2023-09-01", "ap-singapore"} <= header_values
    assert call["body"] == {"EnablePBR": True, "Prompt": "a wooden stool"}


def test_poll_uses_the_matching_query_profile(monkeypatch):
    addon = _load_addon(monkeypatch, _scene(True))
    server = _server_with_credentials(monkeypatch, addon)
    calls = _capture_post(monkeypatch, addon)

    server.poll_hunyuan_job_status_ai("job_abc")

    call = calls[0]
    assert call["url"] == "https://hunyuan.tencentcloudapi.com"
    assert {"QueryHunyuanTo3DProJob", "2023-09-01", "ap-singapore"} <= set(call["headers"].values())
    assert call["body"] == {"JobId": "abc"}


def test_scene_without_the_toggle_behaves_like_mainland(monkeypatch):
    scene = _scene(False)
    del scene.blendermcp_hunyuan3d_intl_pro
    addon = _load_addon(monkeypatch, scene)
    server = _server_with_credentials(monkeypatch, addon)
    calls = _capture_post(monkeypatch, addon)

    server.poll_hunyuan_job_status_ai("job_abc")

    assert calls[0]["url"] == "https://ai3d.tencentcloudapi.com"


# SecretId and SecretKey are one credential (#340). Read each on its own, the id
# could come from the preferences and the key from a scene saved with another
# account's, and Tencent answers that pair with AuthFailure.SignatureFailure.

def _credential_places(monkeypatch, addon, scene, prefs=None, env=None):
    monkeypatch.setattr(addon, "get_blendermcp_addon_preferences", lambda context=None: prefs)
    for name in ("BLENDERMCP_HUNYUAN3D_SECRET_ID", "BLENDERMCP_HUNYUAN3D_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    return addon.BlenderMCPServer()


def _prefs(secret_id="", secret_key=""):
    return types.SimpleNamespace(hunyuan3d_secret_id=secret_id, hunyuan3d_secret_key=secret_key)


def test_credentials_never_pair_an_id_with_another_places_key(monkeypatch):
    scene = _scene(False)
    scene.blendermcp_hunyuan3d_secret_id = "AKIDscene"
    scene.blendermcp_hunyuan3d_secret_key = "scenekey"
    addon = _load_addon(monkeypatch, scene)
    server = _credential_places(monkeypatch, addon, scene, prefs=_prefs(secret_id="AKIDprefs"))

    assert server._get_hunyuan3d_credentials() == ("AKIDscene", "scenekey")


def test_preferences_win_when_they_hold_both(monkeypatch):
    scene = _scene(False)
    scene.blendermcp_hunyuan3d_secret_id = "AKIDold"
    scene.blendermcp_hunyuan3d_secret_key = "oldkey"
    addon = _load_addon(monkeypatch, scene)
    server = _credential_places(monkeypatch, addon, scene, prefs=_prefs("AKIDprefs", "prefskey"))

    assert server._get_hunyuan3d_credentials() == ("AKIDprefs", "prefskey")


def test_environment_pair_is_used_when_nothing_else_is_set(monkeypatch):
    scene = _scene(False)
    addon = _load_addon(monkeypatch, scene)
    server = _credential_places(monkeypatch, addon, scene, prefs=_prefs(), env={
        "BLENDERMCP_HUNYUAN3D_SECRET_ID": "AKIDenv", "BLENDERMCP_HUNYUAN3D_SECRET_KEY": "envkey"})

    assert server._get_hunyuan3d_credentials() == ("AKIDenv", "envkey")


def test_pasted_whitespace_is_dropped(monkeypatch):
    scene = _scene(False)
    addon = _load_addon(monkeypatch, scene)
    server = _credential_places(monkeypatch, addon, scene, prefs=_prefs(" AKIDprefs\n", "prefskey \r\n"))

    assert server._get_hunyuan3d_credentials() == ("AKIDprefs", "prefskey")


def test_a_lone_half_is_reported_missing_not_signed(monkeypatch):
    scene = _scene(False)
    addon = _load_addon(monkeypatch, scene)
    server = _credential_places(monkeypatch, addon, scene, prefs=_prefs(secret_id="AKIDprefs"))
    calls = _capture_post(monkeypatch, addon)

    assert server._get_hunyuan3d_credentials() == ("AKIDprefs", "")
    assert server.create_hunyuan_job_main_site(text_prompt="a stool") == {"error": "SecretId or SecretKey is not given"}
    assert calls == []


def test_the_request_is_signed_with_the_pair_from_one_place(monkeypatch):
    scene = _scene(False)
    scene.blendermcp_hunyuan3d_secret_id = "AKIDscene"
    scene.blendermcp_hunyuan3d_secret_key = "scenekey"
    addon = _load_addon(monkeypatch, scene)
    server = _credential_places(monkeypatch, addon, scene, prefs=_prefs(secret_id=" AKIDprefs "), env={
        "BLENDERMCP_HUNYUAN3D_SECRET_KEY": "envkey"})
    signed = []
    real_sign = addon.BlenderMCPServer.get_tencent_cloud_sign_headers

    def sign(method, path, head, data, service, region, secret_id, secret_key, host=None):
        signed.append((secret_id, secret_key))
        return real_sign(method, path, head, data, service, region, secret_id, secret_key, host)

    monkeypatch.setattr(server, "get_tencent_cloud_sign_headers", sign)
    calls = _capture_post(monkeypatch, addon)

    server.create_hunyuan_job_main_site(text_prompt="a stool")

    assert signed == [("AKIDscene", "scenekey")]
    assert "Credential=AKIDscene/" in calls[0]["headers"]["Authorization"]
