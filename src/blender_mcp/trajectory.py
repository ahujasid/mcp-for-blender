"""
Minimal Dataset B trajectory capture for Blender MCP.

Persists Intent → State → Action → State′ → Observation → Feedback
to Supabase only (no local/JSONL storage). Reuses telemetry config + consent.
"""

from __future__ import annotations

import contextlib
import json
import logging
import platform
import queue
import threading
import time
import uuid
from collections import deque
from typing import Any

import httpx

from .telemetry import MCP_VERSION, get_telemetry

logger = logging.getLogger("blender-mcp-trajectory")

# Fallbacks if TelemetryConfig lacks the fields.
TRAJECTORY_STEPS_TABLE = "trajectory_steps"
TRAJECTORY_FEEDBACK_TABLE = "trajectory_feedback"
# 3: object entries carry aabb_min/aabb_max/dimensions and parent/constraint
# relations. Rows at <=2 have positions but no geometry, so contact- and
# collision-style analysis must filter on this.
# 4: object cap raised from 50 to MAX_SNAPSHOT_OBJECTS, and snapshots carry an
# explicit objects_truncated/objects_listed pair. At <=3 truncation could only
# be inferred from object_count > 50, and the objects kept were whatever
# scene.objects happened to yield first.
# 5: rows carry task_id/client/rows_attempted, each observation is stored whole
# once on its own OBSERVE row (observation.payload, 20k cap) while the rolling
# buffer keeps 2k summaries, episode_end rows close each task with a final
# snapshot and render, human operator batches carry before/after state on their
# last row, and snapshots include mesh counts, material/world fingerprints and
# project_id.
# 6: snapshot `selected` capped at MAX_SNAPSHOT_SELECTED (name-sorted, with
# selected_count/selected_truncated), and every row passes a byte-accurate
# pre-insert guard mirroring the trajectory_steps_size_guard DB constraint —
# a field that cannot be trimmed under its cap is replaced by a
# {"size_guard_dropped": true, "original_bytes": N} stub instead of the row
# being rejected.
# 7: capture limits raised to keep whole scenes rather than truncating them.
# At <=6 SNAPSHOT_BYTE_BUDGET (250k) cut in well before MAX_SNAPSHOT_OBJECTS
# (2000) — object entries run 400-1000 bytes, so snapshots truncated at
# roughly 570 objects and the object cap was never the binding limit. The
# budget now sits above the worst-case cost of a full object list, so
# objects_truncated means the scene really did exceed the object cap.
SCHEMA_VERSION = 7
MAX_RAW_CODE_LENGTH = 40000
MAX_AGENT_OBS_BUFFER = 16
MAX_OBS_SUMMARY_CHARS = 8000
MAX_OBS_PAYLOAD_CHARS = 200000
MAX_PENDING_ROWS = 256
IDLE_EPISODE_TIMEOUT = 600.0

# Byte budgets matched to the trajectory_steps_size_guard DB constraint
# (raw JSON length overestimates pg_column_size, so staying under these keeps
# every row insertable). Snapshots that exceed the budget drop trailing
# objects and set objects_truncated, exactly like the object-count cap.
#
# Sized so the item caps below, not this budget, are what truncate a snapshot:
# a worst-case object entry (long names, 8 modifiers, 8 constraints) is ~1KB,
# so MAX_SNAPSHOT_OBJECTS entries plus MAX_SNAPSHOT_SELECTED names cost ~4.2MB.
# Rows this large are pathological, not typical -- a 600-object scene is ~270KB
# raw -- and jsonb TOAST compresses these highly repetitive payloads by ~100x,
# so the on-disk cost of the headroom is small.
SNAPSHOT_BYTE_BUDGET = 4_500_000
OBSERVATION_BYTE_BUDGET = 400_000

# Per-snapshot object cap. Two snapshots ride on every step row, and object
# entries run ~400 bytes each, so this bounds a row at roughly 1.5 MB. The old
# value of 50 silently truncated any real production scene, which made
# state_delta report phantom adds/removes: scene.objects iterates in an order
# that shifts as objects are created, so before/after kept *different* arbitrary
# 50-object subsets. Snapshots that hit the cap now sort by name first, so both
# sides keep the same subset and the delta stays meaningful.
MAX_SNAPSHOT_OBJECTS = 4000

# Selected-name cap. `selected` rides on both snapshots of every step and,
# unlike `objects`, was unbounded: select-all in a scene big enough to need
# MAX_SNAPSHOT_OBJECTS puts thousands of names in it, and _fit_snapshot could
# not shrink it — the one leak the byte budget missed. Name-sorted so
# before/after keep the same subset, like objects.
MAX_SNAPSHOT_SELECTED = 1000

# Auto-capture: VLM-judged metrics need an image for the step being judged, but
# the agent only screenshots when it chooses to, so most mutating steps have
# none. Render a small offscreen frame ourselves — sampled, not every step, so
# the added latency stays off the common path.
AUTO_CAPTURE_MAX_SIZE = 512
AUTO_CAPTURE_MIN_INTERVAL = 20.0

# Fallback probe for older addons lacking get_world_state_snapshot.
_SNAPSHOT_VIA_EXECUTE_CODE_TEMPLATE = r'''
import json
import bpy
import mathutils

scene = bpy.context.scene
selected = [obj.name for obj in bpy.context.selected_objects]
selected_count = len(selected)
selected_truncated = selected_count > __MAX_SELECTED__
if selected_truncated:
    # Stable subset so before/after snapshots agree; see MAX_SNAPSHOT_SELECTED.
    selected = sorted(selected)[:__MAX_SELECTED__]
objects = []
all_objects = list(scene.objects)
truncated = len(all_objects) > __MAX_OBJECTS__
if truncated:
    # Stable subset so before/after snapshots agree; see MAX_SNAPSHOT_OBJECTS.
    all_objects = sorted(all_objects, key=lambda o: o.name)[:__MAX_OBJECTS__]
for obj in all_objects:
    materials = []
    if getattr(obj, "material_slots", None):
        materials = [slot.material.name for slot in obj.material_slots if slot.material]
    entry = {
        "name": obj.name,
        "type": obj.type,
        "location": [round(float(obj.location.x), 3), round(float(obj.location.y), 3), round(float(obj.location.z), 3)],
        "rotation": [round(float(obj.rotation_euler.x), 3), round(float(obj.rotation_euler.y), 3), round(float(obj.rotation_euler.z), 3)],
        "scale": [round(float(obj.scale.x), 3), round(float(obj.scale.y), 3), round(float(obj.scale.z), 3)],
        "visible": bool(obj.visible_get()),
        "materials": materials,
    }
    bound_box = getattr(obj, "bound_box", None)
    if bound_box:
        try:
            mw = obj.matrix_world
            pts = [mw @ mathutils.Vector(c) for c in bound_box]
            entry["aabb_min"] = [round(min(p[k] for p in pts), 3) for k in range(3)]
            entry["aabb_max"] = [round(max(p[k] for p in pts), 3) for k in range(3)]
            entry["dimensions"] = [round(float(obj.dimensions.x), 3), round(float(obj.dimensions.y), 3), round(float(obj.dimensions.z), 3)]
        except Exception:
            pass
    if obj.parent:
        entry["parent"] = obj.parent.name
        entry["parent_type"] = obj.parent_type
        loc = obj.matrix_local.translation
        entry["local_location"] = [round(float(loc.x), 3), round(float(loc.y), 3), round(float(loc.z), 3)]
    constraints = []
    for c in (getattr(obj, "constraints", None) or [])[:8]:
        centry = {"type": c.type}
        target = getattr(c, "target", None)
        if target:
            centry["target"] = target.name
        constraints.append(centry)
    if constraints:
        entry["constraints"] = constraints
    modifiers = [m.type for m in (getattr(obj, "modifiers", None) or [])[:8]]
    if modifiers:
        entry["modifiers"] = modifiers
    objects.append(entry)

camera = scene.camera
camera_info = None
if camera:
    camera_info = {
        "name": camera.name,
        "location": [round(float(camera.location.x), 3), round(float(camera.location.y), 3), round(float(camera.location.z), 3)],
        "rotation": [round(float(camera.rotation_euler.x), 3), round(float(camera.rotation_euler.y), 3), round(float(camera.rotation_euler.z), 3)],
    }
    if camera.type == "CAMERA" and camera.data:
        camera_info["lens"] = round(float(camera.data.lens), 3)
        camera_info["sensor_width"] = round(float(camera.data.sensor_width), 3)

lights = []
for obj in scene.objects:
    if obj.type != "LIGHT":
        continue
    light_entry = {
        "name": obj.name,
        "location": [round(float(obj.location.x), 3), round(float(obj.location.y), 3), round(float(obj.location.z), 3)],
    }
    if obj.data:
        light_entry["light_type"] = obj.data.type
        light_entry["energy"] = round(float(obj.data.energy), 3)
    lights.append(light_entry)
    if len(lights) >= 20:
        break

print(json.dumps({
    "name": scene.name,
    "object_count": len(scene.objects),
    "objects_listed": len(objects),
    "objects_truncated": truncated,
    "selected": selected,
    "selected_count": selected_count,
    "selected_truncated": selected_truncated,
    "objects": objects,
    "active_camera": camera.name if camera else None,
    "camera": camera_info,
    "lights": lights,
    "materials_count": len(bpy.data.materials),
    "blender_version": bpy.app.version_string,
    "snapshot_source": "execute_code_fallback",
}))
'''

_SNAPSHOT_VIA_EXECUTE_CODE = _SNAPSHOT_VIA_EXECUTE_CODE_TEMPLATE.replace(
    "__MAX_OBJECTS__", str(MAX_SNAPSHOT_OBJECTS)
).replace("__MAX_SELECTED__", str(MAX_SNAPSHOT_SELECTED))


SEMANTIC_ACTIONS: dict[str, str] = {
    "execute_blender_code": "EXECUTE_CODE",
    "download_polyhaven_asset": "DOWNLOAD_ASSET",
    "set_texture": "SET_TEXTURE",
    "download_sketchfab_model": "DOWNLOAD_MODEL",
    "generate_hyper3d_model_via_text": "GENERATE_3D",
    "generate_hyper3d_model_via_images": "GENERATE_3D",
    "import_generated_asset": "IMPORT_ASSET",
    "generate_hunyuan3d_model": "GENERATE_3D",
    "import_generated_asset_hunyuan": "IMPORT_ASSET",
    "get_scene_info": "OBSERVE",
    "get_object_info": "OBSERVE",
    "get_viewport_screenshot": "OBSERVE",
    "look": "OBSERVE",
    "generate_3d": "GENERATE_3D",
    "episode_end": "EPISODE_END",
}


def semantic_action_for_tool(tool_name: str) -> str:
    """Map an MCP tool name to a coarse semantic action label."""
    return SEMANTIC_ACTIONS.get(tool_name, "UNKNOWN")


# Human operators mapped to the same vocabulary as agent actions.
HUMAN_SEMANTIC_ACTIONS: dict[str, str] = {
    "transform.translate": "MOVE",
    "transform.rotate": "ROTATE",
    "transform.resize": "SCALE",
    "object.delete": "DELETE",
    "object.duplicate_move": "DUPLICATE",
    "object.duplicate": "DUPLICATE",
    "mesh.extrude_region_move": "EDIT_MESH",
    "mesh.extrude_region": "EDIT_MESH",
    "mesh.subdivide": "EDIT_MESH",
    "mesh.loopcut_slide": "EDIT_MESH",
    "mesh.bevel": "EDIT_MESH",
    "mesh.inset": "EDIT_MESH",
    "object.modifier_add": "ADD_MODIFIER",
    "object.material_slot_assign": "ASSIGN_MATERIAL",
    "material.new": "ASSIGN_MATERIAL",
    "object.shade_smooth": "MODIFY_MATERIAL",
    "object.shade_flat": "MODIFY_MATERIAL",
    "object.select_all": "SELECT",
    "view3d.select": "SELECT",
}

# Fallback for operators not listed above.
_HUMAN_SEMANTIC_PREFIXES: tuple[tuple[str, str], ...] = (
    ("mesh.", "EDIT_MESH"),
    ("object.modifier", "ADD_MODIFIER"),
    ("material.", "MODIFY_MATERIAL"),
    ("object.light", "ADD_LIGHT"),
    ("transform.", "TRANSFORM"),
    ("object.", "OBJECT_OP"),
)


def semantic_action_for_operator(bl_idname: str) -> str:
    """Map a Blender operator id to a coarse semantic action label."""
    if not bl_idname:
        return "UNKNOWN"
    mapped = HUMAN_SEMANTIC_ACTIONS.get(bl_idname)
    if mapped:
        return mapped
    for prefix, label in _HUMAN_SEMANTIC_PREFIXES:
        if bl_idname.startswith(prefix):
            return label
    return "UNKNOWN"


def compute_state_delta(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compute a compact object-level delta between two world snapshots."""
    before = before or {}
    after = after or {}

    before_objs = {
        obj["name"]: obj
        for obj in before.get("objects") or []
        if isinstance(obj, dict) and "name" in obj
    }
    after_objs = {
        obj["name"]: obj
        for obj in after.get("objects") or []
        if isinstance(obj, dict) and "name" in obj
    }

    objects_added = [name for name in after_objs if name not in before_objs]
    objects_removed = [name for name in before_objs if name not in after_objs]
    objects_changed: list[dict[str, Any]] = []
    for name in before_objs:
        if name in after_objs and before_objs[name] != after_objs[name]:
            objects_changed.append({
                "name": name,
                "before": before_objs[name],
                "after": after_objs[name],
            })

    before_fps = before.get("material_fps") or {}
    after_fps = after.get("material_fps") or {}
    materials_changed = sorted(
        name
        for name in set(before_fps) | set(after_fps)
        if before_fps.get(name) != after_fps.get(name)
    )

    return {
        "objects_added": objects_added,
        "objects_removed": objects_removed,
        "objects_changed": objects_changed,
        "materials_changed": materials_changed,
        "world_changed": before.get("world_fp") != after.get("world_fp"),
        "selection_changed": before.get("selected") != after.get("selected"),
        "camera_changed": before.get("active_camera") != after.get("active_camera"),
        "object_count_before": before.get("object_count"),
        "object_count_after": after.get("object_count"),
    }


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str))
    except Exception:
        return 0


def _fit_snapshot(
    snapshot: dict[str, Any] | None, budget: int = SNAPSHOT_BYTE_BUDGET
) -> dict[str, Any] | None:
    """Shrink a snapshot to the byte budget: cap the selected-name list, then
    drop trailing objects.

    Objects are name-sorted at capture, so before/after snapshots of one step
    keep the same subset and deltas stay meaningful; the selected cap sorts
    for the same reason. Old addons send `selected` uncapped, so this is
    enforced server-side and not just at capture."""
    if not isinstance(snapshot, dict):
        return snapshot
    selected = snapshot.get("selected")
    if isinstance(selected, list) and len(selected) > MAX_SNAPSHOT_SELECTED:
        snapshot = {
            **snapshot,
            "selected": sorted(str(name) for name in selected)[:MAX_SNAPSHOT_SELECTED],
            "selected_count": snapshot.get("selected_count", len(selected)),
            "selected_truncated": True,
        }
    objects = snapshot.get("objects")
    if not objects or _json_size(snapshot) <= budget:
        return snapshot
    objects_budget = budget - (_json_size(snapshot) - _json_size(objects))
    kept: list[Any] = []
    used = 2
    for obj in objects:
        used += _json_size(obj) + 2
        if used > objects_budget:
            break
        kept.append(obj)
    fitted = {
        **snapshot,
        "objects": kept,
        "objects_listed": len(kept),
        "objects_truncated": True,
    }
    while kept and _json_size(fitted) > budget:
        kept.pop()
        fitted["objects_listed"] = len(kept)
    return fitted


# Per-field byte caps mirroring the trajectory_steps_size_guard DB constraint
# (action 262144, observation 524288, state_before/state_after 5242880 bytes).
# Thresholds sit under the DB caps because json.dumps output only approximates
# the jsonb text Postgres measures. The state caps clear SNAPSHOT_BYTE_BUDGET
# so a snapshot fitted upstream is never re-trimmed -- or stubbed -- here.
DB_FIELD_BYTE_CAPS = {
    "action": 250_000,
    "observation": 500_000,
    "state_before": 5_000_000,
    "state_after": 5_000_000,
}


def _utf8_size(value: Any) -> int | None:
    """Byte length of the JSON Postgres will store, or None if unserializable."""
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return None


def _exceeds(value: Any, cap: int) -> bool:
    size = _utf8_size(value)
    return size is None or size > cap


def _trim_observation(observation: dict[str, Any], cap: int) -> dict[str, Any]:
    """Halve the stored-whole payload, then drop it, then drop the oldest
    agent observations, until the field fits."""
    trimmed = dict(observation)
    payload = trimmed.get("payload")
    while isinstance(payload, str) and len(payload) > 512 and _exceeds(trimmed, cap):
        payload = payload[: len(payload) // 2] + "..."
        trimmed["payload"] = payload
    if _exceeds(trimmed, cap):
        trimmed.pop("payload", None)
    agent_obs = trimmed.get("agent_observations")
    while isinstance(agent_obs, list) and agent_obs and _exceeds(trimmed, cap):
        agent_obs = agent_obs[1:]
        trimmed["agent_observations"] = agent_obs
    return trimmed


def _trim_action(action: dict[str, Any], cap: int) -> dict[str, Any]:
    """Cap string params, then drop params wholesale, then shorten raw_code."""
    trimmed = dict(action)
    params = trimmed.get("params")
    if isinstance(params, dict) and _exceeds(trimmed, cap):
        trimmed["params"] = {
            key: _cap_text(value, 500) if isinstance(value, str) else value
            for key, value in params.items()
        }
    if isinstance(params, dict) and _exceeds(trimmed, cap):
        trimmed["params"] = {"params_dropped": True}
    if isinstance(trimmed.get("raw_code"), str) and _exceeds(trimmed, cap):
        trimmed["raw_code"] = _cap_text(trimmed["raw_code"], 2000)
    return trimmed


def _size_guard_stub(value: Any, size: int | None) -> dict[str, Any]:
    stub: dict[str, Any] = {"size_guard_dropped": True, "original_bytes": size}
    if isinstance(value, dict):
        for key in ("kind", "semantic", "tool_name", "snapshot_source", "object_count"):
            kept = value.get(key)
            if isinstance(kept, (str, int, float, bool)):
                stub[key] = kept
    return stub


def _enforce_db_size_guard(payload: dict[str, Any]) -> None:
    """Last defence before insert: no field may violate the DB size guard.

    Upstream budgets (_fit_snapshot, OBSERVATION_BYTE_BUDGET) keep normal rows
    small; this catches whatever they miss, measured in the bytes Postgres
    sees rather than Python characters, so an oversized field is trimmed — or
    replaced with a stub — instead of the whole row being rejected. Mutates
    payload in place. Never raises."""
    sizes: dict[str, int | None] = {}
    for field, cap in DB_FIELD_BYTE_CAPS.items():
        try:
            value = payload.get(field)
            if value is None:
                continue
            size = _utf8_size(value)
            sizes[field] = size
            if size is not None and size <= cap:
                continue
            if field in ("state_before", "state_after") and isinstance(value, dict):
                trimmed = _fit_snapshot(value, budget=min(cap, SNAPSHOT_BYTE_BUDGET))
            elif field == "observation" and isinstance(value, dict):
                trimmed = _trim_observation(value, cap)
            elif field == "action" and isinstance(value, dict):
                trimmed = _trim_action(value, cap)
            else:
                trimmed = None
            if trimmed is None or _exceeds(trimmed, cap):
                trimmed = _size_guard_stub(value, size)
            payload[field] = trimmed
            logger.warning(
                f"Trajectory size guard: {field} was {size} bytes (cap {cap}); "
                f"stored {_utf8_size(trimmed)} bytes"
            )
        except Exception as e:
            logger.debug(f"Size guard failed for {field}: {e}")
            payload[field] = {"size_guard_dropped": True, "original_bytes": None}
    if sizes:
        logger.debug(f"Trajectory row field bytes: {sizes}")


def _normalize_goal(goal: str | None) -> str:
    return (goal or "").strip()


def _cap_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    try:
        limit_i = int(limit)
    except (TypeError, ValueError):
        limit_i = 1000
    if limit_i <= 0:
        return value
    if len(value) <= limit_i:
        return value
    return value[:limit_i] + "..."


def _summarize_agent_payload(payload: Any) -> Any:
    """Keep agent observation summaries small for Supabase rows."""
    if payload is None:
        return None
    if isinstance(payload, str):
        return _cap_text(payload, MAX_OBS_SUMMARY_CHARS)
    if isinstance(payload, dict):
        keys = set(payload.keys())
        is_scene_summary = keys <= {
            "name", "object_count", "objects", "selected", "materials_count",
            "active_camera", "lights", "camera",
        }
        is_object_summary = "name" in payload and "type" in payload
        if is_scene_summary or is_object_summary:
            return payload
        try:
            import json
            return _cap_text(json.dumps(payload, default=str), MAX_OBS_SUMMARY_CHARS)
        except Exception:
            return _cap_text(str(payload), MAX_OBS_SUMMARY_CHARS)
    return _cap_text(str(payload), MAX_OBS_SUMMARY_CHARS)


class TrajectoryRecorder:
    """Consent-gated, best-effort writer for trajectory_steps / trajectory_feedback."""

    def __init__(self):
        self._lock = threading.Lock()
        self._queue: "queue.Queue[tuple[str, dict[str, Any]]]" = queue.Queue(
            maxsize=MAX_PENDING_ROWS
        )
        self._worker = threading.Thread(
            target=self._worker_loop, daemon=True, name="trajectory-writer"
        )
        self._worker.start()
        self._trajectory_id: str = str(uuid.uuid4())
        self._step_index: int = 0
        self._current_goal: str | None = None
        self._task_id: str = uuid.uuid4().hex[:12]
        self._task_step_count: int = 0
        self._human_after_agent: bool = False
        self._last_state_after: dict[str, Any] | None = None
        self._client: dict[str, Any] | None = None
        self._rows_attempted: int = 0
        self._idle_timer: threading.Timer | None = None
        # What the agent requested, not the privileged full state.
        self._agent_obs: deque[dict[str, Any]] = deque(maxlen=MAX_AGENT_OBS_BUFFER)
        self._last_screenshot_ref: str | None = None
        # None = unknown, True = addon has get_world_state_snapshot, False = fallback.
        self._native_snapshot_supported: bool | None = None
        self._last_auto_capture: float = 0.0
        self._auto_capture_supported: bool = True

    def _telemetry(self):
        return get_telemetry()

    def _config(self):
        return self._telemetry().config

    def _steps_table(self) -> str:
        return getattr(self._config(), "trajectory_steps_table", TRAJECTORY_STEPS_TABLE)

    def _feedback_table(self) -> str:
        return getattr(
            self._config(), "trajectory_feedback_table", TRAJECTORY_FEEDBACK_TABLE
        )

    def _can_write(self) -> bool:
        try:
            telemetry = self._telemetry()
            if not telemetry.config.enabled:
                return False
            return bool(telemetry._check_user_consent())
        except Exception as e:
            logger.debug(f"Trajectory consent check failed: {e}")
            return False

    def _snapshot_via_execute_code(self, blender) -> dict[str, Any] | None:
        """Older addons: run snapshot script through execute_code and parse JSON."""
        import json

        result = blender.send_command("execute_code", {"code": _SNAPSHOT_VIA_EXECUTE_CODE})
        raw = result.get("result", "") if isinstance(result, dict) else ""
        if not isinstance(raw, str) or not raw.strip():
            logger.debug("execute_code snapshot returned empty output")
            return None
        # Addon may append other stdout; take the last JSON object line.
        for line in reversed(raw.strip().splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and "objects" in parsed:
                return parsed
        logger.debug("Could not parse execute_code snapshot JSON")
        return None

    def _snapshot_via_scene_info(self, blender) -> dict[str, Any] | None:
        """Last-resort compact snapshot using get_scene_info (very old addons)."""
        result = blender.send_command("get_scene_info")
        if not isinstance(result, dict) or "error" in result:
            return None
        objects = result.get("objects") or []
        total = result.get("object_count", len(objects))
        return {
            "name": result.get("name"),
            "object_count": total,
            # get_scene_info caps its own object list well below our snapshot
            # cap, so this path is truncated whenever the scene outgrows it.
            "objects_listed": len(objects),
            "objects_truncated": bool(
                isinstance(total, int) and total > len(objects)
            ),
            "selected": [],
            "objects": objects,
            "active_camera": None,
            "camera": None,
            "lights": [],
            "materials_count": result.get("materials_count"),
            "blender_version": None,
            "snapshot_source": "get_scene_info_fallback",
        }

    def snapshot_world_state(self) -> dict[str, Any] | None:
        """Fetch a compact world snapshot from Blender, fitted to the row size
        budget. Never raises.

        Prefer native addon handler; if the installed addon is older and lacks
        get_world_state_snapshot (common when users only update the MCP server),
        fall back to execute_code, then get_scene_info.
        """
        return _fit_snapshot(self._snapshot_world_state())

    def _snapshot_world_state(self) -> dict[str, Any] | None:
        try:
            from .server import get_blender_connection

            blender = get_blender_connection()

            use_native = self._native_snapshot_supported is not False
            if use_native:
                try:
                    result = blender.send_command("get_world_state_snapshot")
                    if isinstance(result, dict) and "error" not in result:
                        self._native_snapshot_supported = True
                        if "snapshot_source" not in result:
                            result = {**result, "snapshot_source": "native"}
                        return result
                    logger.debug(f"World snapshot error payload: {result}")
                except Exception as e:
                    msg = str(e).lower()
                    if "unknown command" in msg or "get_world_state_snapshot" in msg:
                        self._native_snapshot_supported = False
                        logger.debug(
                            "Addon lacks get_world_state_snapshot; "
                            "using execute_code fallback for existing installs"
                        )
                    else:
                        logger.debug(f"Native world snapshot failed: {e}")

            if self._native_snapshot_supported is False or not use_native:
                try:
                    parsed = self._snapshot_via_execute_code(blender)
                    if parsed is not None:
                        return parsed
                except Exception as e:
                    logger.debug(f"execute_code snapshot fallback failed: {e}")

                try:
                    return self._snapshot_via_scene_info(blender)
                except Exception as e:
                    logger.debug(f"get_scene_info snapshot fallback failed: {e}")
                    return None

            try:
                parsed = self._snapshot_via_execute_code(blender)
                if parsed is not None:
                    return parsed
            except Exception as e:
                logger.debug(f"execute_code snapshot fallback failed: {e}")
            try:
                return self._snapshot_via_scene_info(blender)
            except Exception as e:
                logger.debug(f"get_scene_info snapshot fallback failed: {e}")
                return None
        except Exception as e:
            logger.debug(f"Failed to snapshot world state: {e}")
            return None

    def _should_auto_capture(self, changed: bool) -> bool:
        """Rate-limit auto-capture. Caller holds no lock."""
        if not changed or not self._auto_capture_supported:
            return False
        now = time.time()
        with self._lock:
            if now - self._last_auto_capture < AUTO_CAPTURE_MIN_INTERVAL:
                return False
            self._last_auto_capture = now
        return True

    def _render_frame(self, prefix: str) -> str | None:
        """Render + upload one offscreen viewport frame. Never raises."""
        try:
            import os
            import tempfile

            from .server import get_blender_connection

            blender = get_blender_connection()
            temp_path = os.path.join(
                tempfile.gettempdir(),
                f"blender_mcp_{prefix}_{os.getpid()}_{uuid.uuid4().hex[:8]}.png",
            )
            try:
                result = blender.send_command(
                    "get_viewport_screenshot",
                    {
                        "max_size": AUTO_CAPTURE_MAX_SIZE,
                        "filepath": temp_path,
                        "format": "png",
                    },
                )
                if not isinstance(result, dict) or "error" in result:
                    logger.debug(f"Frame capture declined: {result}")
                    return None
                if not os.path.exists(temp_path):
                    return None
                with open(temp_path, "rb") as handle:
                    image_bytes = handle.read()
            finally:
                with contextlib.suppress(Exception):
                    os.remove(temp_path)

            if not image_bytes:
                return None
            ref = self._telemetry().upload_screenshot(image_bytes, prefix)
            if not ref:
                return None
            with self._lock:
                self._last_screenshot_ref = ref
            return ref
        except Exception as e:
            msg = str(e).lower()
            # No GPU/viewport in this install: stop retrying for the session.
            if "no 3d viewport" in msg or "unknown command" in msg:
                self._auto_capture_supported = False
            logger.debug(f"Frame capture failed: {e}")
            return None

    def maybe_auto_capture(self, state_delta: dict[str, Any] | None) -> str | None:
        """Render + upload a frame for a step the agent did not screenshot.

        Only fires when the step actually changed the scene and the rate limit
        allows, so an unattended session does not upload a frame per call.
        Returns a storage ref, or None. Never raises.
        """
        try:
            if not self._can_write():
                return None
            delta = state_delta or {}
            changed = bool(
                delta.get("objects_added")
                or delta.get("objects_removed")
                or delta.get("objects_changed")
                or delta.get("materials_changed")
                or delta.get("world_changed")
            )
            if not self._should_auto_capture(changed):
                return None
            return self._render_frame("auto")
        except Exception as e:
            logger.debug(f"Auto-capture failed: {e}")
            return None

    def note_client(self, name: str | None, version: str | None = None) -> None:
        """Record which MCP client (and thus which agent) drives this session."""
        if self._client is not None or not name:
            return
        with self._lock:
            if self._client is None:
                self._client = {"name": name, "version": version}

    def note_goal(self, goal_text: str | None) -> None:
        """Close the running episode when the verbatim goal changes.

        Called before a tool executes, while the scene still shows the previous
        task's end state. Never raises.
        """
        try:
            normalized = _normalize_goal(goal_text)
            if not normalized:
                return
            with self._lock:
                current = self._current_goal
                steps = self._task_step_count
            if (
                steps
                and current
                and normalized != current
                and len(normalized) >= 12
                and self._can_write()
            ):
                self.close_episode("goal_change")
        except Exception as e:
            logger.debug(f"Failed to note goal: {e}")

    def close_episode(self, reason: str) -> bool:
        """Write an episode_end row with a final snapshot and render, then
        rotate task_id. Fires on goal change, idle timeout and shutdown.
        Never raises."""
        try:
            if not self._can_write():
                return False
            with self._lock:
                if not self._task_step_count:
                    return False
                if self._idle_timer:
                    self._idle_timer.cancel()
                    self._idle_timer = None
            self.drain_human_activity()
            state = self.snapshot_world_state()
            ref = self._render_frame("episode") if self._auto_capture_supported else None
            with self._lock:
                trajectory_id = self._trajectory_id
                task_id = self._task_id
                goal = self._current_goal
                human_after = self._human_after_agent
                step_index = self._step_index
                self._step_index += 1
                agent_obs = list(self._agent_obs)
                if state:
                    self._last_state_after = state
                self._task_id = uuid.uuid4().hex[:12]
                self._task_step_count = 0
                self._human_after_agent = False
            payload = self.build_step_payload(
                tool_name="episode_end",
                goal_text=goal,
                params={"reason": reason},
                raw_code=None,
                state_before=None,
                state_after=state,
                success=True,
                error=None,
                duration_ms=None,
                screenshot_ref=ref,
                trajectory_id=trajectory_id,
                step_index=step_index,
                agent_observations=agent_obs,
                observation_kind="episode_end",
                goal_source="session" if goal else "none",
                screenshot_source="episode_end" if ref else None,
                task_id=task_id,
            )
            payload["observation"]["human_steps_after_last_agent"] = human_after
            return self._enqueue_row(self._steps_table(), payload)
        except Exception as e:
            logger.debug(f"Failed to close episode: {e}")
            return False

    def _reset_idle_timer(self) -> None:
        with self._lock:
            if self._idle_timer:
                self._idle_timer.cancel()
            self._idle_timer = threading.Timer(
                IDLE_EPISODE_TIMEOUT, self.close_episode, args=("idle",)
            )
            self._idle_timer.daemon = True
            self._idle_timer.start()

    def note_agent_observation(
        self,
        *,
        modality: str,
        tool_name: str,
        summary: Any = None,
        screenshot_ref: str | None = None,
    ) -> None:
        """Record what the agent actually observed (consent-gated buffer). Never raises."""
        try:
            if not self._can_write():
                return
            entry = {
                "modality": modality,
                "tool_name": tool_name,
                "summary": _summarize_agent_payload(summary),
                "screenshot_ref": screenshot_ref,
                "timestamp": time.time(),
            }
            with self._lock:
                self._agent_obs.append(entry)
                if screenshot_ref:
                    self._last_screenshot_ref = screenshot_ref
        except Exception as e:
            logger.debug(f"Failed to note agent observation: {e}")

    def _agent_obs_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._agent_obs)

    def _begin_step_locked(
        self, goal_text: str | None
    ) -> tuple[str, int, str | None, str, str]:
        """Advance step_index under lock.

        Returns (trajectory_id, index, resolved_goal, goal_source, task_id).

        Trajectory id is session-scoped (one per MCP process recorder). We do
        NOT reset step_index when user_prompt/goal text changes — agents pass a
        different prompt nearly every tool call, which previously forced every
        row to step_index=0.

        Most mutating tools declare `user_prompt: str = ""`, so an agent that
        omits it would otherwise write the action rows — the ones carrying
        raw_code and state deltas — with no intent at all. When this call
        supplies no goal we backfill the one tracked earlier in the session and
        mark the row 'session' so verbatim prompts stay distinguishable from
        inherited ones.
        """
        normalized = _normalize_goal(goal_text)
        if normalized:
            if not self._current_goal:
                self._current_goal = normalized
            elif len(normalized) >= 12:
                self._current_goal = normalized

        if normalized:
            resolved_goal: str | None = normalized
            goal_source = "call"
        elif self._current_goal:
            resolved_goal = self._current_goal
            goal_source = "session"
        else:
            resolved_goal = None
            goal_source = "none"

        trajectory_id = self._trajectory_id
        step_index = self._step_index
        self._step_index += 1
        self._task_step_count += 1
        return trajectory_id, step_index, resolved_goal, goal_source, self._task_id

    def build_step_payload(
        self,
        *,
        tool_name: str,
        goal_text: str | None,
        params: dict[str, Any] | None,
        raw_code: str | None,
        state_before: dict[str, Any] | None,
        state_after: dict[str, Any] | None,
        success: bool,
        error: str | None,
        duration_ms: float | None,
        screenshot_ref: str | None = None,
        blender_version: str | None = None,
        trajectory_id: str | None = None,
        step_index: int | None = None,
        customer_uuid: str | None = None,
        session_id: str | None = None,
        agent_observations: list[dict[str, Any]] | None = None,
        observation_kind: str = "action",
        goal_source: str | None = None,
        screenshot_source: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Build a trajectory_steps row dict (pure; no I/O)."""
        telemetry = self._telemetry()
        agent_obs = (
            agent_observations
            if agent_observations is not None
            else self._agent_obs_snapshot()
        )
        resolved_screenshot = screenshot_ref
        if resolved_screenshot is None:
            with self._lock:
                resolved_screenshot = self._last_screenshot_ref

        modalities: list[str] = []
        for obs in agent_obs:
            mod = obs.get("modality")
            if mod and mod not in modalities:
                modalities.append(mod)
        if resolved_screenshot and "screenshot" not in modalities:
            modalities.append("screenshot")
        if "privileged_state" not in modalities and (
            state_before is not None or state_after is not None
        ):
            modalities.append("privileged_state")

        while agent_obs and _json_size(agent_obs) > OBSERVATION_BYTE_BUDGET:
            agent_obs = agent_obs[1:]

        max_goal = getattr(self._config(), "max_prompt_length", 1000)
        if not isinstance(max_goal, int):
            max_goal = 1000
        if goal_source is None:
            if _normalize_goal(goal_text):
                goal_source = "call"
            else:
                with self._lock:
                    session_goal = self._current_goal
                if session_goal:
                    goal_text = session_goal
                    goal_source = "session"
                else:
                    goal_source = "none"
        capped_goal = _cap_text(goal_text, max_goal) if goal_text else goal_text

        if not blender_version:
            for snap in (state_after, state_before):
                if isinstance(snap, dict) and snap.get("blender_version"):
                    blender_version = snap.get("blender_version")
                    break

        return {
            "customer_uuid": customer_uuid or telemetry._customer_uuid,
            "session_id": session_id or telemetry._session_id,
            "trajectory_id": trajectory_id or self._trajectory_id,
            "task_id": task_id or self._task_id,
            "step_index": step_index if step_index is not None else max(0, self._step_index - 1),
            "schema_version": SCHEMA_VERSION,
            "client": self._client,
            # Overwritten to "human" for operators run directly in Blender.
            "actor": "agent",
            "goal_text": capped_goal,
            "goal_source": goal_source,
            "observation": {
                "kind": observation_kind,
                "modalities": modalities,
                "agent_observations": agent_obs,
                "screenshot_ref": resolved_screenshot,
                # Auto-captured frames show this step; agent frames may predate
                # it. Judging code must be able to tell them apart.
                "screenshot_source": (
                    (screenshot_source or "agent_tool") if resolved_screenshot else None
                ),
            },
            "action": {
                "semantic": semantic_action_for_tool(tool_name),
                "tool_name": tool_name,
                "params": params or {},
                "raw_code": _cap_text(raw_code, MAX_RAW_CODE_LENGTH),
            },
            "state_before": state_before,
            "state_after": state_after,
            "state_delta": compute_state_delta(state_before, state_after),
            "outcome": {
                "success": success,
                "error": error,
                "duration_ms": duration_ms,
            },
            "version": MCP_VERSION,
            "platform": platform.system().lower(),
            "blender_version": blender_version,
            "event_timestamp": int(time.time()),
        }

    def record_step(
        self,
        *,
        tool_name: str,
        goal_text: str | None = None,
        params: dict[str, Any] | None = None,
        raw_code: str | None = None,
        state_before: dict[str, Any] | None = None,
        state_after: dict[str, Any] | None = None,
        success: bool = True,
        error: str | None = None,
        duration_ms: float | None = None,
        screenshot_ref: str | None = None,
        blender_version: str | None = None,
        observation_kind: str = "action",
    ) -> bool:
        """POST one trajectory step. Returns True if sent. Never raises.

        Screenshots are tool-level only (get_viewport_screenshot → OBSERVE).
        Mutate steps may inherit the last agent screenshot_ref; they never capture.
        """
        try:
            if not self._can_write():
                return False

            # Render our own frame for this step before falling back to the
            # agent's last screenshot, which may show a much earlier state.
            auto_ref = self.maybe_auto_capture(
                compute_state_delta(state_before, state_after)
            )
            if screenshot_ref is None:
                if auto_ref:
                    screenshot_ref = auto_ref
                else:
                    with self._lock:
                        screenshot_ref = self._last_screenshot_ref

            with self._lock:
                trajectory_id, step_index, resolved_goal, goal_source, task_id = (
                    self._begin_step_locked(goal_text)
                )
                agent_obs = list(self._agent_obs)
                self._human_after_agent = False
                if state_after:
                    self._last_state_after = state_after

            payload = self.build_step_payload(
                tool_name=tool_name,
                goal_text=resolved_goal,
                params=params,
                raw_code=raw_code,
                state_before=state_before,
                state_after=state_after,
                success=success,
                error=error,
                duration_ms=duration_ms,
                screenshot_ref=screenshot_ref,
                blender_version=blender_version,
                trajectory_id=trajectory_id,
                step_index=step_index,
                agent_observations=agent_obs,
                observation_kind=observation_kind,
                goal_source=goal_source,
                screenshot_source="auto_capture" if auto_ref else None,
                task_id=task_id,
            )
            sent = self._enqueue_row(self._steps_table(), payload)
            self._reset_idle_timer()
            return sent
        except Exception as e:
            logger.debug(f"Failed to record trajectory step: {e}")
            return False

    def record_observe_step(
        self,
        *,
        tool_name: str,
        goal_text: str | None = None,
        modality: str,
        summary: Any = None,
        screenshot_ref: str | None = None,
        success: bool = True,
        error: str | None = None,
        duration_ms: float | None = None,
    ) -> bool:
        """Record an OBSERVE step and update the agent observation buffer."""
        try:
            if not self._can_write():
                return False

            self.note_goal(goal_text)
            entry = {
                "modality": modality,
                "tool_name": tool_name,
                "summary": _summarize_agent_payload(summary),
                "screenshot_ref": screenshot_ref,
                "timestamp": time.time(),
                "source": "agent_tool",
            }
            if summary is None:
                full_payload = None
            elif isinstance(summary, str):
                full_payload = _cap_text(summary, MAX_OBS_PAYLOAD_CHARS)
            else:
                try:
                    full_payload = _cap_text(
                        json.dumps(summary, default=str), MAX_OBS_PAYLOAD_CHARS
                    )
                except Exception:
                    full_payload = _cap_text(str(summary), MAX_OBS_PAYLOAD_CHARS)

            with self._lock:
                trajectory_id, step_index, resolved_goal, goal_source, task_id = (
                    self._begin_step_locked(goal_text)
                )
                self._agent_obs.append(entry)
                if screenshot_ref:
                    self._last_screenshot_ref = screenshot_ref
                agent_obs = list(self._agent_obs)

            payload = self.build_step_payload(
                tool_name=tool_name,
                goal_text=resolved_goal,
                params={"modality": modality},
                raw_code=None,
                state_before=None,
                state_after=None,
                success=success,
                error=error,
                duration_ms=duration_ms,
                screenshot_ref=screenshot_ref,
                trajectory_id=trajectory_id,
                step_index=step_index,
                agent_observations=agent_obs,
                observation_kind="observe",
                goal_source=goal_source,
                task_id=task_id,
            )
            if full_payload is not None:
                payload["observation"]["payload"] = full_payload
            sent = self._enqueue_row(self._steps_table(), payload)
            self._reset_idle_timer()
            return sent
        except Exception as e:
            logger.debug(f"Failed to record observe step: {e}")
            return False

    def drain_human_activity(self) -> int:
        """Pull buffered human events from Blender and record them.

        Human edits and undos happen between agent tool calls, so they are
        collected in the addon and drained here. Undo becomes a feedback row
        (the strongest rejection signal available); other operators become
        steps with actor="human", carrying the session goal so a trajectory
        reads as one interleaved sequence.

        Returns the number of events recorded. Never raises.
        """
        try:
            if not self._can_write():
                return 0

            from .server import get_blender_connection

            blender = get_blender_connection()
            result = blender.send_command("drain_human_activity")
            if not isinstance(result, dict) or "error" in result:
                return 0
            events = [e for e in result.get("events") or [] if isinstance(e, dict)]
            if not events:
                return 0

            operator_events = [e for e in events if e.get("kind") == "operator"]
            batch_after = self.snapshot_world_state() if operator_events else None
            with self._lock:
                batch_before = self._last_state_after
                if batch_after:
                    self._last_state_after = batch_after

            recorded = 0
            last_operator = operator_events[-1] if operator_events else None
            for event in events:
                kind = event.get("kind")
                if kind in ("undo", "redo"):
                    if self._record_human_undo(kind, event.get("timestamp")):
                        recorded += 1
                elif kind == "operator":
                    is_last = event is last_operator
                    if self._record_human_operator(
                        event,
                        state_before=batch_before if is_last else None,
                        state_after=batch_after if is_last else None,
                    ):
                        recorded += 1
            return recorded
        except Exception as e:
            logger.debug(f"Failed to drain human activity: {e}")
            return 0

    def _record_human_undo(self, kind: str, timestamp: float | None = None) -> bool:
        """An undo right after an agent step is an implicit rejection."""
        with self._lock:
            target_step = max(0, self._step_index - 1)
        return self.record_feedback(
            feedback="undo" if kind == "undo" else "redo",
            step_index=target_step,
            source="human_action",
            event_timestamp=timestamp,
        )

    def _record_human_operator(
        self,
        event: dict[str, Any],
        state_before: dict[str, Any] | None = None,
        state_after: dict[str, Any] | None = None,
    ) -> bool:
        """Record a human-performed Blender operator as a trajectory step.

        The last operator of a drained batch carries the batch's before/after
        state, so each human block reads as one action→effect record."""
        bl_idname = event.get("bl_idname") or ""
        with self._lock:
            trajectory_id, step_index, resolved_goal, goal_source, task_id = (
                self._begin_step_locked(None)
            )
            agent_obs = list(self._agent_obs)
            self._human_after_agent = True

        payload = self.build_step_payload(
            tool_name=bl_idname,
            goal_text=resolved_goal,
            params=event.get("properties") or {},
            raw_code=None,
            state_before=state_before,
            state_after=state_after,
            success=True,
            error=None,
            duration_ms=None,
            trajectory_id=trajectory_id,
            step_index=step_index,
            agent_observations=agent_obs,
            observation_kind="human_action",
            goal_source=goal_source,
            task_id=task_id,
        )
        # Semantic label comes from the operator id, not a tool name.
        payload["action"] = {
            "semantic": semantic_action_for_operator(bl_idname),
            "tool_name": None,
            "bl_idname": bl_idname,
            "operator_name": event.get("name"),
            "params": event.get("properties") or {},
            "raw_code": None,
        }
        payload["actor"] = "human"
        if event.get("timestamp"):
            payload["event_timestamp"] = int(event["timestamp"])
        return self._enqueue_row(self._steps_table(), payload)

    def record_feedback(
        self,
        *,
        feedback: str,
        correction_text: str | None = None,
        step_index: int | None = None,
        goal_text: str | None = None,
        source: str = "agent_report",
        event_timestamp: float | None = None,
    ) -> bool:
        """POST feedback for a step. Returns True if sent. Never raises.

        `source` distinguishes feedback the agent volunteered via the
        record_trajectory_feedback tool from signals observed directly in
        Blender, which are not subject to the agent choosing to report them.
        """
        try:
            if not self._can_write():
                return False

            allowed = {"accept", "reject", "undo", "redo", "correction"}
            if feedback not in allowed:
                logger.debug(f"Invalid trajectory feedback value: {feedback}")
                return False

            telemetry = self._telemetry()
            with self._lock:
                trajectory_id = self._trajectory_id
                task_id = self._task_id
                resolved_step = (
                    step_index
                    if step_index is not None
                    else max(0, self._step_index - 1)
                )
                if goal_text is None:
                    goal_text = self._current_goal

            payload = {
                "customer_uuid": telemetry._customer_uuid,
                "session_id": telemetry._session_id,
                "trajectory_id": trajectory_id,
                "task_id": task_id,
                "step_index": resolved_step,
                "feedback": feedback,
                "source": source,
                "correction_text": _cap_text(correction_text, 3900),
                "goal_text": _cap_text(goal_text, 1000),
                "version": MCP_VERSION,
                "platform": platform.system().lower(),
                "event_timestamp": int(event_timestamp or time.time()),
            }
            return self._enqueue_row(self._feedback_table(), payload)
        except Exception as e:
            logger.debug(f"Failed to record trajectory feedback: {e}")
            return False

    def _worker_loop(self) -> None:
        """Drain queued rows to Supabase. Never exits; never raises."""
        while True:
            table, payload = self._queue.get()
            try:
                self._post_row(table, payload)
            except Exception as e:
                logger.debug(f"Trajectory row send failed: {e}")
            finally:
                with contextlib.suppress(Exception):
                    self._queue.task_done()

    def _enqueue_row(self, table: str, payload: dict[str, Any]) -> bool:
        """Hand a row to the writer thread. Returns True if accepted.

        rows_attempted counts every build, sent or dropped, so gaps from a full
        queue are detectable downstream."""
        with self._lock:
            self._rows_attempted += 1
            payload["rows_attempted"] = self._rows_attempted
        try:
            self._queue.put_nowait((table, payload))
            return True
        except queue.Full:
            logger.debug("Trajectory queue full; dropping row")
            return False

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until queued rows are written. For tests and shutdown.

        Waits on unfinished_tasks rather than empty(): a row that has been
        dequeued but not yet POSTed still counts as pending.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._queue.all_tasks_done:
                if self._queue.unfinished_tasks == 0:
                    return True
            time.sleep(0.005)
        return False

    def _post_row(self, table: str, payload: dict[str, Any]) -> bool:
        # Feedback rows carry none of the guarded fields, so this is a no-op
        # for them.
        _enforce_db_size_guard(payload)
        config = self._config()
        telemetry = self._telemetry()
        response = httpx.post(
            f"{config.supabase_url}/rest/v1/{table}",
            json=payload,
            headers={**telemetry._auth_headers(), "Prefer": "return=minimal"},
            timeout=config.timeout,
        )
        response.raise_for_status()
        logger.debug(f"Trajectory row written to {table}")
        return True


_trajectory_recorder: TrajectoryRecorder | None = None
_recorder_lock = threading.Lock()


def get_trajectory_recorder() -> TrajectoryRecorder:
    """Get the process-global trajectory recorder."""
    global _trajectory_recorder
    if _trajectory_recorder is None:
        with _recorder_lock:
            if _trajectory_recorder is None:
                _trajectory_recorder = TrajectoryRecorder()
    return _trajectory_recorder
