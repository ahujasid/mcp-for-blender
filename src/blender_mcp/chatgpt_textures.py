"""Texture images made by the user's own ChatGPT subscription, through a browser.

There is no API for a ChatGPT plan, so this drives chatgpt.com in a Chrome
window the user can see. Chrome is started normally, with its own profile and a
DevTools port, and Playwright attaches to it over CDP afterwards. Starting it
without automation flags matters: Google sign-in refuses browsers launched by
an automation driver, and the user signs in by hand in that window.

The user stays in charge of the account. Nothing here types a password or
answers a captcha: when ChatGPT wants a sign-in or a human check, the job
brings the window to the front, raises a desktop notification, and waits for
the user while the tool reports what it is waiting for.

chatgpt.com changes often and its class names are generated, so every element
is found from a list of candidates in SELECTORS, preferring data attributes
and ids over visible text (the interface is localized). When nothing matches,
a job ends with a screenshot of the window instead of hanging.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("BlenderMCPChatGPT")

CHATGPT_URL = "https://chatgpt.com/"
DEFAULT_CDP_PORT = 9333
# How long one job waits for the user to sign in or pass a check, in total.
USER_ACTION_TIMEOUT_S = 15 * 60
# How long ChatGPT gets to finish an image once the prompt is sent.
GENERATION_TIMEOUT_S = 5 * 60
# An image counts as final once its source has not changed for this long
# after generation stopped (ChatGPT streams blurry previews first).
SETTLE_S = 4.0
MIN_IMAGE_PX = 512
POLL_S = 1.5

SELECTORS = {
    "composer": [
        "#prompt-textarea",
        "#mobile-composer-prompt",
        "textarea[name='prompt-textarea']",
        "[contenteditable='true'][role='textbox']",
        "form textarea",
    ],
    "submit": [
        "[data-testid='send-button']",
        "button[data-composer-submit]",
        "form button[type='submit']",
    ],
    "file_input": [
        "input[type='file'][accept*='image']:not([id*='camera'])",
        "input[type='file']:not([id*='camera'])",
    ],
    "assistant_message": [
        "[data-message-author-role='assistant']",
        "[data-testid^='conversation-turn'] [data-message-author-role='assistant']",
    ],
}

# Runs in the page. Language independent: it reads data attributes, ids, URLs
# and iframe sources, plus a few titles that challenge pages use.
_PAGE_STATE_JS = r"""
(sel) => {
  const any = (list) => list.some((s) => { try { return !!document.querySelector(s); } catch (e) { return false; } });
  const visible = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
  const host = location.hostname, path = location.pathname, title = document.title || "";
  const frames = [...document.querySelectorAll("iframe")].map((f) => f.src || "");
  const challenge =
    path.includes("/cdn-cgi/") ||
    frames.some((s) => /challenges\.cloudflare\.com|arkoselabs|funcaptcha|hcaptcha|recaptcha/.test(s)) ||
    any(["#challenge-form", "#cf-challenge-running", "[name='cf-turnstile-response']", "#turnstile-wrapper"]) ||
    /just a moment|un momento|verify you are human|verifica que eres|checking your browser/i.test(title);
  const authHost = /(^|\.)auth\.openai\.com$|(^|\.)auth0\.openai\.com$|accounts\.google\.com$|login\.live\.com$|login\.microsoftonline\.com$|appleid\.apple\.com$/.test(host);
  const loginButtons = [...document.querySelectorAll(
    "[data-login-button], [data-testid='login-button'], [data-mobile-auth-entry-action='login'], a[href*='/auth/login']"
  )].some(visible);
  const login = authHost || /\/auth\/login|\/log-in/.test(path) || loginButtons;
  const composer = sel.composer.some((s) => visible(document.querySelector(s)));
  const submit = sel.submit.map((s) => document.querySelector(s)).find((b) => !!b);
  const stopLabel = submit && submit.getAttribute("data-stop-label");
  const generating =
    any(["[data-testid='stop-button']", "button[aria-label*='Stop' i]"]) ||
    (!!stopLabel && submit.getAttribute("aria-label") === stopLabel);
  const alerts = [...document.querySelectorAll("[role='alert'], [data-testid*='toast']")]
    .filter(visible).map((a) => a.innerText.trim()).filter(Boolean).slice(0, 3);
  return { url: location.href, title, challenge, login, composer, generating, alerts,
           submitDisabled: !!submit && (submit.disabled || submit.getAttribute("aria-disabled") === "true") };
}
"""

# Large images on the page, by source, so a new one can be told from the rest.
_IMAGES_JS = r"""
(minPx) => [...document.querySelectorAll("img")]
  .filter((i) => i.complete && i.naturalWidth >= minPx && i.naturalHeight >= minPx && i.src && !i.src.startsWith("data:image/svg"))
  .map((i) => ({ src: i.src, w: i.naturalWidth, h: i.naturalHeight }))
"""

_LAST_REPLY_JS = r"""
(sels) => {
  for (const s of sels) {
    const all = document.querySelectorAll(s);
    if (all.length) return all[all.length - 1].innerText.trim().slice(0, 1500);
  }
  return "";
}
"""

# Downscales PNG/JPEG bytes in the page, which already has an image decoder, so
# the server needs no imaging library. Returns base64 JPEG.
_PREVIEW_JS = r"""
async ([b64, size]) => {
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  const bitmap = await createImageBitmap(new Blob([bytes]));
  const scale = Math.min(1, size / Math.max(bitmap.width, bitmap.height));
  const canvas = new OffscreenCanvas(Math.round(bitmap.width * scale), Math.round(bitmap.height * scale));
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  const blob = await canvas.convertToBlob({ type: "image/jpeg", quality: 0.85 });
  const buf = new Uint8Array(await blob.arrayBuffer());
  let s = ""; for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode(...buf.subarray(i, i + 0x8000));
  return btoa(s);
}
"""


class ChatGPTUnavailable(Exception):
    """Chrome or Playwright can't be used on this machine."""


def textures_dir() -> Path:
    base = os.getenv("BLENDERMCP_TEXTURES_DIR")
    path = Path(base) if base else Path.home() / ".mcp-for-blender" / "textures"
    path.mkdir(parents=True, exist_ok=True)
    return path


def profile_dir() -> Path:
    base = os.getenv("BLENDERMCP_CHATGPT_PROFILE")
    path = Path(base) if base else Path.home() / ".mcp-for-blender" / "chatgpt-profile"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cdp_port() -> int:
    try:
        return int(os.getenv("BLENDERMCP_CHATGPT_CDP_PORT", DEFAULT_CDP_PORT))
    except ValueError:
        return DEFAULT_CDP_PORT


def find_browser(platform: str | None = None, env=None, which=shutil.which) -> str | None:
    """Chrome, or another Chromium browser with the same DevTools protocol."""
    env = os.environ if env is None else env
    platform = platform or sys.platform
    override = env.get("BLENDERMCP_CHATGPT_BROWSER")
    if override:
        return override
    candidates: list[str] = []
    if platform == "win32":
        for root in (env.get("PROGRAMFILES"), env.get("PROGRAMFILES(X86)"), env.get("LOCALAPPDATA")):
            if root:
                candidates.append(os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"))
        for root in (env.get("PROGRAMFILES(X86)"), env.get("PROGRAMFILES")):
            if root:
                candidates.append(os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"))
    elif platform == "darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
        found = which(name)
        if found:
            return found
    return None


def build_prompt(prompt: str, tileable: bool = True, has_reference: bool = False) -> str:
    """The message sent to ChatGPT. English steers its image model most reliably;
    the user's description goes in as written, in any language."""
    subject = prompt.strip().rstrip(".")
    if tileable:
        text = (
            f"Create an image: a seamless, tileable texture of {subject}. "
            "Flat orthographic view straight onto the surface, filling the whole square edge to edge, "
            "so the left edge continues into the right and the top into the bottom. "
            "Even, diffuse lighting with no cast shadows, highlights or vignette; no perspective, "
            "no objects in front of it, no text, no border, no watermark. Square 1:1."
        )
    else:
        text = (
            f"Create an image: a flat texture image of {subject}, seen straight on and filling the frame, "
            "with even lighting, no perspective, no text, no border. Square 1:1."
        )
    if has_reference:
        text += " Match the material, colors and style of the attached reference image."
    return text + " Reply with the image only."


def safe_name(text: str, fallback: str = "Texture") -> str:
    words = re.findall(r"[A-Za-z0-9]+", text or "")
    name = "".join(w.capitalize() for w in words[:4])[:40]
    return name or fallback


def notify(title: str, message: str) -> None:
    """Best-effort desktop notification, so the user sees a request while in another app."""
    try:
        env = {**os.environ, "MCPFB_TITLE": title, "MCPFB_MESSAGE": message}
        if sys.platform == "win32":
            script = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
                "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                "$n=$t.GetElementsByTagName('text');"
                "$n.Item(0).AppendChild($t.CreateTextNode($env:MCPFB_TITLE)) > $null;"
                "$n.Item(1).AppendChild($t.CreateTextNode($env:MCPFB_MESSAGE)) > $null;"
                "$id='{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show("
                "[Windows.UI.Notifications.ToastNotification]::new($t))"
            )
            subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e", "on run argv", "-e",
                              "display notification (item 2 of argv) with title (item 1 of argv)",
                              "-e", "end run", title, message],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("notify-send"):
            subprocess.Popen(["notify-send", title, message], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        logger.debug(f"Notification failed: {e}")


USER_ACTIONS = {
    "login": ("Sign in to ChatGPT",
              "Sign in to ChatGPT in the Chrome window that opened (the MCP for Blender profile). "
              "The texture continues by itself once you are signed in."),
    "challenge": ("ChatGPT needs a human check",
                  "ChatGPT is showing a verification (captcha or 'verify you are human') in the Chrome window. "
                  "Complete it there; the texture continues by itself."),
}


@dataclass
class Job:
    id: str
    prompt: str
    name: str
    tileable: bool
    reference: str | None
    status: str = "starting"  # starting, waiting_user, generating, done, failed
    action: str | None = None  # login / challenge while waiting_user
    detail: str = ""
    image_path: str | None = None
    preview_jpeg: bytes | None = None
    screenshot: bytes | None = None
    started: float = field(default_factory=time.monotonic)
    attempts: int = 0
    material_reply: str | None = None
    task: asyncio.Task | None = None

    @property
    def finished(self) -> bool:
        return self.status in ("done", "failed")


class ChatGPTBrowser:
    """One Chrome window shared by every job; jobs take turns on it."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()
        self.jobs: dict[str, Job] = {}

    async def _connect(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise ChatGPTUnavailable(
                "Playwright is not installed. Reinstall mcp-for-blender (it is a dependency).") from e
        if self._browser and self._browser.is_connected():
            return self._browser
        if self._pw is None:
            self._pw = await async_playwright().start()
        endpoint = f"http://127.0.0.1:{cdp_port()}"
        try:
            self._browser = await self._pw.chromium.connect_over_cdp(endpoint, timeout=3000)
            return self._browser
        except Exception:
            pass
        exe = find_browser()
        if not exe:
            raise ChatGPTUnavailable(
                "Google Chrome (or Edge/Chromium) was not found. Install Chrome, or set "
                "BLENDERMCP_CHATGPT_BROWSER to a Chromium browser's executable.")
        subprocess.Popen(
            [exe, f"--remote-debugging-port={cdp_port()}", f"--user-data-dir={profile_dir()}",
             "--no-first-run", "--no-default-browser-check", "--window-size=1280,900", CHATGPT_URL],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            await asyncio.sleep(0.7)
            try:
                self._browser = await self._pw.chromium.connect_over_cdp(endpoint, timeout=3000)
                return self._browser
            except Exception:
                continue
        raise ChatGPTUnavailable(
            f"Started {exe} but could not reach it on port {cdp_port()}. If Chrome was already open with "
            "this profile, close that window and try again, or set BLENDERMCP_CHATGPT_CDP_PORT.")

    async def _page(self):
        browser = await self._connect()
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        for page in context.pages:
            if urlparse(page.url).hostname in ("chatgpt.com", "chat.openai.com"):
                return page
        return context.pages[0] if context.pages else await context.new_page()

    def start(self, prompt: str, name: str | None, tileable: bool, reference: str | None) -> Job:
        job = Job(id=uuid.uuid4().hex[:8], prompt=prompt, name=name or safe_name(prompt),
                  tileable=tileable, reference=reference)
        self.jobs[job.id] = job
        job.task = asyncio.get_running_loop().create_task(self._run(job))
        return job

    async def wait(self, job: Job, seconds: float, stop_on_user_action: bool = True) -> Job:
        """Wait for the job to finish for at most `seconds`.

        With stop_on_user_action, return as soon as it needs the user, so the
        model can tell them straight away instead of sitting on the request.
        """
        deadline = time.monotonic() + max(0.0, seconds)
        while not job.finished and time.monotonic() < deadline:
            if stop_on_user_action and job.status == "waiting_user":
                break
            await asyncio.sleep(0.5)
        return job

    async def _run(self, job: Job):
        try:
            async with self._lock:
                await self._generate(job)
        except ChatGPTUnavailable as e:
            job.status, job.detail = "failed", str(e)
        except Exception as e:
            logger.exception("ChatGPT texture job failed")
            job.status, job.detail = "failed", f"{type(e).__name__}: {e}"
            await self._snap(job)

    async def _state(self, page) -> dict:
        try:
            return await page.evaluate(_PAGE_STATE_JS, SELECTORS)
        except Exception:
            # Mid-navigation; treat as not ready yet.
            return {"challenge": False, "login": False, "composer": False, "generating": False, "alerts": []}

    async def _snap(self, job: Job):
        try:
            page = await self._page()
            job.screenshot = await page.screenshot(type="jpeg", quality=70)
        except Exception:
            pass

    async def _until_ready(self, job: Job, page) -> bool:
        """Wait until the composer is usable, asking the user for sign-in or checks."""
        notified = None
        deadline = time.monotonic() + USER_ACTION_TIMEOUT_S
        while time.monotonic() < deadline:
            st = await self._state(page)
            action = "challenge" if st["challenge"] else "login" if st["login"] else None
            if action is None and st["composer"]:
                job.status, job.action = "starting", None
                return True
            if action:
                job.status, job.action = "waiting_user", action
                job.detail = USER_ACTIONS[action][1]
                if notified != action:
                    notified = action
                    try:
                        await page.bring_to_front()
                    except Exception:
                        pass
                    notify(USER_ACTIONS[action][0], USER_ACTIONS[action][1])
            await asyncio.sleep(POLL_S)
        job.status = "failed"
        job.detail = "Gave up waiting for the ChatGPT window to be ready (sign-in or verification not completed)."
        await self._snap(job)
        return False

    async def _first(self, page, key: str):
        for sel in SELECTORS[key]:
            loc = page.locator(sel).first
            try:
                if await loc.count():
                    return loc
            except Exception:
                continue
        return None

    async def _generate(self, job: Job):
        job.attempts += 1
        page = await self._page()
        await page.goto(CHATGPT_URL, wait_until="domcontentloaded")
        if not await self._until_ready(job, page):
            return
        if job.reference:
            ref = await _local_reference(job.reference)
            file_input = await self._first(page, "file_input")
            if file_input is None:
                raise RuntimeError("Could not find ChatGPT's file upload field for the reference image.")
            await file_input.set_input_files(ref)
        composer = await self._first(page, "composer")
        if composer is None:
            raise RuntimeError("Could not find ChatGPT's message box.")
        await composer.click()
        await composer.fill(build_prompt(job.prompt, job.tileable, bool(job.reference)))
        before = {img["src"] for img in await page.evaluate(_IMAGES_JS, MIN_IMAGE_PX)}

        # An attached image keeps the send button disabled until it has uploaded.
        deadline = time.monotonic() + 90
        while (await self._state(page)).get("submitDisabled") and time.monotonic() < deadline:
            await asyncio.sleep(1)
        submit = await self._first(page, "submit")
        try:
            if submit is not None and await submit.is_enabled():
                await submit.click()
            else:
                await composer.press("Enter")
        except Exception:
            await composer.press("Enter")

        job.status, job.detail = "generating", "ChatGPT is generating the image."
        image = await self._wait_for_image(job, page, before)
        if image is None:
            return
        data = await _download(page, image["src"])
        ext = ".png" if data[:8] == b"\x89PNG\r\n\x1a\n" else ".webp" if data[8:12] == b"WEBP" else ".jpg"
        path = textures_dir() / f"{job.name}_{job.id}{ext}"
        path.write_bytes(data)
        job.image_path = str(path)
        try:
            job.preview_jpeg = base64.b64decode(
                await page.evaluate(_PREVIEW_JS, [base64.b64encode(data).decode("ascii"), 384]))
        except Exception as e:
            logger.debug(f"Preview failed: {e}")
        job.status, job.detail = "done", f"{image['w']}x{image['h']} image saved to {path}"

    async def _wait_for_image(self, job: Job, page, before: set[str]) -> dict | None:
        deadline = time.monotonic() + GENERATION_TIMEOUT_S
        sent_at = time.monotonic()
        last_src, stable_since = None, None
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_S)
            st = await self._state(page)
            if st["challenge"] or st["login"]:
                action = "challenge" if st["challenge"] else "login"
                job.status, job.action, job.detail = "waiting_user", action, USER_ACTIONS[action][1]
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
                notify(*USER_ACTIONS[action])
                # The prompt is lost when ChatGPT sends the user away; start over once they're back.
                if job.attempts < 3 and await self._until_ready(job, page):
                    job.status, job.detail = "starting", "Resending the prompt after sign-in/verification."
                    await self._generate(job)
                elif job.status != "failed":
                    job.status, job.detail = "failed", "Interrupted by sign-in/verification too many times."
                return None
            new = [img for img in await page.evaluate(_IMAGES_JS, MIN_IMAGE_PX) if img["src"] not in before]
            if new and not st["generating"]:
                src = new[-1]["src"]
                if src != last_src:
                    last_src, stable_since = src, time.monotonic()
                elif time.monotonic() - stable_since >= SETTLE_S:
                    return new[-1]
            elif not new and not st["generating"] and time.monotonic() - sent_at > 20:
                # Finished without an image: ChatGPT answered in text (a question, a refusal,
                # a usage limit). Hand its words back so the user can decide.
                reply = await page.evaluate(_LAST_REPLY_JS, SELECTORS["assistant_message"])
                alerts = " ".join(st.get("alerts") or [])
                job.status = "failed"
                job.detail = ("ChatGPT replied without making an image"
                              + (f": {reply}" if reply else "") + (f" [notice: {alerts}]" if alerts else ""))
                await self._snap(job)
                return None
        job.status = "failed"
        job.detail = f"No image appeared within {GENERATION_TIMEOUT_S // 60} minutes."
        await self._snap(job)
        return None

async def _local_reference(reference: str) -> str:
    if urlparse(reference).scheme in ("http", "https"):
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            r = await client.get(reference)
            r.raise_for_status()
        suffix = Path(urlparse(reference).path).suffix or ".png"
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="mcpfb_ref_")
        with os.fdopen(fd, "wb") as f:
            f.write(r.content)
        return path
    if not os.path.isfile(reference):
        raise ChatGPTUnavailable(f"Reference image not found: {reference}. Give an absolute path or an http(s) URL.")
    return reference


_BLOB_JS = r"""
async (src) => {
  const buf = new Uint8Array(await (await fetch(src)).arrayBuffer());
  let s = ""; for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode(...buf.subarray(i, i + 0x8000));
  return btoa(s);
}
"""


async def _download(page, src: str) -> bytes:
    """Fetch the image. blob: and data: URLs only resolve inside the page; http(s)
    goes through the browser context, which has the cookies and no CORS rules."""
    if not src.startswith(("http:", "https:")):
        return base64.b64decode(await page.evaluate(_BLOB_JS, src))
    response = await page.context.request.get(src)
    if not response.ok:
        raise RuntimeError(f"Downloading the generated image failed: HTTP {response.status}")
    return await response.body()


_browser: ChatGPTBrowser | None = None


def get_browser() -> ChatGPTBrowser:
    global _browser
    if _browser is None:
        _browser = ChatGPTBrowser()
    return _browser


def describe(job: Job) -> str:
    """One status line for the model, with what to do next."""
    if job.status == "done":
        return f"done: {job.detail}"
    if job.status == "failed":
        return f"failed: {job.detail}"
    if job.status == "waiting_user":
        return (f"waiting for the user ({job.action}): {job.detail} Tell the user this, then call "
                f"generate_texture(job=\"{job.id}\") to keep waiting.")
    return (f"{job.status}: {job.detail or 'working'} Call generate_texture(job=\"{job.id}\") to keep waiting "
            "(images usually take 30-90 seconds).")


__all__ = [
    "ChatGPTBrowser", "ChatGPTUnavailable", "Job", "SELECTORS", "build_prompt", "describe", "find_browser",
    "get_browser", "notify", "safe_name", "textures_dir",
]
