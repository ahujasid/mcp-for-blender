"""One generate/poll/import flow across Tripo, Hunyuan3D, Hyper3D Rodin and Meshy,
and for World Labs worlds (generate_gaussian_world).

Each provider used to be three or four tools with different id shapes. Here a
generation is a single call: submit, poll until done or out of time, import.
If time runs out the caller gets a job handle ("<provider>:<kind>:<id>") and
passes it back to resume, so no client timeout ever strands a paid generation.

The addon still does the provider work (and the Premium routing), through the
commands it has always had, so this needs no addon update. Meshy is the
exception: it is new, and on the user's own key its text generation is two
paid tasks, a preview (mesh) and then a refine (texture), which this module
chains and resumes. Through Premium, the Premium server runs both as one job.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from .premium_hint import PREMIUM_URL

logger = logging.getLogger("BlenderMCPServer")

PROVIDERS = ("tripo", "hunyuan3d", "hyper3d", "meshy")
# World generators: whole environments for generate_gaussian_world, never generate_3d.
WORLD_PROVIDERS = ("worldlabs",)

# Auto-pick order. Premium generators come first because the user is paying
PREMIUM_ORDER = ("tripo", "meshy", "hyper3d", "hunyuan3d")
OWN_KEY_ORDER = ("hunyuan3d", "hyper3d", "tripo", "meshy")


# Meshy task kinds: text preview, text refine, image to model, and a Premium
# job (either input, preview and refine done by the Premium server).
MESHY_KINDS = ("t2p", "t2r", "i2m", "prm")
# Addons from this protocol on can run Meshy through Premium.
MESHY_PREMIUM_PROTOCOL = 14
# Addons from this protocol on pass pose_mode to Meshy.
MESHY_POSE_PROTOCOL = 15
# World Labs job kinds: an operation on the user's own key, or a Premium job.
WORLDLABS_KINDS = ("op", "prm")
# Addons from this protocol on have World Labs.
WORLDLABS_PROTOCOL = 16
# Splat files a finished world offers, smallest first.
WORLD_RESOLUTIONS = ("100k", "500k", "full_res")
# Meshy's pose_mode values; the model comes out in that pose, ready to rig.
MESHY_POSES = ("a-pose", "t-pose")
MESHY_PROMPT_MAX = 800
TRIPO_PROMPT_MAX = 1024
# Rodin takes several views of one object; a folder sends the images in it.
RODIN_MAX_IMAGES = 5
RODIN_FOLDER_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")

POLL_INTERVAL_S = 5.0
# A whole generate_3d call, submit included, stays under this. MCP clients on
# the TypeScript SDK cut tool calls off at 60 s by default, and a reply that
# never arrives takes its job handle with it while the generation is charged.
MAX_CALL_S = 45.0
# Unfinished jobs older than this are dropped from the pending file: the
# Premium sweeper fails jobs after 60 minutes, and provider results expire.
PENDING_MAX_AGE_S = 2 * 3600

TRIPO_UNAVAILABLE = ("Tripo is not enabled. In the MCP for Blender sidebar in Blender (press N in the 3D Viewport), "
                     "tick Tripo under AI Model Generation and paste a Tripo API key, or use MCP for Blender Premium: "
                     f"{PREMIUM_URL}?ref=mcp-tripo. If Premium is on, update the Blender addon: run "
                     "`uvx mcp-for-blender install-addon`, then restart Blender.")
MESHY_UNAVAILABLE = ("Meshy is not enabled. In the MCP for Blender sidebar in Blender (press N in the 3D Viewport), "
                     "tick Meshy under AI Model Generation and paste a Meshy API key (needs a paid Meshy plan).")
MESHY_PREMIUM_NEEDS_UPDATE = ("Meshy through MCP for Blender Premium needs the latest Blender addon: run "
                              "`uvx mcp-for-blender install-addon`, then restart Blender and tick Meshy in the "
                              "MCP for Blender sidebar.")
MESHY_POSE_NEEDS_UPDATE = ("pose_mode needs the latest Blender addon: run `uvx mcp-for-blender install-addon`, "
                           "then restart Blender.")
MESHY_POSE_NOT_IN_PREMIUM = ("pose_mode isn't available through MCP for Blender Premium yet, only on your own Meshy "
                             "API key. Generate without pose_mode, or switch Generation Source to Your own API keys "
                             "in the addon preferences.")

Send = Callable[[str, dict], Any]
Progress = Callable[[float, float], Awaitable[None]]


class GenerationError(Exception):
    """A failure to relay to the user as written, without retrying."""


class Unsupported(GenerationError):
    """The addon can't do this at all, so resuming the job won't help."""


@dataclass
class Job:
    provider: str
    kind: str   # tripo: "rid" | "task"; hyper3d: "fal" | "main"; hunyuan3d: "job"; meshy: one of MESHY_KINDS;
                # worldlabs: one of WORLDLABS_KINDS
    ident: str  # request id, job id, task id, or "<task_uuid>|<subscription_key>"
    name: str

    @property
    def handle(self) -> str:
        return f"{self.provider}:{self.kind}:{self.ident}"

    @classmethod
    def parse(cls, handle: str, name: str) -> "Job":
        provider, kind, ident = (handle.split(":", 2) + ["", ""])[:3]
        if (provider not in PROVIDERS + WORLD_PROVIDERS or not kind or not ident
                or (provider == "meshy" and kind not in MESHY_KINDS)
                or (provider == "worldlabs" and kind not in WORLDLABS_KINDS)):
            raise GenerationError(f"Not a generation job handle: {handle!r}")
        return cls(provider, kind, ident, name)

    @property
    def is_world(self) -> bool:
        return self.provider in WORLD_PROVIDERS

    @property
    def resume_hint(self) -> str:
        tool, thing = ("generate_gaussian_world", "world") if self.is_world else ("generate_3d", "model")
        return (f'Call {tool}(job="{self.handle}", name="{self.name}") to keep waiting; it imports the '
                f"{thing} when it's ready. Don't start a new generation.")


def request_key(provider: str, prompt: str | None, image: str | None,
                quality: str | None, bbox_condition: list | None, pose_mode: str | None = None) -> str:
    """Identifies a request, so a retry of one still running can be recognised."""
    fields = [provider, prompt, image, quality, bbox_condition]
    # Only when set, so requests without a pose keep the keys they had.
    if pose_mode:
        fields.append(pose_mode)
    raw = json.dumps(fields)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Pending:
    """Generations submitted but not yet imported or failed, kept on disk.

    A client timeout or a restart loses the job handle from the conversation,
    and the usual next step is the same request again, which pays twice. With
    the job written down here, that retry resumes it instead. Never raises:
    a missing or unreadable file just means nothing is pending.

    Besides those request entries, refine records say which refine a Meshy
    preview already has. They have no "key" or "handle", so find/add/remove
    never touch them and they outlive the import: a preview handle resumed
    afterwards still finds its refine instead of paying for another.
    """

    def __init__(self, path: Path | None, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock

    def _load(self) -> list[dict]:
        if not self.path:
            return []
        try:
            entries = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(entries, list):
            return []
        oldest = self.clock() - PENDING_MAX_AGE_S
        return [e for e in entries if isinstance(e, dict) and e.get("started_at", 0) >= oldest]

    def _save(self, entries: list[dict]) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(entries), encoding="utf-8")
        except Exception as e:
            logger.debug(f"Pending generations write failed: {e}")

    def find(self, key: str) -> Job | None:
        for entry in self._load():
            if entry.get("key") == key:
                try:
                    return Job.parse(entry["handle"], entry.get("name") or "Generated")
                except (GenerationError, KeyError):
                    return None
        return None

    def add(self, key: str, job: Job) -> None:
        entries = [e for e in self._load() if e.get("handle") != job.handle]
        entries.append({"key": key, "handle": job.handle, "name": job.name, "started_at": self.clock()})
        self._save(entries)

    def remove(self, job: Job) -> None:
        entries = self._load()
        kept = [e for e in entries if e.get("handle") != job.handle]
        if len(kept) != len(entries):
            self._save(kept)

    def replace(self, old: Job | str, new: Job) -> None:
        """Point the request entry for `old` at `new`, keeping its key and age."""
        old_handle = old if isinstance(old, str) else old.handle
        entries = self._load()
        matched = [e for e in entries if e.get("handle") == old_handle]
        for entry in matched:
            entry["handle"] = new.handle
        if matched:
            self._save(entries)

    def record_refine(self, preview_id: str, refine_handle: str) -> None:
        entries = [e for e in self._load() if e.get("refine_of") != preview_id]
        entries.append({"refine_of": preview_id, "refine": refine_handle, "started_at": self.clock()})
        self._save(entries)

    def refine_for(self, preview_id: str) -> str | None:
        for entry in self._load():
            if entry.get("refine_of") == preview_id and entry.get("refine"):
                return entry["refine"]
        return None


def _relay(result: Any) -> None:
    """Raise with the provider's own words if the reply is a failure."""
    if not isinstance(result, dict):
        if isinstance(result, str) and result.lower().startswith("error"):
            raise GenerationError(result)
        return
    if result.get("code"):
        raise GenerationError(f"{result.get('message') or result['code']} (code {result['code']}; "
                              "tell the user as written and don't retry automatically)")
    if result.get("error"):
        raise GenerationError(str(result["error"]))


def default_name(prompt: str | None) -> str:
    words = re.findall(r"[A-Za-z0-9]+", prompt or "")[:3]
    return "".join(w.capitalize() for w in words) or "Generated"


def choose_provider(requested: str, premium: list[str], own_key_enabled: dict[str, bool]) -> tuple[str, bool]:
    """(provider, is_premium) for a request. `requested` is a provider or "auto"."""
    requested = (requested or "auto").lower()
    if requested != "auto":
        if requested not in PROVIDERS:
            raise GenerationError(f"Unknown provider {requested!r}. Use one of: auto, {', '.join(PROVIDERS)}")
        if premium:
            if requested not in premium:
                raise GenerationError(f"{requested} is not switched on in MCP for Blender Premium. "
                                      f"On: {', '.join(premium)}. Tick it in the Blender sidebar.")
            return requested, True
        if not own_key_enabled.get(requested):
            if requested == "meshy":
                raise GenerationError(MESHY_UNAVAILABLE)
            if requested == "tripo":
                raise GenerationError(TRIPO_UNAVAILABLE)
            raise GenerationError(f"{requested} is not enabled. Turn it on and add an API key in the "
                                  "MCP for Blender sidebar in Blender (press N in the 3D Viewport).")
        return requested, False
    for name in PREMIUM_ORDER:
        if name in premium:
            return name, True
    for name in OWN_KEY_ORDER:
        if own_key_enabled.get(name):
            return name, False
    raise GenerationError(
        "No 3D generator is enabled. In Blender's MCP for Blender sidebar, turn on Hunyuan3D, Hyper3D "
        "Rodin, Tripo or Meshy with an API key, or use MCP for Blender Premium (no keys needed): "
        f"{PREMIUM_URL}?ref=mcp-no-generator"
    )


def is_image_bytes(data: bytes) -> bool:
    """PNG, JPEG or WebP, by their magic numbers rather than the file name."""
    return (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


def _rodin_images(image: str) -> list:
    if urlparse(image).scheme in ("http", "https"):
        return [image]
    if not os.path.exists(image):
        raise GenerationError(f"Image not found: {image}. Give an absolute file path or an http(s) URL.")
    paths = [Path(image)]
    if os.path.isdir(image):
        # Sorted, so the first view (the one Premium uses) is predictable.
        paths = sorted(p for p in Path(image).iterdir()
                       if p.is_file() and p.suffix.lower() in RODIN_FOLDER_SUFFIXES)
        if not paths:
            raise GenerationError(f"No PNG, JPEG or WebP images in {image}.")
        if len(paths) > RODIN_MAX_IMAGES:
            raise GenerationError(f"Hyper3D takes up to {RODIN_MAX_IMAGES} images; {image} has {len(paths)}. "
                                  "Put the views of one object in a folder of their own.")
    images = []
    for path in paths:
        data = path.read_bytes()
        # The bytes go to a third-party service, so a path to anything but an
        # image (a key file, a config) must not be sent.
        if not is_image_bytes(data):
            raise GenerationError(f"Not a PNG, JPEG or WebP image: {path}")
        images.append((path.suffix, base64.b64encode(data).decode("ascii")))
    return images


def submit_world(send: Send, name: str, prompt: str | None, image: str | None) -> Job:
    """Start a World Labs world. The addon reads image files and folders itself
    and picks the model: the sidebar's on the user's key, Marble 1.1 through Premium."""
    result = send("create_worldlabs_job", {"text_prompt": prompt, "image": image, "name": name})
    _relay(result)
    if not isinstance(result, dict) or not result.get("job_id") or result.get("kind") not in WORLDLABS_KINDS:
        raise GenerationError(f"World Labs returned no job: {result}")
    return Job("worldlabs", result["kind"], result["job_id"], name)


def process_bbox(bbox: list | None) -> list[int] | None:
    if bbox is None:
        return None
    if len(bbox) != 3 or any(v <= 0 for v in bbox):
        raise GenerationError("bbox_condition must be three positive numbers [length, width, height]")
    if all(isinstance(v, int) for v in bbox):
        return list(bbox)
    return [int(float(v) / max(bbox) * 100) for v in bbox]


def submit(send: Send, provider: str, name: str, prompt: str | None, image: str | None,
           quality: str | None, bbox_condition: list | None, supports_quality: bool,
           pose_mode: str | None = None) -> Job | str:
    """Start a generation. Returns a Job, or a finished message for providers
    that generate synchronously (Hunyuan3D LOCAL_API)."""
    if image and provider != "hyper3d" and os.path.isdir(image):
        raise GenerationError(f"{provider} takes one image file, not a folder. Pass one image, "
                              "or provider='hyper3d' to send the folder's images as views of one object.")
    if provider == "tripo":
        if prompt and len(prompt) > TRIPO_PROMPT_MAX:
            raise GenerationError(f"Tripo prompts are limited to {TRIPO_PROMPT_MAX} characters; "
                                  f"this one is {len(prompt)}. Shorten it.")
        params = {"text_prompt": prompt, "image": image}
        if quality:
            params["quality"] = quality
        result = send("create_tripo_job", params)
        _relay(result)
        if isinstance(result, dict) and result.get("request_id"):
            return Job("tripo", "rid", result["request_id"], name)
        if isinstance(result, dict) and result.get("task_id"):
            return Job("tripo", "task", result["task_id"], name)
        raise GenerationError(f"Tripo returned no job id: {result}")

    if provider == "meshy":
        # No quality or bbox: Meshy sizes the model itself (auto_size).
        if prompt and len(prompt) > MESHY_PROMPT_MAX:
            raise GenerationError(f"Meshy prompts are limited to {MESHY_PROMPT_MAX} characters; "
                                  f"this one is {len(prompt)}. Shorten it.")
        params = {"text_prompt": prompt, "image": image}
        if pose_mode:
            params["pose_mode"] = pose_mode
        result = send("create_meshy_job", params)
        _relay(result)
        started = ("t2p", "i2m", "prm")
        if not isinstance(result, dict) or not result.get("task_id") or result.get("kind") not in started:
            raise GenerationError(f"Meshy returned no task: {result}")
        return Job("meshy", result["kind"], result["task_id"], name)

    if provider == "hunyuan3d":
        params = {"text_prompt": prompt, "image": image}
        if quality and supports_quality:
            params["quality"] = quality
        result = send("create_hunyuan_job", params)
        _relay(result)
        response = result.get("Response", {}) if isinstance(result, dict) else {}
        if response.get("Error"):
            raise GenerationError(str(response["Error"].get("Message") or response["Error"]))
        if "JobId" in response:
            return Job("hunyuan3d", "job", f"job_{response['JobId']}", name)
        if isinstance(result, dict) and result.get("status") == "DONE":
            return "Generated and imported by the local Hunyuan3D server. Find it with get_scene_info."
        raise GenerationError(f"Hunyuan3D returned no job: {result}")

    images = _rodin_images(image) if image else None
    result = send("create_rodin_job", {
        "text_prompt": prompt if not image else None,
        "images": images,
        "bbox_condition": process_bbox(bbox_condition),
    })
    _relay(result)
    if result.get("submit_time") and result.get("uuid"):
        return Job("hyper3d", "main", f"{result['uuid']}|{result['jobs']['subscription_key']}", name)
    if result.get("request_id"):
        return Job("hyper3d", "fal", result["request_id"], name)
    raise GenerationError(f"Hyper3D returned no job: {result}")


def poll(send: Send, job: Job) -> tuple[str, Any]:
    """("running" | "done" | "failed", detail) for one status check, or
    ("refine", None) when a Meshy preview is ready for its refine."""
    if job.provider == "worldlabs":
        result = send("poll_worldlabs_job", {"job_id": job.ident, "kind": job.kind})
        status = str(result.get("status", "")).upper() if isinstance(result, dict) else ""
        # A failed world also carries "error"; read the status first so it
        # counts as World Labs' verdict, not as a lost connection.
        if status == "FAILED":
            return "failed", result.get("error") or status
        _relay(result)
        if status == "SUCCEEDED":
            return "done", None
        progress = result.get("progress") if isinstance(result, dict) else None
        return "running", str(progress or status or "IN_PROGRESS")

    if job.provider == "meshy":
        result = send("poll_meshy_job_status", {"task_id": job.ident, "kind": job.kind})
        status = str(result.get("status", "")).upper() if isinstance(result, dict) else ""
        # A failed task also carries "error"; read the status first so it counts
        # as Meshy's verdict, not as a lost connection.
        if status in ("FAILED", "CANCELED"):
            return "failed", result.get("error") or status
        _relay(result)
        if status == "SUCCEEDED":
            if job.kind == "t2p":
                return "refine", None
            if job.kind == "prm":
                # The addon imports a Premium job by its id.
                return "done", None
            if not result.get("model_url"):
                return "failed", "finished without a model URL"
            return "done", result["model_url"]
        parts = [status or "PENDING"]
        if job.kind in ("t2p", "t2r"):
            parts.append("texturing" if job.kind == "t2r" else "modelling")
        if isinstance(result, dict) and result.get("progress") is not None:
            parts.append(f"{result['progress']}%")
        return "running", " ".join(parts)

    if job.provider == "tripo" and job.kind == "task":
        result = send("poll_tripo_job_status", {"task_id": job.ident})
        status = str(result.get("status", "")).lower() if isinstance(result, dict) else ""
        # A failed task also carries "error"; read the status first so it counts
        # as Tripo's verdict, not as a lost connection.
        if status in ("failed", "cancelled", "banned", "expired"):
            return "failed", result.get("error") or status
        if status == "success":
            if not result.get("model_url"):
                return "failed", result.get("error") or "finished without a model URL"
            _relay(result)
            return "done", result["model_url"]
        _relay(result)
        parts = [status or "queued"]
        if isinstance(result, dict) and result.get("progress") is not None:
            parts.append(f"{result['progress']}%")
        return "running", " ".join(parts)

    if job.provider == "tripo" or (job.provider == "hyper3d" and job.kind == "fal"):
        command = "poll_tripo_job_status" if job.provider == "tripo" else "poll_rodin_job_status"
        result = send(command, {"request_id": job.ident})
        status = str(result.get("status", "")).upper() if isinstance(result, dict) else ""
        # A failed job also carries "error"; read the status first so it counts
        # as the provider's verdict, not as a lost connection.
        if status not in ("COMPLETED", "IN_QUEUE", "IN_PROGRESS", ""):
            return "failed", result.get("error") or status
        _relay(result)
        if status == "COMPLETED":
            return "done", None
        return "running", status or "IN_QUEUE"

    if job.provider == "hyper3d":
        result = send("poll_rodin_job_status", {"subscription_key": job.ident.split("|", 1)[1]})
        _relay(result)
        statuses = result.get("status_list") or []
        if statuses and all(s == "Done" for s in statuses):
            return "done", None
        if any(s in ("Failed", "Canceled") for s in statuses):
            return "failed", ", ".join(statuses)
        return "running", ", ".join(statuses) or "Waiting"

    result = send("poll_hunyuan_job_status", {"job_id": job.ident})
    _relay(result)
    response = result.get("Response", {}) if isinstance(result, dict) else {}
    status = response.get("Status", "")
    if status == "DONE":
        files = response.get("ResultFile3Ds") or []
        glb = next((f.get("Url") for f in files if str(f.get("Type", "")).upper() == "GLB"), None)
        url = glb or next((f.get("Url") for f in files if f.get("Url")), None)
        if not url:
            return "failed", "finished without a model file"
        return "done", url
    if status == "FAIL" or response.get("Error"):
        return "failed", response.get("ErrorMessage") or response.get("Error") or "generation failed"
    return "running", status or "WAIT"


def import_result(send: Send, job: Job, detail: Any, options: dict | None = None) -> Any:
    if job.provider == "worldlabs":
        result = send("import_worldlabs_world", {"name": job.name, "job_id": job.ident, "kind": job.kind,
                                                 **(options or {})})
    elif job.provider == "meshy" and job.kind == "prm":
        result = send("import_generated_asset_meshy", {"name": job.name, "task_id": job.ident})
    elif job.provider == "meshy":
        result = send("import_generated_asset_meshy", {"name": job.name, "model_url": detail})
    elif job.provider == "tripo" and job.kind == "task":
        result = send("import_generated_asset_tripo", {"name": job.name, "model_url": detail})
    elif job.provider == "tripo":
        result = send("import_generated_asset_tripo", {"request_id": job.ident, "name": job.name})
    elif job.provider == "hyper3d" and job.kind == "fal":
        result = send("import_generated_asset", {"request_id": job.ident, "name": job.name})
    elif job.provider == "hyper3d":
        result = send("import_generated_asset", {"task_uuid": job.ident.split("|", 1)[0], "name": job.name})
    else:
        result = send("import_generated_asset_hunyuan", {"name": job.name, "zip_file_url": detail})
    _relay(result)
    if isinstance(result, dict) and result.get("succeed") is False:
        raise GenerationError(result.get("error") or result.get("message") or "Import failed")
    return result


def _start_refine(send: Send, job: Job, pending: Pending | None) -> None:
    """Move a finished Meshy preview on to its refine, in place, so every hint
    from here on names the refine. At most one refine per preview: one already
    recorded is reused, and the addon also dedupes, for a refine whose reply
    was lost before it could be recorded here. No await in here, so two calls
    in this server can't interleave."""
    preview_id = job.ident
    refine = pending.refine_for(preview_id) if pending else None
    if refine:
        refine_job = Job.parse(refine, job.name)
    else:
        result = send("refine_meshy_job", {"preview_task_id": preview_id})
        _relay(result)
        if not isinstance(result, dict) or not result.get("task_id"):
            raise GenerationError(f"Meshy returned no refine task: {result}")
        refine_job = Job("meshy", "t2r", result["task_id"], job.name)
        if pending:
            pending.record_refine(preview_id, refine_job.handle)
    if pending:
        pending.replace(job, refine_job)
    job.kind, job.ident = refine_job.kind, refine_job.ident


async def wait_and_import(send: Send, job: Job, wait_seconds: float,
                          progress: Progress | None = None,
                          sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                          clock: Callable[[], float] = time.monotonic,
                          pending: Pending | None = None,
                          import_options: dict | None = None) -> tuple[bool, Any]:
    """Poll until done or `wait_seconds` pass. (imported, the import's reply
    or the latest status). `import_options` go to the import command.

    Once a job is submitted, any error short of the provider saying it failed
    carries the job handle, since the generation may still finish and is paid.
    """
    start = clock()
    while True:
        try:
            state, detail = poll(send, job)
            if state == "refine":
                _start_refine(send, job, pending)
                continue
            if state == "done":
                imported = import_result(send, job, detail, import_options)
        except Unsupported:
            raise
        except Exception as e:
            raise GenerationError(f"{e}. The generation may still be running or finished. {job.resume_hint}") from e
        if state == "done":
            if pending:
                pending.remove(job)
            return True, imported if job.is_world else job.name
        if state == "failed":
            if pending:
                pending.remove(job)
            raise GenerationError(f"Generation failed: {str(detail).rstrip('.')}. This attempt was not imported.")
        elapsed = clock() - start
        if elapsed + POLL_INTERVAL_S > wait_seconds:
            return False, str(detail)
        if progress:
            try:
                await progress(elapsed, wait_seconds)
            except Exception:
                pass
        await sleep(POLL_INTERVAL_S)
