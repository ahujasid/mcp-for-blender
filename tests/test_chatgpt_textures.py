"""generate_texture: ChatGPT-made textures through the user's browser.

The browser itself is faked here; these cover the prompt, the job lifecycle the
model sees (waiting for the user, resuming, done) and the Blender script.
"""

import ast
import asyncio

from blender_mcp import blender_scripts, chatgpt_textures, server
from blender_mcp.chatgpt_textures import ChatGPTBrowser, Job, build_prompt, describe, find_browser, safe_name


def test_tileable_prompt_asks_for_a_seamless_flat_texture():
    text = build_prompt("musgo sobre piedra", tileable=True)
    assert "musgo sobre piedra" in text
    assert "seamless" in text and "tileable" in text and "no text" in text
    assert "reference" not in text


def test_one_off_prompt_is_not_tileable_and_mentions_the_reference():
    text = build_prompt("a wooden witch sign", tileable=False, has_reference=True)
    assert "seamless" not in text
    assert "attached reference image" in text


def test_safe_name_makes_a_material_name():
    assert safe_name("old mossy cobblestones, wet") == "OldMossyCobblestonesWet"
    assert safe_name("¡!") == "Texture"


def test_find_browser_prefers_the_override_then_chrome(tmp_path):
    assert find_browser("win32", env={"BLENDERMCP_CHATGPT_BROWSER": "C:/x/chrome.exe"}) == "C:/x/chrome.exe"
    chrome = tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe"
    chrome.parent.mkdir(parents=True)
    chrome.write_text("")
    assert find_browser("win32", env={"PROGRAMFILES": str(tmp_path)}, which=lambda _: None) == str(chrome)
    assert find_browser("linux", env={}, which=lambda n: "/usr/bin/chromium" if n == "chromium" else None) \
        == "/usr/bin/chromium"
    assert find_browser("linux", env={}, which=lambda _: None) is None


def test_waiting_for_the_user_tells_the_model_what_to_relay():
    job = Job(id="abc123", prompt="p", name="P", tileable=True, reference=None,
              status="waiting_user", action="login", detail=chatgpt_textures.USER_ACTIONS["login"][1])
    text = describe(job)
    assert "Sign in to ChatGPT" in text and 'job="abc123"' in text


def test_texture_material_script_compiles():
    code = blender_scripts.build(blender_scripts.TEXTURE_MATERIAL, {
        "image_path": "C:/t/x.png", "name": "X", "apply_to": ["Casa"], "tile_size": 1.0,
        "projection": "box", "bump": 0.25})
    ast.parse(code)
    assert "SystemExit" not in code and 'n.type == "BSDF_PRINCIPLED"' in code


class FakeBrowser(ChatGPTBrowser):
    """Needs the user once, then finishes."""

    def __init__(self):
        super().__init__()
        self.user_done = asyncio.Event()

    async def _generate(self, job):
        job.status, job.action, job.detail = "waiting_user", "login", chatgpt_textures.USER_ACTIONS["login"][1]
        await self.user_done.wait()
        job.status, job.image_path, job.preview_jpeg, job.detail = "done", "C:/t/x.png", b"jpeg", "ok"


def test_tool_reports_sign_in_then_finishes_on_resume(monkeypatch):
    fake = FakeBrowser()
    monkeypatch.setattr(chatgpt_textures, "get_browser", lambda: fake)
    made = []
    monkeypatch.setattr(server, "_texture_material",
                        lambda job, apply_to, *a: made.append(apply_to) or "Material 'Moss' made.")

    async def scenario():
        first = await server.generate_texture(None, prompt="moss", apply_to=["Casa"], wait_seconds=10)
        text = first.content[0].text
        assert text.startswith("waiting for the user (login)")
        job_id = text.split('job="')[1].split('"')[0]
        fake.user_done.set()
        second = await server.generate_texture(None, job=job_id, apply_to=["Casa"], wait_seconds=10)
        assert "Material 'Moss' made." in second.content[0].text
        assert second.content[1].type == "image"
        # Asking again doesn't build a second material.
        again = await server.generate_texture(None, job=job_id, apply_to=["Casa"])
        assert "Material 'Moss' made." in again.content[0].text
        assert made == [["Casa"]]

    asyncio.run(scenario())


def test_unknown_job_and_missing_prompt_are_errors(monkeypatch):
    monkeypatch.setattr(chatgpt_textures, "get_browser", lambda: ChatGPTBrowser())
    assert "unknown job" in asyncio.run(server.generate_texture(None, job="nope")).content[0].text
    assert "give a prompt" in asyncio.run(server.generate_texture(None)).content[0].text
    assert "projection" in asyncio.run(server.generate_texture(None, prompt="x", projection="tri")).content[0].text
