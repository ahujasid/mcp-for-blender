"""MCP Apps and OpenAI MCP extensions, for clients that support them.

Everything here is additive: a client that advertises none of these
capabilities (Claude Code, Cursor, ...) sees the same tools and replies as
before. Spec: https://github.com/openai/mcp-extensions/blob/main/docs/spec.md

- Asset pickers: search tools show results as a thumbnail grid via
  `openai/elicitation/create` and return what the user picked.
- Composer @-mentions: an app-only tool that searches scene objects,
  materials and collections.
- Viewport app: the latest viewport screenshot, inline in the chat whenever
  the model takes one (expandable to fullscreen), or as a thread tab. It
  recaptures after the model changes the scene, and clicking an object in it
  attaches that object to the next message.

OpenAI's Python SDK for these needs the `mcp` 2.0 beta, so the few wire shapes
we use are written out by hand against `mcp` 1.x.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from importlib import resources
from typing import Any, Literal

from mcp.shared.message import ServerMessageMetadata
from mcp.types import ElicitResult, Icon
from pydantic import BaseModel

logger = logging.getLogger("BlenderMCPServer")

APPS_EXTENSION = "io.modelcontextprotocol/ui"
APP_MIME_TYPE = "text/html;profile=mcp-app"
OPENAI_ELICITATION_EXTENSION = "openai/elicitation"
OPENAI_ELICITATION_METHOD = "openai/elicitation/create"

# Force these on or off. Codex's MCP client advertises neither capability even
# where it supports them, so the Codex plugin turns both on explicitly.
APPS_ENV = "BLENDER_MCP_APPS"
OPENAI_FORMS_ENV = "BLENDER_MCP_OPENAI_FORMS"

# Set on a session whose client refused an OpenAI form, so we stop offering pickers.
_FORMS_REFUSED_ATTR = "_blender_mcp_openai_forms_refused"


def _env_flag(name: str) -> bool | None:
    value = os.environ.get(name, "").strip().lower()
    if value in {"1", "true", "on"}:
        return True
    if value in {"0", "false", "off"}:
        return False
    return None


# ---------------------------------------------------------------- capabilities

def _client_capabilities(session: Any):
    params = getattr(session, "client_params", None)
    return getattr(params, "capabilities", None) if params else None


def client_extensions(session: Any) -> dict[str, Any]:
    """Every extension the client advertised, from `extensions` and `experimental`.

    `mcp` 1.x has no `extensions` field on ClientCapabilities, but keeps unknown
    keys, so it arrives in model_extra.
    """
    caps = _client_capabilities(session)
    if caps is None:
        return {}
    merged: dict[str, Any] = {}
    for source in (getattr(caps, "experimental", None), (caps.model_extra or {}).get("extensions")):
        if isinstance(source, dict):
            merged.update(source)
    return merged


def supports_openai_forms(session: Any) -> bool:
    if session is None or getattr(session, _FORMS_REFUSED_ATTR, False):
        return False
    override = _env_flag(OPENAI_FORMS_ENV)
    if override is not None:
        return override
    settings = client_extensions(session).get(OPENAI_ELICITATION_EXTENSION)
    return isinstance(settings, dict) and isinstance(settings.get("form"), dict)


def supports_apps(session: Any) -> bool:
    """Whether the client renders MCP Apps and honours `visibility: ["app"]`.

    Any OpenAI extension counts too: ChatGPT and Codex support MCP Apps, and
    hiding the Viewport entrypoint from them by mistake costs more than showing
    two app-only tools to a client that ignores them.
    """
    override = _env_flag(APPS_ENV)
    if override is not None:
        return override
    return any(key == APPS_EXTENSION or key.startswith("openai/") for key in client_extensions(session))


def is_app_only(tool: Any) -> bool:
    meta = getattr(tool, "meta", None) or {}
    ui = meta.get("ui") if isinstance(meta, dict) else None
    return isinstance(ui, dict) and ui.get("visibility") == ["app"]


# ---------------------------------------------------------------- asset picker

@dataclass
class PickerOption:
    id: str
    title: str
    description: str | None = None
    thumbnail: str | None = None


class _OpenAIFormRequest(BaseModel):
    method: Literal["openai/elicitation/create"] = OPENAI_ELICITATION_METHOD
    params: dict[str, Any]


def picker_schema(title: str, options: list[PickerOption]) -> dict[str, Any]:
    """A single-select form field whose options render as a thumbnail grid."""
    choices = []
    for option in options:
        choice: dict[str, Any] = {"const": option.id, "title": option.title}
        if option.description:
            choice["description"] = option.description
        if option.thumbnail:
            choice["x-openai-thumbnail"] = {"src": option.thumbnail}
        choices.append(choice)
    return {
        "type": "object",
        "properties": {"asset": {"type": "string", "title": title, "oneOf": choices}},
        "required": ["asset"],
    }


@dataclass
class PickResult:
    action: Literal["accept", "decline", "cancel"]
    asset_id: str | None = None


async def pick_asset(ctx: Any, message: str, title: str, options: list[PickerOption]) -> PickResult | None:
    """Ask the user to pick one option, or None when no picker could be shown.

    None covers clients without OpenAI forms and any failure showing the form,
    so callers fall back to returning the plain result list.
    """
    options = [o for o in options if o.id]
    try:
        session = ctx.session
    except Exception:
        session = None
    if not options or not supports_openai_forms(session):
        return None
    ids = {o.id for o in options}
    request = _OpenAIFormRequest(
        params={"mode": "form", "message": message, "requestedSchema": picker_schema(title, options)}
    )
    try:
        result = await session.send_request(
            request,
            ElicitResult,
            metadata=ServerMessageMetadata(related_request_id=ctx.request_id),
        )
    except Exception as e:
        # A client that can't show the form answers with an error straight away,
        # so don't keep asking it for the rest of the session.
        try:
            setattr(session, _FORMS_REFUSED_ATTR, True)
        except Exception:
            pass
        logger.warning(f"Asset picker could not be shown, falling back to a text list: {e}")
        return None
    if result.action != "accept":
        return PickResult(result.action)
    chosen = (result.content or {}).get("asset")
    if chosen not in ids:
        logger.warning(f"Asset picker returned an unknown choice: {chosen!r}")
        return None
    return PickResult("accept", chosen)


def picked_reply(source: str, picked: PickResult, blocks: dict[str, str], full_listing: str) -> str:
    """The tool reply after a picker was shown."""
    if picked.action == "accept":
        return (
            f"The user picked this {source} result from a visual picker. Use it; don't "
            f"search again or pick a different one unless they ask.\n\n{blocks[picked.asset_id]}"
        )
    return (
        f"These {source} results were shown to the user as a visual picker and they closed "
        "it without choosing. Don't pick one for them: ask what they'd like instead.\n\n"
        + full_listing
    )


# ---------------------------------------------------------------- viewport app

# v2 is fullscreen-only; a new URI so hosts don't reuse the cached inline v1.
VIEWPORT_URI = "ui://mcp-for-blender/viewport-v2"
VIEWPORT_TITLE = "Blender Viewport"
# Where get_viewport_screenshot puts the capture state for the app, out of the
# model's sight (structuredContent would reach the model too).
VIEWPORT_STATE_META = "mcp-for-blender/viewport"

# The mcp-for-blender.com favicon cube, monochrome in currentColor so the
# host can theme it, on the 20px viewbox the entrypoint icon guidelines ask for.
_VIEWPORT_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20" fill="currentColor">'
    '<path d="M10 2.54 16.13 6.05 10 9.57 3.87 6.05z"/><path d="M3.4 6.95 9.57 10.47V17.5L3.4 13.98z"/>'
    '<path d="M10.47 10.47 16.6 6.95v7.03l-6.13 3.52z" opacity=".5"/></svg>'
)


def viewport_icon() -> Icon:
    from urllib.parse import quote

    return Icon(
        src="data:image/svg+xml," + quote(_VIEWPORT_ICON_SVG),
        mimeType="image/svg+xml",
        sizes=["any"],
    )


def viewport_html() -> str:
    return resources.files("blender_mcp").joinpath("apps/viewport.html").read_text(encoding="utf-8")


# Blender commands that can change what the viewport shows. Any of these bumps
# the scene version, which tells an open Viewport app to capture again.
SCENE_CHANGING_COMMANDS = frozenset({
    "execute_code",
    "set_texture",
    "download_polyhaven_asset",
    "download_sketchfab_model",
    "download_polypizza_model",
    "import_generated_asset",
    "import_generated_asset_hunyuan",
    "import_generated_asset_tripo",
})

# How long Blender must have been quiet before the app captures on its own, so
# a run of edits gives one capture at the end instead of one per edit.
AUTO_CAPTURE_QUIET_S = 0.8

# Views kept for clicking on older captures; a view is two 4x4 matrices.
_KEPT_VIEWS = 50

CaptureSource = Literal["model", "user", "auto"]


class ViewportStore:
    """The latest viewport screenshot, shared by the model's tool and the app.

    The app polls this rather than Blender, so an open panel costs nothing on
    Blender's main thread until someone asks for a fresh capture. It also
    counts scene-changing commands, so the app knows when its image is stale.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._png: bytes | None = None
        self._captured_at: float | None = None
        self._source: str | None = None
        self._origin: dict[str, Any] = {}
        self._scene_version = 0
        self._captured_scene_version = 0
        self._commands_running = 0
        self._last_command_at = 0.0
        # seq -> the camera a capture was rendered with, for clicking on it.
        self._views: dict[int, dict[str, Any]] = {}

    def put(self, png: bytes, source: CaptureSource, view: dict[str, Any] | None = None,
            scene_version: int | None = None, origin: dict[str, Any] | None = None) -> None:
        """Store a capture. `scene_version` is the version read before capturing
        began, so an edit that lands mid-capture still counts as unseen.
        `origin` is the file and scene it shows, from addons that report them."""
        with self._lock:
            self._seq += 1
            self._png = png
            self._captured_at = time.time()
            self._source = source
            self._origin = dict(origin or {})
            self._captured_scene_version = self._scene_version if scene_version is None else scene_version
            if view:
                self._views[self._seq] = view
                for old in sorted(self._views)[:-_KEPT_VIEWS]:
                    del self._views[old]

    def view(self, seq: int) -> dict[str, Any] | None:
        with self._lock:
            return self._views.get(seq)

    @property
    def scene_version(self) -> int:
        with self._lock:
            return self._scene_version

    def command_started(self) -> None:
        with self._lock:
            self._commands_running += 1

    def command_finished(self, command_type: str) -> None:
        with self._lock:
            self._commands_running = max(0, self._commands_running - 1)
            self._last_command_at = time.time()
            if command_type in SCENE_CHANGING_COMMANDS:
                self._scene_version += 1

    def snapshot(self) -> tuple[dict[str, Any], bytes | None]:
        with self._lock:
            quiet = self._commands_running == 0 and time.time() - self._last_command_at >= AUTO_CAPTURE_QUIET_S
            state = {
                "seq": self._seq,
                "captured_at": self._captured_at,
                "source": self._source,
                # Full .blend path ("" if unsaved) and scene name, shown so
                # it's clear which Blender file a screenshot is of.
                "file": self._origin.get("file"),
                "scene": self._origin.get("scene"),
                "scene_count": self._origin.get("scene_count"),
                "pickable": self._seq in self._views,
                # The scene changed after this image was taken.
                "stale": self._scene_version > self._captured_scene_version,
                "blender_quiet": quiet,
            }
            return state, self._png


viewport_store = ViewportStore()
