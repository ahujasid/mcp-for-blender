# Code created by Siddharth Ahuja: www.github.com/ahujasid © 2025

import re
import bpy
import mathutils
import json
import threading
import socket
import queue
import time
import requests
import tempfile
import traceback
import os
import shutil
import uuid
import zipfile
import zlib
from bpy.props import IntProperty, BoolProperty
import io
from datetime import datetime
import hashlib, hmac, base64
import os.path as osp
from collections import deque
from urllib.parse import quote
from contextlib import contextmanager, redirect_stdout, suppress
from bpy.app.handlers import persistent

bl_info = {
    "name": "MCP for Blender",
    "author": "BlenderMCP",
    "version": (1, 6),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > MCP for Blender",
    "description": "Connect Blender to Claude via MCP",
    "category": "Interface",
}

# Keep in sync with blender_mcp.addon_manager.EXPECTED_ADDON_PROTOCOL_VERSION.
ADDON_PROTOCOL_VERSION = 5

# Per-snapshot object cap for get_world_state_snapshot. Keep in sync with
# blender_mcp.trajectory.MAX_SNAPSHOT_OBJECTS.
MAX_SNAPSHOT_OBJECTS = 4000

# Selected-name cap for get_world_state_snapshot: select-all in a large scene
# would otherwise make `selected` the dominant field of both step snapshots.
# Keep in sync with blender_mcp.trajectory.MAX_SNAPSHOT_SELECTED.
MAX_SNAPSHOT_SELECTED = 1000

RODIN_FREE_TRIAL_KEY = "vibecoding"

# Add User-Agent as required by Poly Haven API
REQ_HEADERS = requests.utils.default_headers()
REQ_HEADERS.update({"User-Agent": "blender-mcp"})

#region Poly Pizza constants and helpers

POLYPIZZA_API_BASE = "https://api.poly.pizza/v1.1"

# The MCP server resolves human-friendly category/licence names to the numeric
# ids the API filters on, so only ids arrive here. Every query parameter of
# the API is Capitalized (Limit, Page, Category, License, Animated — see
# poly.pizza/apispec/v1.1.yaml): lowercase variants are accepted with HTTP 200
# and then silently ignored, so the capitalisation is load-bearing.


def _polypizza_category_id(category):
    """Validate a numeric category id (names are resolved by the MCP server)."""
    if category is None or category == "":
        return None
    if isinstance(category, bool) or not (
        isinstance(category, int)
        or (isinstance(category, str) and category.strip().lstrip("-").isdigit())
    ):
        raise ValueError(f"Poly Pizza category must be a numeric id in 0-11, got {category!r}")
    value = int(category)
    if not 0 <= value <= 11:
        raise ValueError(f"Poly Pizza category id {value} is out of range (valid ids are 0-11)")
    return value


def _polypizza_licence_id(licence):
    """Validate a numeric licence id (names are resolved by the MCP server)."""
    if licence is None or licence == "":
        return None
    if isinstance(licence, bool) or not (
        isinstance(licence, int)
        or (isinstance(licence, str) and licence.strip().lstrip("-").isdigit())
    ):
        raise ValueError(f"Poly Pizza licence must be 0 (CC-BY) or 1 (CC0), got {licence!r}")
    value = int(licence)
    if value not in (0, 1):
        raise ValueError(f"Poly Pizza licence id {value} is invalid (0 = CC-BY, 1 = CC0)")
    return value


def _polypizza_filter_params(category=None, licence=None, animated=False):
    """Build the query filters for a Poly Pizza search.

    Keys are Capitalized and values numeric because the API silently ignores
    anything else. `Animated` is omitted unless animated-only results were asked
    for: the server treats `Animated=0` as falsy and does not filter on it.
    """
    params = {}
    category_id = _polypizza_category_id(category)
    if category_id is not None:
        params["Category"] = category_id
    licence_id = _polypizza_licence_id(licence)
    if licence_id is not None:
        params["License"] = licence_id
    if animated:
        params["Animated"] = 1
    return params


def _polypizza_summarize_model(model):
    """Trim an API record down to the fields worth sending back over MCP."""
    creator = model.get("Creator") or {}
    return {
        "ID": model.get("ID"),
        "Title": model.get("Title"),
        "Creator": creator.get("Username") if isinstance(creator, dict) else None,
        "Licence": model.get("Licence"),
        "Tri Count": model.get("Tri Count"),
        "Animated": bool(model.get("Animated")),
        "Category": model.get("Category"),
        "Tags": model.get("Tags") or [],
        "Thumbnail": model.get("Thumbnail"),
    }


def _polypizza_cdn_error(status_code, headers, content):
    """Describe a CDN response that is not a GLB, or None when it is one.

    static.poly.pizza sits behind Cloudflare bot management and answers 403 with
    an HTML challenge from datacenter IPs. That is neither an auth failure nor a
    missing model, so it gets its own message.
    """
    if status_code == 200 and content[:4] == b"glTF":
        return None

    headers = headers or {}
    content_type = ""
    for key in ("Content-Type", "content-type"):
        value = headers.get(key)
        if value:
            content_type = str(value).lower()
            break

    challenged = bool(headers.get("cf-mitigated") or headers.get("Cf-Mitigated"))
    looks_like_html = "text/html" in content_type or content[:1] == b"<"

    if challenged or (looks_like_html and status_code != 200):
        return (
            f"Poly Pizza's CDN returned a Cloudflare bot-protection challenge (HTTP {status_code}) "
            "instead of the model file. This is not an API key problem - static.poly.pizza takes no "
            "API key - and the model exists. The CDN blocks datacenter, VPN and cloud IPs; retry from "
            "a residential connection, or download the .glb by hand from https://poly.pizza and import "
            "it with File > Import > glTF 2.0."
        )
    if status_code != 200:
        return f"Poly Pizza model file download failed with status code {status_code}"
    if looks_like_html:
        return (
            "Poly Pizza's CDN returned an HTML page instead of a GLB file. The download link may have "
            "expired; search again to get a fresh one."
        )
    return "Poly Pizza returned a file that is not a valid GLB (missing glTF magic bytes)"

#endregion

#region Manual edit capture
# Records what the human does in Blender while an MCP session is live.

MAX_EDIT_EVENTS = 256

# Operators that fire constantly during interactive work and carry no meaningful
# intent on their own.
_IGNORED_OPERATORS = frozenset({
    "view3d.rotate",
    "view3d.move",
    "view3d.zoom",
    "view3d.dolly",
    "view3d.view_axis",
    "view3d.view_orbit",
    "view3d.view_pan",
    "view3d.smoothview",
    "view3d.cursor3d",
    "wm.tool_set_by_id",
    "wm.context_set_value",
    "screen.animation_step",
})

# Operator properties holding filesystem paths. Never recorded.
_PATH_PROPERTY_NAMES = frozenset({
    "filepath",
    "filename",
    "directory",
    "filepath_raw",
    "relpath",
})
_PATH_PROPERTY_SUBSTRINGS = ("filepath", "filename", "directory", "_dir", "path")
MAX_OPERATOR_PROPERTY_CHARS = 200

# depsgraph_update_post fires on every scene update, many times per second
# during interactive drags.
EDIT_POLL_MIN_INTERVAL = 0.1


def _is_path_property(identifier):
    """True if an operator property likely holds a filesystem path."""
    lowered = identifier.lower()
    if lowered in _PATH_PROPERTY_NAMES:
        return True
    return any(token in lowered for token in _PATH_PROPERTY_SUBSTRINGS)


class UserEditRecorder:
    """Buffers human-originated operator and undo events for the MCP server.

    Anything that happens while an agent command is running is attributed to
    the agent, not the human; `agent_command()` brackets that window.
    """

    def __init__(self):
        self._events = deque(maxlen=MAX_EDIT_EVENTS)
        self._agent_depth = 0
        self._last_operator_count = 0
        self._seen_baseline = False
        self._last_poll_time = 0.0

    @contextmanager
    def agent_command(self):
        """Suppress capture for the duration of an agent-issued command."""
        self._agent_depth += 1
        try:
            yield
        finally:
            self._agent_depth = max(0, self._agent_depth - 1)
            self._resync_operator_baseline()

    @property
    def _suppressed(self):
        return self._agent_depth > 0

    def _operator_stack(self):
        try:
            return list(bpy.context.window_manager.operators)
        except Exception:
            return []

    def _resync_operator_baseline(self):
        self._last_operator_count = len(self._operator_stack())
        self._seen_baseline = True

    def poll_operators(self, now=None):
        """Emit rows for operators run since the last poll. Main thread only.

        Throttled to EDIT_POLL_MIN_INTERVAL.
        """
        if self._suppressed:
            return
        now = time.time() if now is None else now
        if (now - self._last_poll_time) < EDIT_POLL_MIN_INTERVAL:
            return
        self._last_poll_time = now
        stack = self._operator_stack()
        count = len(stack)

        # First poll only establishes a baseline.
        if not self._seen_baseline:
            self._last_operator_count = count
            self._seen_baseline = True
            return

        if count <= self._last_operator_count:
            # Unchanged, or shrank because of an undo. Hold the high-water
            # mark so a later redo does not replay emitted operators.
            return

        for op in stack[self._last_operator_count:count]:
            self._record_operator(op)
        self._last_operator_count = count

    def _record_operator(self, op):
        try:
            bl_idname = getattr(op, "bl_idname", None)
            if not bl_idname:
                return
            # bl_idname is UPPER_CASE_OT_form; normalise to bpy.ops form.
            normalized = bl_idname.lower().replace("_ot_", ".", 1)
            if normalized in _IGNORED_OPERATORS:
                return
            self._events.append({
                "kind": "operator",
                "bl_idname": normalized,
                "name": getattr(op, "name", None),
                "properties": self._operator_properties(op),
                "timestamp": time.time(),
            })
        except Exception as e:
            print(f"Manual edit capture: failed to record operator: {e}")

    @staticmethod
    def _operator_properties(op):
        """Best-effort scalar snapshot of an operator's resolved properties."""
        props = {}
        try:
            rna_props = op.properties.bl_rna.properties
        except Exception:
            return props
        for prop in rna_props:
            if prop.identifier == "rna_type":
                continue
            if _is_path_property(prop.identifier):
                continue
            try:
                value = getattr(op.properties, prop.identifier)
            except Exception:
                continue
            if isinstance(value, str):
                props[prop.identifier] = value[:MAX_OPERATOR_PROPERTY_CHARS]
            elif isinstance(value, (bool, int, float)):
                props[prop.identifier] = value
            elif hasattr(value, "__len__") and not isinstance(value, (dict, bytes)):
                try:
                    items = [
                        v[:MAX_OPERATOR_PROPERTY_CHARS] if isinstance(v, str) else v
                        for v in value
                        if isinstance(v, (bool, int, float, str))
                    ]
                    if items and len(items) <= 16:
                        props[prop.identifier] = items
                except Exception:
                    continue
        return props

    def record_undo(self, kind):
        """Record an undo/redo. This is the strongest rejection signal we get."""
        if self._suppressed:
            return
        self._events.append({
            "kind": kind,
            "timestamp": time.time(),
        })
        # Keep the high-water mark so a redo does not re-emit consumed entries.
        self._last_operator_count = max(
            self._last_operator_count, len(self._operator_stack())
        )
        self._seen_baseline = True

    def drain(self):
        """Hand buffered events to the MCP server and clear them."""
        events = list(self._events)
        self._events.clear()
        return events


_edit_recorder = UserEditRecorder()


def get_edit_recorder():
    return _edit_recorder


@persistent
def _blendermcp_undo_post(scene, depsgraph=None):
    _edit_recorder.record_undo("undo")


@persistent
def _blendermcp_redo_post(scene, depsgraph=None):
    _edit_recorder.record_undo("redo")


@persistent
def _blendermcp_depsgraph_post(scene, depsgraph=None):
    _edit_recorder.poll_operators()


def _telemetry_consent_enabled():
    """Read the consent preference directly. Fails closed."""
    try:
        addon_prefs = bpy.context.preferences.addons.get(__name__)
        if not addon_prefs:
            return False
        return bool(addon_prefs.preferences.telemetry_consent)
    except Exception:
        return False


def _register_edit_capture_handlers():
    """Attach manual-edit handlers, but only with telemetry consent."""
    if not _telemetry_consent_enabled():
        _unregister_edit_capture_handlers()
        return False

    handlers = [
        (bpy.app.handlers.undo_post, _blendermcp_undo_post),
        (bpy.app.handlers.redo_post, _blendermcp_redo_post),
        (bpy.app.handlers.depsgraph_update_post, _blendermcp_depsgraph_post),
    ]
    for handler_list, fn in handlers:
        if fn not in handler_list:
            handler_list.append(fn)
    return True


def sync_edit_capture_handlers():
    """Re-apply the consent gate. Safe to call when consent or server state changes."""
    try:
        server_running = bool(
            getattr(bpy.types, "blendermcp_server", None)
            and bpy.types.blendermcp_server.running
        )
    except Exception:
        server_running = False

    if not server_running:
        _unregister_edit_capture_handlers()
        return False
    return _register_edit_capture_handlers()


def _unregister_edit_capture_handlers():
    handlers = [
        (bpy.app.handlers.undo_post, _blendermcp_undo_post),
        (bpy.app.handlers.redo_post, _blendermcp_redo_post),
        (bpy.app.handlers.depsgraph_update_post, _blendermcp_depsgraph_post),
    ]
    for handler_list, fn in handlers:
        with suppress(ValueError):
            handler_list.remove(fn)
#endregion


def get_blendermcp_addon_preferences(context=None):
    """Get add-on preferences object if available."""
    if context is None:
        context = bpy.context
    addon = context.preferences.addons.get(__name__)
    return addon.preferences if addon else None

class BlenderMCPServer:
    def __init__(self, host='localhost', port=9876):
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.server_thread = None
        # Commands are pushed here by client threads and drained by a single
        # timer running on Blender's main thread. bpy.app.timers is not
        # thread-safe, so registering a timer per command (the previous
        # approach) could silently drop the callback - on Windows especially -
        # leaving the client blocked in recv() until its socket timeout.
        self.command_queue = queue.Queue()
        # Live client sockets, so stop() can unblock threads parked in recv().
        self._clients = set()
        self._clients_lock = threading.Lock()

    def _get_config_value(self, scene_attr, pref_attr=None, env_var=None):
        """Read config in order: addon preferences -> scene -> env var."""
        prefs = get_blendermcp_addon_preferences()
        if prefs and pref_attr:
            pref_value = getattr(prefs, pref_attr, "")
            if pref_value:
                return pref_value

        scene_value = getattr(bpy.context.scene, scene_attr, "")
        if scene_value:
            return scene_value

        if env_var:
            env_value = os.getenv(env_var, "")
            if env_value:
                return env_value
        return ""

    def _get_hyper3d_api_key(self):
        # Let the free-trial button temporarily override persistent keys
        # without overwriting user-saved private keys.
        scene_value = getattr(bpy.context.scene, "blendermcp_hyper3d_api_key", "")
        if scene_value == RODIN_FREE_TRIAL_KEY:
            return scene_value
        return self._get_config_value(
            "blendermcp_hyper3d_api_key",
            "hyper3d_api_key",
            "BLENDERMCP_HYPER3D_API_KEY",
        )

    def _get_sketchfab_api_key(self):
        return self._get_config_value(
            "blendermcp_sketchfab_api_key",
            "sketchfab_api_key",
            "BLENDERMCP_SKETCHFAB_API_KEY",
        )

    def _get_polypizza_api_key(self):
        return self._get_config_value(
            "blendermcp_polypizza_api_key",
            "polypizza_api_key",
            "BLENDERMCP_POLYPIZZA_API_KEY",
        )

    def _get_hunyuan3d_secret_id(self):
        return self._get_config_value(
            "blendermcp_hunyuan3d_secret_id",
            "hunyuan3d_secret_id",
            "BLENDERMCP_HUNYUAN3D_SECRET_ID",
        )

    def _get_hunyuan3d_secret_key(self):
        return self._get_config_value(
            "blendermcp_hunyuan3d_secret_key",
            "hunyuan3d_secret_key",
            "BLENDERMCP_HUNYUAN3D_SECRET_KEY",
        )

    def _get_hunyuan3d_api_url(self):
        return self._get_config_value(
            "blendermcp_hunyuan3d_api_url",
            "hunyuan3d_api_url",
            "BLENDERMCP_HUNYUAN3D_API_URL",
        ) or "http://localhost:8081"

    def start(self):
        if bpy.app.background:
            print("BlenderMCP: cannot start server in background mode (blender -b) - commands would never execute\n"
                  "BlenderMCP: run Blender with a GUI, or use a virtual display: xvfb-run -a blender")
            return

        if self.running:
            print("Server is already running")
            return

        self.running = True

        try:
            # Create socket
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            # Backlog of 1 meant a reconnecting client could complete the TCP
            # handshake and then never be accept()ed - a connection that looks
            # established but is never serviced.
            self.socket.listen(5)

            # Start server thread
            self.server_thread = threading.Thread(target=self._server_loop)
            self.server_thread.daemon = True
            self.server_thread.start()

            _register_edit_capture_handlers()

            # start() is called from the operator, i.e. the main thread, so
            # this is the only safe place to touch bpy.app.timers.
            if not bpy.app.timers.is_registered(self._drain_command_queue):
                bpy.app.timers.register(self._drain_command_queue, persistent=True)

            print(f"BlenderMCP server started on {self.host}:{self.port}")
        except Exception as e:
            print(f"Failed to start server: {str(e)}")
            self.stop()

    def stop(self):
        self.running = False

        _unregister_edit_capture_handlers()
        get_edit_recorder().drain()

        try:
            if bpy.app.timers.is_registered(self._drain_command_queue):
                bpy.app.timers.unregister(self._drain_command_queue)
        except Exception:
            pass

        # Close socket
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None

        # Shut down live client sockets. Without this, handler threads stay
        # parked in a blocking recv() forever; being daemon threads they then
        # outlive the restart and close connections the new server owns
        # (the WinError 10054 seen after toggling the addon).
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass

        # Drop any commands that will never be serviced now.
        while True:
            try:
                self.command_queue.get_nowait()
            except queue.Empty:
                break

        # Wait for thread to finish
        if self.server_thread:
            try:
                if self.server_thread.is_alive():
                    self.server_thread.join(timeout=1.0)
            except:
                pass
            self.server_thread = None

        print("BlenderMCP server stopped")

    def _server_loop(self):
        """Main server loop in a separate thread"""
        print("Server thread started")
        self.socket.settimeout(1.0)  # Timeout to allow for stopping

        while self.running:
            try:
                # Accept new connection
                try:
                    client, address = self.socket.accept()
                    print(f"Connected to client: {address}")

                    # Handle client in a separate thread
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client,)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                except socket.timeout:
                    # Just check running condition
                    continue
                except Exception as e:
                    print(f"Error accepting connection: {str(e)}")
                    time.sleep(0.5)
            except Exception as e:
                print(f"Error in server loop: {str(e)}")
                if not self.running:
                    break
                time.sleep(0.5)

        print("Server thread stopped")

    def _drain_command_queue(self):
        """Run queued commands on Blender's main thread.

        Registered once by start(); returns the poll interval so Blender keeps
        calling it. All bpy access happens here, on the main thread.
        """
        if not self.running:
            return None

        while True:
            try:
                command, client = self.command_queue.get_nowait()
            except queue.Empty:
                break

            try:
                response = self.execute_command(command)
                response_json = json.dumps(response)
            except Exception as e:
                print(f"Error executing command: {str(e)}")
                traceback.print_exc()
                response_json = json.dumps({"status": "error", "message": str(e)})

            try:
                client.sendall(response_json.encode('utf-8'))
            except Exception:
                print("Failed to send response - client disconnected")

        return 0.05

    def _handle_client(self, client):
        """Handle connected client"""
        print("Client handler started")
        # A finite timeout keeps this loop responsive to self.running instead
        # of parking in recv() forever.
        client.settimeout(1.0)
        with self._clients_lock:
            self._clients.add(client)
        buffer = b''

        try:
            while self.running:
                # Receive data
                try:
                    data = client.recv(8192)
                    if not data:
                        print("Client disconnected")
                        break

                    buffer += data
                    try:
                        # Try to parse command
                        command = json.loads(buffer.decode('utf-8'))
                        buffer = b''

                        # Hand off to the main thread. Never call
                        # bpy.app.timers.register() from here - it is not
                        # thread-safe and the callback can be silently lost.
                        print(f"Queued command: {command.get('type')}")
                        self.command_queue.put((command, client))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        # Incomplete data, wait for more. A multi-byte UTF-8
                        # character can land split across a recv() chunk
                        # boundary, which fails decode() before json.loads()
                        # ever runs - that's incomplete data too, not garbage.
                        pass
                except socket.timeout:
                    # Expected; loop round and re-check self.running.
                    continue
                except Exception as e:
                    print(f"Error receiving data: {str(e)}")
                    break
        except Exception as e:
            print(f"Error in client handler: {str(e)}")
        finally:
            with self._clients_lock:
                self._clients.discard(client)
            try:
                client.close()
            except:
                pass
            print("Client handler stopped")

    def execute_command(self, command):
        """Execute a command in the main Blender thread"""
        try:
            with get_edit_recorder().agent_command():
                return self._execute_command_internal(command)

        except Exception as e:
            print(f"Error executing command: {str(e)}")
            traceback.print_exc()
            return {"status": "error", "message": str(e)}

    def _execute_command_internal(self, command):
        """Internal command execution with proper context"""
        cmd_type = command.get("type")
        params = command.get("params", {})

        # Trivial liveness check. Touches no bpy data, so a successful ping
        # alongside a failing command isolates data access from transport.
        if cmd_type == "ping":
            return {"status": "success", "result": {"pong": True}}

        # Add a handler for checking PolyHaven status
        if cmd_type == "get_polyhaven_status":
            return {"status": "success", "result": self.get_polyhaven_status()}

        # Base handlers that are always available
        handlers = {
            "get_scene_info": self.get_scene_info,
            "get_world_state_snapshot": self.get_world_state_snapshot,
            "get_addon_info": self.get_addon_info,
            "get_object_info": self.get_object_info,
            "get_viewport_screenshot": self.get_viewport_screenshot,
            "execute_code": self.execute_code,
            "drain_human_activity": self.drain_human_activity,
            "get_telemetry_consent": self.get_telemetry_consent,
            "set_telemetry_consent": self.set_telemetry_consent,
            "get_polyhaven_status": self.get_polyhaven_status,
            "get_hyper3d_status": self.get_hyper3d_status,
            "get_sketchfab_status": self.get_sketchfab_status,
            "get_polypizza_status": self.get_polypizza_status,
            "get_hunyuan3d_status": self.get_hunyuan3d_status,
        }

        # Add Polyhaven handlers only if enabled
        if bpy.context.scene.blendermcp_use_polyhaven:
            polyhaven_handlers = {
                "get_polyhaven_categories": self.get_polyhaven_categories,
                "search_polyhaven_assets": self.search_polyhaven_assets,
                "download_polyhaven_asset": self.download_polyhaven_asset,
                "set_texture": self.set_texture,
            }
            handlers.update(polyhaven_handlers)

        # Add Hyper3d handlers only if enabled
        if bpy.context.scene.blendermcp_use_hyper3d:
            polyhaven_handlers = {
                "create_rodin_job": self.create_rodin_job,
                "poll_rodin_job_status": self.poll_rodin_job_status,
                "import_generated_asset": self.import_generated_asset,
            }
            handlers.update(polyhaven_handlers)

        # Add Sketchfab handlers only if enabled
        if bpy.context.scene.blendermcp_use_sketchfab:
            sketchfab_handlers = {
                "search_sketchfab_models": self.search_sketchfab_models,
                "get_sketchfab_model_preview": self.get_sketchfab_model_preview,
                "download_sketchfab_model": self.download_sketchfab_model,
            }
            handlers.update(sketchfab_handlers)

        # Add Poly Pizza handlers only if enabled
        if bpy.context.scene.blendermcp_use_polypizza:
            polypizza_handlers = {
                "search_polypizza_models": self.search_polypizza_models,
                "download_polypizza_model": self.download_polypizza_model,
            }
            handlers.update(polypizza_handlers)

        # Add Hunyuan3d handlers only if enabled
        if bpy.context.scene.blendermcp_use_hunyuan3d:
            hunyuan_handlers = {
                "create_hunyuan_job": self.create_hunyuan_job,
                "poll_hunyuan_job_status": self.poll_hunyuan_job_status,
                "import_generated_asset_hunyuan": self.import_generated_asset_hunyuan,
                "texture_hunyuan_mesh": self.texture_hunyuan_job_local,
            }
            handlers.update(hunyuan_handlers)

        handler = handlers.get(cmd_type)
        if handler:
            try:
                print(f"Executing handler for {cmd_type}")
                result = handler(**params)
                print(f"Handler execution complete")
                return {"status": "success", "result": result}
            except Exception as e:
                print(f"Error in handler: {str(e)}")
                traceback.print_exc()
                return {"status": "error", "message": str(e)}
        else:
            return {"status": "error", "message": f"Unknown command type: {cmd_type}"}



    def get_addon_info(self):
        """Version/capability handshake for the MCP server (and install tooling)."""
        return {
            "name": bl_info.get("name", "MCP for Blender"),
            "addon_version": list(bl_info.get("version", (0, 0))),
            "protocol_version": ADDON_PROTOCOL_VERSION,
            "capabilities": sorted([
                "get_scene_info",
                "get_world_state_snapshot",
                "get_addon_info",
                "get_object_info",
                "get_viewport_screenshot",
                "execute_code",
                "drain_human_activity",
                "get_telemetry_consent",
                "set_telemetry_consent",
            ]),
            "blender_version": bpy.app.version_string,
        }

    def get_scene_info(self):
        """Get information about the current Blender scene"""
        try:
            print("Getting scene info...")
            # Simplify the scene info to reduce data size
            scene_info = {
                "name": bpy.context.scene.name,
                "object_count": len(bpy.context.scene.objects),
                "objects": [],
                "materials_count": len(bpy.data.materials),
            }

            # Collect minimal object information (limit to first 10 objects)
            for i, obj in enumerate(bpy.context.scene.objects):
                if i >= 10:  # Reduced from 20 to 10
                    break

                obj_info = {
                    "name": obj.name,
                    "type": obj.type,
                    # Only include basic location data
                    "location": [round(float(obj.location.x), 2),
                                round(float(obj.location.y), 2),
                                round(float(obj.location.z), 2)],
                }
                scene_info["objects"].append(obj_info)

            print(f"Scene info collected: {len(scene_info['objects'])} objects")
            return scene_info
        except Exception as e:
            print(f"Error in get_scene_info: {str(e)}")
            traceback.print_exc()
            return {"error": str(e)}

    def drain_human_activity(self):
        """Return human-originated events buffered since the last drain.

        Consent is enforced MCP-side (the server only drains and uploads when
        the user has opted in), but we also refuse here so a buffer does not
        accumulate for a user who has said no.
        """
        try:
            if not self.get_telemetry_consent().get("consent"):
                get_edit_recorder().drain()
                return {"events": []}
            return {"events": get_edit_recorder().drain()}
        except Exception as e:
            print(f"Error draining manual edits: {str(e)}")
            return {"error": str(e)}

    @staticmethod
    def _snapshot_geometry(obj):
        """World-space AABB + dimensions for one object, or None.

        Without these, downstream analysis cannot compute contact, containment
        or collision: `scale` alone is a multiplier on unknown base geometry.
        Uses obj.bound_box (8 cached local corners) rather than mesh vertices,
        so cost is constant per object regardless of poly count.
        """
        bound_box = getattr(obj, "bound_box", None)
        if not bound_box:
            return None
        try:
            matrix_world = obj.matrix_world
            xs, ys, zs = [], [], []
            for corner in bound_box:
                world = matrix_world @ mathutils.Vector(corner)
                xs.append(world.x)
                ys.append(world.y)
                zs.append(world.z)
            return {
                "aabb_min": [round(min(xs), 3), round(min(ys), 3), round(min(zs), 3)],
                "aabb_max": [round(max(xs), 3), round(max(ys), 3), round(max(zs), 3)],
                "dimensions": [
                    round(float(obj.dimensions.x), 3),
                    round(float(obj.dimensions.y), 3),
                    round(float(obj.dimensions.z), 3),
                ],
            }
        except Exception:
            return None

    @staticmethod
    def _snapshot_relations(obj):
        """Parent and constraint targets, so hierarchies read correctly.

        World `location` alone misreports parented objects, whose authored
        values are parent-relative.
        """
        relations = {}
        parent = getattr(obj, "parent", None)
        if parent:
            relations["parent"] = parent.name
            relations["parent_type"] = obj.parent_type
            loc = obj.matrix_local.translation
            relations["local_location"] = [
                round(float(loc.x), 3),
                round(float(loc.y), 3),
                round(float(loc.z), 3),
            ]
        constraints = []
        for constraint in getattr(obj, "constraints", None) or []:
            entry = {"type": constraint.type}
            target = getattr(constraint, "target", None)
            if target:
                entry["target"] = target.name
            constraints.append(entry)
            if len(constraints) >= 8:
                break
        if constraints:
            relations["constraints"] = constraints
        modifiers = [m.type for m in (getattr(obj, "modifiers", None) or [])[:8]]
        if modifiers:
            relations["modifiers"] = modifiers
        return relations

    @staticmethod
    def _snapshot_animation(obj):
        """Action name and per-channel keyframe summary for one object, or {}.

        Static transforms alone cannot distinguish an authored edit from
        playback landing on a different frame. Reads F-curve metadata
        (`data_path`, `array_index`, `len(keyframe_points)`) rather than
        individual keyframes, so cost stays proportional to channel count
        rather than to animation length.
        """
        try:
            anim_data = getattr(obj, "animation_data", None)
            if not anim_data:
                return {}

            animation = {}
            action = getattr(anim_data, "action", None)
            if action:
                animation["action"] = action.name
                channels = []
                total_keyframes = 0
                frame_min, frame_max = None, None
                for fcurve in action.fcurves:
                    keyframe_points = fcurve.keyframe_points
                    count = len(keyframe_points)
                    total_keyframes += count
                    if count and len(channels) < 16:
                        channels.append({
                            "data_path": fcurve.data_path,
                            "array_index": fcurve.array_index,
                            "keyframes": count,
                        })
                    if count:
                        first = keyframe_points[0].co.x
                        last = keyframe_points[-1].co.x
                        frame_min = first if frame_min is None else min(frame_min, first)
                        frame_max = last if frame_max is None else max(frame_max, last)
                if channels:
                    animation["channels"] = channels
                animation["keyframe_count"] = total_keyframes
                if frame_min is not None:
                    animation["frame_range"] = [round(float(frame_min), 3),
                                                round(float(frame_max), 3)]

            drivers = getattr(anim_data, "drivers", None)
            if drivers and len(drivers):
                animation["driver_count"] = len(drivers)

            nla_tracks = [
                track.name
                for track in (getattr(anim_data, "nla_tracks", None) or [])[:8]
            ]
            if nla_tracks:
                animation["nla_tracks"] = nla_tracks

            return {"animation": animation} if animation else {}
        except Exception:
            return {}

    @staticmethod
    def _shader_fingerprint(id_block):
        """Stable short hash of a node tree (material or world), or None.

        Node identities plus rounded input values, so tweaking a color or
        rewiring a link changes the fingerprint. Lets downstream deltas see
        shader edits that leave every object transform untouched.
        """
        try:
            if id_block is None:
                return None
            tree = id_block.node_tree if getattr(id_block, "use_nodes", False) else None
            if tree is None:
                color = getattr(id_block, "diffuse_color", None) or getattr(id_block, "color", None)
                basis = str([round(float(v), 3) for v in color]) if color is not None else ""
            else:
                parts = []
                for node in tree.nodes:
                    values = []
                    for sock in node.inputs:
                        dv = getattr(sock, "default_value", None)
                        if isinstance(dv, (int, float)):
                            values.append(round(float(dv), 3))
                        elif dv is not None:
                            with suppress(TypeError, ValueError):
                                values.extend(round(float(v), 3) for v in dv)
                    parts.append(f"{node.bl_idname}{values}")
                parts.sort()
                parts.append(str(len(tree.links)))
                basis = "|".join(parts)
            return format(zlib.crc32(basis.encode("utf-8")), "08x")
        except Exception:
            return None

    @staticmethod
    def _project_id():
        """Salted hash linking sessions on the same .blend without storing its path."""
        try:
            filepath = bpy.data.filepath
            if not filepath:
                return None
            return hashlib.sha256(f"{uuid.getnode()}:{filepath}".encode("utf-8")).hexdigest()[:16]
        except Exception:
            return None

    def get_world_state_snapshot(self):
        """Compact world-state snapshot for trajectory capture (no mesh/shader detail)."""
        try:
            scene = bpy.context.scene
            selected = [obj.name for obj in bpy.context.selected_objects]
            selected_count = len(selected)
            selected_truncated = selected_count > MAX_SNAPSHOT_SELECTED
            if selected_truncated:
                # Sorted so before/after snapshots keep the same subset.
                selected = sorted(selected)[:MAX_SNAPSHOT_SELECTED]
            objects = []

            all_objects = list(scene.objects)
            truncated = len(all_objects) > MAX_SNAPSHOT_OBJECTS
            if truncated:
                # scene.objects iterates in an order that shifts as objects are
                # created, so an arbitrary prefix would leave the before/after
                # snapshots of one step holding different subsets and the delta
                # reporting phantom adds/removes. Sorting keeps them aligned.
                all_objects = sorted(all_objects, key=lambda o: o.name)[:MAX_SNAPSHOT_OBJECTS]

            for obj in all_objects:
                materials = []
                if getattr(obj, "material_slots", None):
                    materials = [
                        slot.material.name
                        for slot in obj.material_slots
                        if slot.material
                    ]

                entry = {
                    "name": obj.name,
                    "type": obj.type,
                    "location": [
                        round(float(obj.location.x), 3),
                        round(float(obj.location.y), 3),
                        round(float(obj.location.z), 3),
                    ],
                    "rotation": [
                        round(float(obj.rotation_euler.x), 3),
                        round(float(obj.rotation_euler.y), 3),
                        round(float(obj.rotation_euler.z), 3),
                    ],
                    "scale": [
                        round(float(obj.scale.x), 3),
                        round(float(obj.scale.y), 3),
                        round(float(obj.scale.z), 3),
                    ],
                    "visible": bool(obj.visible_get()),
                    "materials": materials,
                }
                geometry = self._snapshot_geometry(obj)
                if geometry:
                    entry.update(geometry)
                entry.update(self._snapshot_relations(obj))
                entry.update(self._snapshot_animation(obj))
                data = getattr(obj, "data", None)
                if obj.type == "MESH" and data is not None:
                    entry["mesh"] = {
                        "vertices": len(data.vertices),
                        "polygons": len(data.polygons),
                    }
                objects.append(entry)

            camera = scene.camera
            camera_info = None
            if camera:
                camera_info = {
                    "name": camera.name,
                    "location": [
                        round(float(camera.location.x), 3),
                        round(float(camera.location.y), 3),
                        round(float(camera.location.z), 3),
                    ],
                    "rotation": [
                        round(float(camera.rotation_euler.x), 3),
                        round(float(camera.rotation_euler.y), 3),
                        round(float(camera.rotation_euler.z), 3),
                    ],
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
                    "location": [
                        round(float(obj.location.x), 3),
                        round(float(obj.location.y), 3),
                        round(float(obj.location.z), 3),
                    ],
                }
                if obj.data:
                    light_entry["light_type"] = obj.data.type
                    light_entry["energy"] = round(float(obj.data.energy), 3)
                lights.append(light_entry)
                if len(lights) >= 20:
                    break

            return {
                "name": scene.name,
                "object_count": len(scene.objects),
                # Explicit, so consumers never have to infer truncation from a
                # hardcoded cap they might disagree with.
                "objects_listed": len(objects),
                "objects_truncated": truncated,
                "selected": selected,
                "selected_count": selected_count,
                "selected_truncated": selected_truncated,
                "frame_current": scene.frame_current,
                "frame_start": scene.frame_start,
                "frame_end": scene.frame_end,
                "fps": round(float(scene.render.fps) / scene.render.fps_base, 3),
                "objects": objects,
                "active_camera": camera.name if camera else None,
                "camera": camera_info,
                "lights": lights,
                "materials_count": len(bpy.data.materials),
                "material_fps": {
                    m.name: self._shader_fingerprint(m)
                    for m in list(bpy.data.materials)[:200]
                },
                "world_fp": self._shader_fingerprint(scene.world),
                "project_id": self._project_id(),
                "blender_version": bpy.app.version_string,
                "snapshot_source": "native",
            }
        except Exception as e:
            print(f"Error in get_world_state_snapshot: {str(e)}")
            traceback.print_exc()
            return {"error": str(e)}

    @staticmethod
    def _get_aabb(obj):
        """ Returns the world-space axis-aligned bounding box (AABB) of an object. """
        if obj.type != 'MESH':
            raise TypeError("Object must be a mesh")

        # Get the bounding box corners in local space
        local_bbox_corners = [mathutils.Vector(corner) for corner in obj.bound_box]

        # Convert to world coordinates
        world_bbox_corners = [obj.matrix_world @ corner for corner in local_bbox_corners]

        # Compute axis-aligned min/max coordinates
        min_corner = mathutils.Vector(map(min, zip(*world_bbox_corners)))
        max_corner = mathutils.Vector(map(max, zip(*world_bbox_corners)))

        return [
            [*min_corner], [*max_corner]
        ]

    def get_object_info(self, name):
        """Get detailed information about a specific object"""
        obj = bpy.data.objects.get(name)
        if not obj:
            raise ValueError(f"Object not found: {name}")

        # Basic object info
        obj_info = {
            "name": obj.name,
            "type": obj.type,
            "location": [obj.location.x, obj.location.y, obj.location.z],
            "rotation": [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z],
            "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            "visible": obj.visible_get(),
            "materials": [],
        }

        if obj.type == "MESH":
            bounding_box = self._get_aabb(obj)
            obj_info["world_bounding_box"] = bounding_box

        # Add material slots
        for slot in obj.material_slots:
            if slot.material:
                obj_info["materials"].append(slot.material.name)

        # Add mesh data if applicable
        if obj.type == 'MESH' and obj.data:
            mesh = obj.data
            obj_info["mesh"] = {
                "vertices": len(mesh.vertices),
                "edges": len(mesh.edges),
                "polygons": len(mesh.polygons),
            }

        return obj_info

    def get_viewport_screenshot(self, max_size=800, filepath=None, format="png"):
        """
        Capture a screenshot of the current 3D viewport and save it to the specified path.

        Parameters:
        - max_size: Maximum size in pixels for the largest dimension of the image
        - filepath: Path where to save the screenshot file
        - format: Image format (png, jpg, etc.)

        Returns success/error status
        """
        # screen.screenshot_area captures the OS window framebuffer, which is
        # all-black whenever the Blender window is not composited in the
        # foreground (the normal case when Blender is driven headless-style via
        # MCP). Render the viewport with gpu.types.GPUOffScreen.draw_view3d
        # instead, which is independent of window compositing state, and fall
        # back to the window grab if offscreen rendering is unavailable (e.g. no
        # GPU context). The response reports which path produced the image.
        try:
            if not filepath:
                return {"error": "No filepath provided"}

            area = region = space = None
            for a in bpy.context.screen.areas:
                if a.type == 'VIEW_3D':
                    area = a
                    space = a.spaces.active
                    region = next((r for r in a.regions if r.type == 'WINDOW'), None)
                    break

            if not area or region is None or space is None:
                return {"error": "No 3D viewport found"}

            method = "offscreen"
            try:
                import gpu
                import numpy as np

                r3d = space.region_3d
                src_w, src_h = region.width, region.height
                if max(src_w, src_h) > max_size:
                    s = max_size / max(src_w, src_h)
                    width, height = max(1, int(src_w * s)), max(1, int(src_h * s))
                else:
                    width, height = src_w, src_h

                offscreen = gpu.types.GPUOffScreen(width, height)
                try:
                    offscreen.draw_view3d(
                        bpy.context.scene, bpy.context.view_layer, space, region,
                        r3d.view_matrix, r3d.window_matrix, do_color_management=True,
                    )
                    buf = offscreen.texture_color.read()
                finally:
                    offscreen.free()

                buf.dimensions = width * height * 4
                pixels = np.asarray(buf, dtype=np.float32) / 255.0  # GPU buffer is 0..255

                image = bpy.data.images.new("mcp_viewport", width, height, alpha=True)
                image.pixels.foreach_set(pixels.ravel())
                image.filepath_raw = filepath
                image.file_format = format.upper()
                image.save()
                bpy.data.images.remove(image)

            except Exception as offscreen_err:
                print(f"[BlenderMCP] offscreen capture failed ({offscreen_err}); "
                      "falling back to window grab", flush=True)
                method = "window_grab"
                with bpy.context.temp_override(area=area):
                    bpy.ops.screen.screenshot_area(filepath=filepath)
                img = bpy.data.images.load(filepath)
                width, height = img.size
                if max(width, height) > max_size:
                    s = max_size / max(width, height)
                    width, height = int(width * s), int(height * s)
                    img.scale(width, height)
                    img.file_format = format.upper()
                    img.save()
                bpy.data.images.remove(img)

            return {
                "success": True,
                "width": width,
                "height": height,
                "filepath": filepath,
                "method": method,
            }

        except Exception as e:
            return {"error": str(e)}

    def execute_code(self, code):
        """Execute arbitrary Blender Python code"""
        # This is powerful but potentially dangerous - use with caution
        try:
            # Create a local namespace for execution
            namespace = {"bpy": bpy}

            # Capture stdout during execution, and return it as result
            capture_buffer = io.StringIO()
            with redirect_stdout(capture_buffer):
                exec(code, namespace)

            captured_output = capture_buffer.getvalue()
            return {"executed": True, "result": captured_output}
        except Exception as e:
            raise Exception(f"Code execution error: {str(e)}")



    def get_polyhaven_categories(self, asset_type):
        """Get categories for a specific asset type from Polyhaven"""
        try:
            if asset_type not in ["hdris", "textures", "models", "all"]:
                return {"error": f"Invalid asset type: {asset_type}. Must be one of: hdris, textures, models, all"}

            response = requests.get(f"https://api.polyhaven.com/categories/{asset_type}", headers=REQ_HEADERS)
            if response.status_code == 200:
                return {"categories": response.json()}
            else:
                return {"error": f"API request failed with status code {response.status_code}"}
        except Exception as e:
            return {"error": str(e)}

    def search_polyhaven_assets(self, asset_type=None, categories=None):
        """Search for assets from Polyhaven with optional filtering"""
        try:
            url = "https://api.polyhaven.com/assets"
            params = {}

            if asset_type and asset_type != "all":
                if asset_type not in ["hdris", "textures", "models"]:
                    return {"error": f"Invalid asset type: {asset_type}. Must be one of: hdris, textures, models, all"}
                params["type"] = asset_type

            if categories:
                params["categories"] = categories

            response = requests.get(url, params=params, headers=REQ_HEADERS)
            if response.status_code == 200:
                # Limit the response size to avoid overwhelming Blender
                assets = response.json()
                # Return only the first 20 assets to keep response size manageable
                limited_assets = {}
                for i, (key, value) in enumerate(assets.items()):
                    if i >= 20:  # Limit to 20 assets
                        break
                    limited_assets[key] = value

                return {"assets": limited_assets, "total_count": len(assets), "returned_count": len(limited_assets)}
            else:
                return {"error": f"API request failed with status code {response.status_code}"}
        except Exception as e:
            return {"error": str(e)}

    def download_polyhaven_asset(self, asset_id, asset_type, resolution="1k", file_format=None):
        try:
            # First get the files information
            files_response = requests.get(f"https://api.polyhaven.com/files/{asset_id}", headers=REQ_HEADERS)
            if files_response.status_code != 200:
                return {"error": f"Failed to get asset files: {files_response.status_code}"}

            files_data = files_response.json()

            # Handle different asset types
            if asset_type == "hdris":
                # For HDRIs, download the .hdr or .exr file
                if not file_format:
                    file_format = "hdr"  # Default format for HDRIs

                if "hdri" in files_data and resolution in files_data["hdri"] and file_format in files_data["hdri"][resolution]:
                    file_info = files_data["hdri"][resolution][file_format]
                    file_url = file_info["url"]

                    # For HDRIs, we need to save to a temporary file first
                    # since Blender can't properly load HDR data directly from memory
                    with tempfile.NamedTemporaryFile(suffix=f".{file_format}", delete=False) as tmp_file:
                        # Download the file
                        response = requests.get(file_url, headers=REQ_HEADERS)
                        if response.status_code != 200:
                            return {"error": f"Failed to download HDRI: {response.status_code}"}

                        tmp_file.write(response.content)
                        tmp_path = tmp_file.name

                    try:
                        # Create a new world if none exists
                        if not bpy.data.worlds:
                            bpy.data.worlds.new("World")

                        world = bpy.data.worlds[0]
                        world.use_nodes = True
                        node_tree = world.node_tree

                        # Clear existing nodes
                        for node in node_tree.nodes:
                            node_tree.nodes.remove(node)

                        # Create nodes
                        tex_coord = node_tree.nodes.new(type='ShaderNodeTexCoord')
                        tex_coord.location = (-800, 0)

                        mapping = node_tree.nodes.new(type='ShaderNodeMapping')
                        mapping.location = (-600, 0)

                        # Load the image from the temporary file
                        env_tex = node_tree.nodes.new(type='ShaderNodeTexEnvironment')
                        env_tex.location = (-400, 0)
                        env_tex.image = bpy.data.images.load(tmp_path)

                        # Use a color space that exists in all Blender versions
                        if file_format.lower() == 'exr':
                            # Try to use Linear color space for EXR files
                            try:
                                env_tex.image.colorspace_settings.name = 'Linear'
                            except:
                                # Fallback to Non-Color if Linear isn't available
                                env_tex.image.colorspace_settings.name = 'Non-Color'
                        else:  # hdr
                            # For HDR files, try these options in order
                            for color_space in ['Linear', 'Linear Rec.709', 'Non-Color']:
                                try:
                                    env_tex.image.colorspace_settings.name = color_space
                                    break  # Stop if we successfully set a color space
                                except:
                                    continue

                        background = node_tree.nodes.new(type='ShaderNodeBackground')
                        background.location = (-200, 0)

                        output = node_tree.nodes.new(type='ShaderNodeOutputWorld')
                        output.location = (0, 0)

                        # Connect nodes
                        node_tree.links.new(tex_coord.outputs['Generated'], mapping.inputs['Vector'])
                        node_tree.links.new(mapping.outputs['Vector'], env_tex.inputs['Vector'])
                        node_tree.links.new(env_tex.outputs['Color'], background.inputs['Color'])
                        node_tree.links.new(background.outputs['Background'], output.inputs['Surface'])

                        # Set as active world
                        bpy.context.scene.world = world

                        # Clean up temporary file
                        try:
                            tempfile._cleanup()  # This will clean up all temporary files
                        except:
                            pass

                        return {
                            "success": True,
                            "message": f"HDRI {asset_id} imported successfully",
                            "image_name": env_tex.image.name
                        }
                    except Exception as e:
                        return {"error": f"Failed to set up HDRI in Blender: {str(e)}"}
                else:
                    return {"error": f"Requested resolution or format not available for this HDRI"}

            elif asset_type == "textures":
                if not file_format:
                    file_format = "jpg"  # Default format for textures

                downloaded_maps = {}

                try:
                    for map_type in files_data:
                        if map_type not in ["blend", "gltf"]:  # Skip non-texture files
                            if resolution in files_data[map_type] and file_format in files_data[map_type][resolution]:
                                file_info = files_data[map_type][resolution][file_format]
                                file_url = file_info["url"]

                                # Use NamedTemporaryFile like we do for HDRIs
                                with tempfile.NamedTemporaryFile(suffix=f".{file_format}", delete=False) as tmp_file:
                                    # Download the file
                                    response = requests.get(file_url, headers=REQ_HEADERS)
                                    if response.status_code == 200:
                                        tmp_file.write(response.content)
                                        tmp_path = tmp_file.name

                                        # Load image from temporary file
                                        image = bpy.data.images.load(tmp_path)
                                        image.name = f"{asset_id}_{map_type}.{file_format}"

                                        # Pack the image into .blend file
                                        image.pack()

                                        # Set color space based on map type
                                        if map_type in ['color', 'diffuse', 'albedo']:
                                            try:
                                                image.colorspace_settings.name = 'sRGB'
                                            except:
                                                pass
                                        else:
                                            try:
                                                image.colorspace_settings.name = 'Non-Color'
                                            except:
                                                pass

                                        downloaded_maps[map_type] = image

                                        # Clean up temporary file
                                        try:
                                            os.unlink(tmp_path)
                                        except:
                                            pass

                    if not downloaded_maps:
                        return {"error": f"No texture maps found for the requested resolution and format"}

                    # Create a new material with the downloaded textures
                    mat = bpy.data.materials.new(name=asset_id)
                    mat.use_nodes = True
                    nodes = mat.node_tree.nodes
                    links = mat.node_tree.links

                    # Clear default nodes
                    for node in nodes:
                        nodes.remove(node)

                    # Create output node
                    output = nodes.new(type='ShaderNodeOutputMaterial')
                    output.location = (300, 0)

                    # Create principled BSDF node
                    principled = nodes.new(type='ShaderNodeBsdfPrincipled')
                    principled.location = (0, 0)
                    links.new(principled.outputs[0], output.inputs[0])

                    # Add texture nodes based on available maps
                    tex_coord = nodes.new(type='ShaderNodeTexCoord')
                    tex_coord.location = (-800, 0)

                    mapping = nodes.new(type='ShaderNodeMapping')
                    mapping.location = (-600, 0)
                    mapping.vector_type = 'TEXTURE'  # Changed from default 'POINT' to 'TEXTURE'
                    links.new(tex_coord.outputs['UV'], mapping.inputs['Vector'])

                    # Position offset for texture nodes
                    x_pos = -400
                    y_pos = 300

                    # Connect different texture maps
                    for map_type, image in downloaded_maps.items():
                        tex_node = nodes.new(type='ShaderNodeTexImage')
                        tex_node.location = (x_pos, y_pos)
                        tex_node.image = image

                        # Set color space based on map type
                        if map_type.lower() in ['color', 'diffuse', 'albedo']:
                            try:
                                tex_node.image.colorspace_settings.name = 'sRGB'
                            except:
                                pass  # Use default if sRGB not available
                        else:
                            try:
                                tex_node.image.colorspace_settings.name = 'Non-Color'
                            except:
                                pass  # Use default if Non-Color not available

                        links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])

                        # Connect to appropriate input on Principled BSDF
                        if map_type.lower() in ['color', 'diffuse', 'albedo']:
                            links.new(tex_node.outputs['Color'], principled.inputs['Base Color'])
                        elif map_type.lower() in ['roughness', 'rough']:
                            links.new(tex_node.outputs['Color'], principled.inputs['Roughness'])
                        elif map_type.lower() in ['metallic', 'metalness', 'metal']:
                            links.new(tex_node.outputs['Color'], principled.inputs['Metallic'])
                        elif map_type.lower() in ['normal', 'nor']:
                            # Add normal map node
                            normal_map = nodes.new(type='ShaderNodeNormalMap')
                            normal_map.location = (x_pos + 200, y_pos)
                            links.new(tex_node.outputs['Color'], normal_map.inputs['Color'])
                            links.new(normal_map.outputs['Normal'], principled.inputs['Normal'])
                        elif map_type in ['displacement', 'disp', 'height']:
                            # Add displacement node
                            disp_node = nodes.new(type='ShaderNodeDisplacement')
                            disp_node.location = (x_pos + 200, y_pos - 200)
                            links.new(tex_node.outputs['Color'], disp_node.inputs['Height'])
                            links.new(disp_node.outputs['Displacement'], output.inputs['Displacement'])

                        y_pos -= 250

                    return {
                        "success": True,
                        "message": f"Texture {asset_id} imported as material",
                        "material": mat.name,
                        "maps": list(downloaded_maps.keys())
                    }

                except Exception as e:
                    return {"error": f"Failed to process textures: {str(e)}"}

            elif asset_type == "models":
                # For models, prefer glTF format if available
                if not file_format:
                    file_format = "gltf"  # Default format for models

                if file_format in files_data and resolution in files_data[file_format]:
                    file_info = files_data[file_format][resolution][file_format]
                    file_url = file_info["url"]

                    # Create a temporary directory to store the model and its dependencies
                    temp_dir = tempfile.mkdtemp()
                    main_file_path = ""

                    try:
                        # Download the main model file
                        main_file_name = file_url.split("/")[-1]
                        main_file_path = os.path.join(temp_dir, main_file_name)

                        response = requests.get(file_url, headers=REQ_HEADERS)
                        if response.status_code != 200:
                            return {"error": f"Failed to download model: {response.status_code}"}

                        with open(main_file_path, "wb") as f:
                            f.write(response.content)

                        # Check for included files and download them
                        if "include" in file_info and file_info["include"]:
                            for include_path, include_info in file_info["include"].items():
                                # Get the URL for the included file - this is the fix
                                include_url = include_info["url"]

                                # Validate include_path — the API response controls these
                                # dict keys; a malicious or MITM'd response could request an
                                # absolute path or one containing ".." to escape temp_dir
                                # and write arbitrary files (e.g. ~/.bashrc, authorized_keys).
                                # Mirrors the zip-slip check in download_sketchfab_model.
                                target_path = os.path.join(temp_dir, os.path.normpath(include_path))
                                abs_temp_dir = os.path.abspath(temp_dir)
                                abs_target_path = os.path.abspath(target_path)
                                if (os.path.isabs(include_path)
                                        or ".." in include_path
                                        or not abs_target_path.startswith(abs_temp_dir + os.sep)):
                                    print(f"Skipping include with unsafe path: {include_path}")
                                    continue

                                # Create the directory structure for the included file
                                include_file_path = target_path
                                os.makedirs(os.path.dirname(include_file_path), exist_ok=True)

                                # Download the included file
                                include_response = requests.get(include_url, headers=REQ_HEADERS)
                                if include_response.status_code == 200:
                                    with open(include_file_path, "wb") as f:
                                        f.write(include_response.content)
                                else:
                                    print(f"Failed to download included file: {include_path}")

                        # Import the model into Blender
                        if file_format == "gltf" or file_format == "glb":
                            bpy.ops.import_scene.gltf(filepath=main_file_path)
                        elif file_format == "fbx":
                            bpy.ops.import_scene.fbx(filepath=main_file_path)
                        elif file_format == "obj":
                            bpy.ops.import_scene.obj(filepath=main_file_path)
                        elif file_format == "blend":
                            # For blend files, we need to append or link
                            with bpy.data.libraries.load(main_file_path, link=False) as (data_from, data_to):
                                data_to.objects = data_from.objects

                            # Link the objects to the scene
                            for obj in data_to.objects:
                                if obj is not None:
                                    bpy.context.collection.objects.link(obj)
                        else:
                            return {"error": f"Unsupported model format: {file_format}"}

                        # Get the names of imported objects
                        imported_objects = [obj.name for obj in bpy.context.selected_objects]

                        return {
                            "success": True,
                            "message": f"Model {asset_id} imported successfully",
                            "imported_objects": imported_objects
                        }
                    except Exception as e:
                        return {"error": f"Failed to import model: {str(e)}"}
                    finally:
                        # Clean up temporary directory
                        with suppress(Exception):
                            shutil.rmtree(temp_dir)
                else:
                    return {"error": f"Requested format or resolution not available for this model"}

            else:
                return {"error": f"Unsupported asset type: {asset_type}"}

        except Exception as e:
            return {"error": f"Failed to download asset: {str(e)}"}

    def set_texture(self, object_name, texture_id):
        """Apply a previously downloaded Polyhaven texture to an object by creating a new material"""
        try:
            # Get the object
            obj = bpy.data.objects.get(object_name)
            if not obj:
                return {"error": f"Object not found: {object_name}"}

            # Make sure object can accept materials
            if not hasattr(obj, 'data') or not hasattr(obj.data, 'materials'):
                return {"error": f"Object {object_name} cannot accept materials"}

            # Find all images related to this texture and ensure they're properly loaded
            texture_images = {}
            for img in bpy.data.images:
                if img.name.startswith(texture_id + "_"):
                    # Extract the map type from the image name
                    map_type = img.name.split('_')[-1].split('.')[0]

                    # Force a reload of the image
                    img.reload()

                    # Ensure proper color space
                    if map_type.lower() in ['color', 'diffuse', 'albedo']:
                        try:
                            img.colorspace_settings.name = 'sRGB'
                        except:
                            pass
                    else:
                        try:
                            img.colorspace_settings.name = 'Non-Color'
                        except:
                            pass

                    # Ensure the image is packed
                    if not img.packed_file:
                        img.pack()

                    texture_images[map_type] = img
                    print(f"Loaded texture map: {map_type} - {img.name}")

                    # Debug info
                    print(f"Image size: {img.size[0]}x{img.size[1]}")
                    print(f"Color space: {img.colorspace_settings.name}")
                    print(f"File format: {img.file_format}")
                    print(f"Is packed: {bool(img.packed_file)}")

            if not texture_images:
                return {"error": f"No texture images found for: {texture_id}. Please download the texture first."}

            # Create a new material
            new_mat_name = f"{texture_id}_material_{object_name}"

            # Remove any existing material with this name to avoid conflicts
            existing_mat = bpy.data.materials.get(new_mat_name)
            if existing_mat:
                bpy.data.materials.remove(existing_mat)

            new_mat = bpy.data.materials.new(name=new_mat_name)
            new_mat.use_nodes = True

            # Set up the material nodes
            nodes = new_mat.node_tree.nodes
            links = new_mat.node_tree.links

            # Clear default nodes
            nodes.clear()

            # Create output node
            output = nodes.new(type='ShaderNodeOutputMaterial')
            output.location = (600, 0)

            # Create principled BSDF node
            principled = nodes.new(type='ShaderNodeBsdfPrincipled')
            principled.location = (300, 0)
            links.new(principled.outputs[0], output.inputs[0])

            # Add texture nodes based on available maps
            tex_coord = nodes.new(type='ShaderNodeTexCoord')
            tex_coord.location = (-800, 0)

            mapping = nodes.new(type='ShaderNodeMapping')
            mapping.location = (-600, 0)
            mapping.vector_type = 'TEXTURE'  # Changed from default 'POINT' to 'TEXTURE'
            links.new(tex_coord.outputs['UV'], mapping.inputs['Vector'])

            # Position offset for texture nodes
            x_pos = -400
            y_pos = 300

            # Connect different texture maps
            for map_type, image in texture_images.items():
                tex_node = nodes.new(type='ShaderNodeTexImage')
                tex_node.location = (x_pos, y_pos)
                tex_node.image = image

                # Set color space based on map type
                if map_type.lower() in ['color', 'diffuse', 'albedo']:
                    try:
                        tex_node.image.colorspace_settings.name = 'sRGB'
                    except:
                        pass  # Use default if sRGB not available
                else:
                    try:
                        tex_node.image.colorspace_settings.name = 'Non-Color'
                    except:
                        pass  # Use default if Non-Color not available

                links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])

                # Connect to appropriate input on Principled BSDF
                if map_type.lower() in ['color', 'diffuse', 'albedo']:
                    links.new(tex_node.outputs['Color'], principled.inputs['Base Color'])
                elif map_type.lower() in ['roughness', 'rough']:
                    links.new(tex_node.outputs['Color'], principled.inputs['Roughness'])
                elif map_type.lower() in ['metallic', 'metalness', 'metal']:
                    links.new(tex_node.outputs['Color'], principled.inputs['Metallic'])
                elif map_type.lower() in ['normal', 'nor', 'dx', 'gl']:
                    # Add normal map node
                    normal_map = nodes.new(type='ShaderNodeNormalMap')
                    normal_map.location = (x_pos + 200, y_pos)
                    links.new(tex_node.outputs['Color'], normal_map.inputs['Color'])
                    links.new(normal_map.outputs['Normal'], principled.inputs['Normal'])
                elif map_type.lower() in ['displacement', 'disp', 'height']:
                    # Add displacement node
                    disp_node = nodes.new(type='ShaderNodeDisplacement')
                    disp_node.location = (x_pos + 200, y_pos - 200)
                    disp_node.inputs['Scale'].default_value = 0.1  # Reduce displacement strength
                    links.new(tex_node.outputs['Color'], disp_node.inputs['Height'])
                    links.new(disp_node.outputs['Displacement'], output.inputs['Displacement'])

                y_pos -= 250

            # Second pass: Connect nodes with proper handling for special cases
            texture_nodes = {}

            # First find all texture nodes and store them by map type
            for node in nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    for map_type, image in texture_images.items():
                        if node.image == image:
                            texture_nodes[map_type] = node
                            break

            # Now connect everything using the nodes instead of images
            # Handle base color (diffuse)
            for map_name in ['color', 'diffuse', 'albedo']:
                if map_name in texture_nodes:
                    links.new(texture_nodes[map_name].outputs['Color'], principled.inputs['Base Color'])
                    print(f"Connected {map_name} to Base Color")
                    break

            # Handle roughness
            for map_name in ['roughness', 'rough']:
                if map_name in texture_nodes:
                    links.new(texture_nodes[map_name].outputs['Color'], principled.inputs['Roughness'])
                    print(f"Connected {map_name} to Roughness")
                    break

            # Handle metallic
            for map_name in ['metallic', 'metalness', 'metal']:
                if map_name in texture_nodes:
                    links.new(texture_nodes[map_name].outputs['Color'], principled.inputs['Metallic'])
                    print(f"Connected {map_name} to Metallic")
                    break

            # Handle normal maps
            for map_name in ['gl', 'dx', 'nor']:
                if map_name in texture_nodes:
                    normal_map_node = nodes.new(type='ShaderNodeNormalMap')
                    normal_map_node.location = (100, 100)
                    links.new(texture_nodes[map_name].outputs['Color'], normal_map_node.inputs['Color'])
                    links.new(normal_map_node.outputs['Normal'], principled.inputs['Normal'])
                    print(f"Connected {map_name} to Normal")
                    break

            # Handle displacement
            for map_name in ['displacement', 'disp', 'height']:
                if map_name in texture_nodes:
                    disp_node = nodes.new(type='ShaderNodeDisplacement')
                    disp_node.location = (300, -200)
                    disp_node.inputs['Scale'].default_value = 0.1  # Reduce displacement strength
                    links.new(texture_nodes[map_name].outputs['Color'], disp_node.inputs['Height'])
                    links.new(disp_node.outputs['Displacement'], output.inputs['Displacement'])
                    print(f"Connected {map_name} to Displacement")
                    break

            # Handle ARM texture (Ambient Occlusion, Roughness, Metallic)
            if 'arm' in texture_nodes:
                # Blender 4.0 removed ShaderNodeSeparateRGB (renamed to
                # ShaderNodeSeparateColor, added in 3.3). Branch on the running
                # Blender version so pre-4.0 behavior is untouched.
                if bpy.app.version >= (4, 0):
                    sep = nodes.new(type='ShaderNodeSeparateColor')  # defaults to mode='RGB'
                    in_socket, ch_r, ch_g, ch_b = 'Color', 'Red', 'Green', 'Blue'
                else:
                    sep = nodes.new(type='ShaderNodeSeparateRGB')
                    in_socket, ch_r, ch_g, ch_b = 'Image', 'R', 'G', 'B'
                sep.location = (-200, -100)
                links.new(texture_nodes['arm'].outputs['Color'], sep.inputs[in_socket])

                # Connect Roughness (G) if no dedicated roughness map
                if not any(map_name in texture_nodes for map_name in ['roughness', 'rough']):
                    links.new(sep.outputs[ch_g], principled.inputs['Roughness'])
                    print("Connected ARM.G to Roughness")

                # Connect Metallic (B) if no dedicated metallic map
                if not any(map_name in texture_nodes for map_name in ['metallic', 'metalness', 'metal']):
                    links.new(sep.outputs[ch_b], principled.inputs['Metallic'])
                    print("Connected ARM.B to Metallic")

                # For AO (R channel), multiply with base color if we have one
                base_color_node = None
                for map_name in ['color', 'diffuse', 'albedo']:
                    if map_name in texture_nodes:
                        base_color_node = texture_nodes[map_name]
                        break

                if base_color_node:
                    mix_node = nodes.new(type='ShaderNodeMixRGB')
                    mix_node.location = (100, 200)
                    mix_node.blend_type = 'MULTIPLY'
                    mix_node.inputs['Fac'].default_value = 0.8  # 80% influence

                    # Disconnect direct connection to base color
                    for link in base_color_node.outputs['Color'].links:
                        if link.to_socket == principled.inputs['Base Color']:
                            links.remove(link)

                    # Connect through the mix node
                    links.new(base_color_node.outputs['Color'], mix_node.inputs[1])
                    links.new(sep.outputs[ch_r], mix_node.inputs[2])
                    links.new(mix_node.outputs['Color'], principled.inputs['Base Color'])
                    print("Connected ARM.R to AO mix with Base Color")

            # Handle AO (Ambient Occlusion) if separate
            if 'ao' in texture_nodes:
                base_color_node = None
                for map_name in ['color', 'diffuse', 'albedo']:
                    if map_name in texture_nodes:
                        base_color_node = texture_nodes[map_name]
                        break

                if base_color_node:
                    mix_node = nodes.new(type='ShaderNodeMixRGB')
                    mix_node.location = (100, 200)
                    mix_node.blend_type = 'MULTIPLY'
                    mix_node.inputs['Fac'].default_value = 0.8  # 80% influence

                    # Disconnect direct connection to base color
                    for link in base_color_node.outputs['Color'].links:
                        if link.to_socket == principled.inputs['Base Color']:
                            links.remove(link)

                    # Connect through the mix node
                    links.new(base_color_node.outputs['Color'], mix_node.inputs[1])
                    links.new(texture_nodes['ao'].outputs['Color'], mix_node.inputs[2])
                    links.new(mix_node.outputs['Color'], principled.inputs['Base Color'])
                    print("Connected AO to mix with Base Color")

            # CRITICAL: Make sure to clear all existing materials from the object
            while len(obj.data.materials) > 0:
                obj.data.materials.pop(index=0)

            # Assign the new material to the object
            obj.data.materials.append(new_mat)

            # CRITICAL: Make the object active and select it
            bpy.context.view_layer.objects.active = obj
            obj.select_set(True)

            # CRITICAL: Force Blender to update the material
            bpy.context.view_layer.update()

            # Get the list of texture maps
            texture_maps = list(texture_images.keys())

            # Get info about texture nodes for debugging
            material_info = {
                "name": new_mat.name,
                "has_nodes": new_mat.use_nodes,
                "node_count": len(new_mat.node_tree.nodes),
                "texture_nodes": []
            }

            for node in new_mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    connections = []
                    for output in node.outputs:
                        for link in output.links:
                            connections.append(f"{output.name} → {link.to_node.name}.{link.to_socket.name}")

                    material_info["texture_nodes"].append({
                        "name": node.name,
                        "image": node.image.name,
                        "colorspace": node.image.colorspace_settings.name,
                        "connections": connections
                    })

            return {
                "success": True,
                "message": f"Created new material and applied texture {texture_id} to {object_name}",
                "material": new_mat.name,
                "maps": texture_maps,
                "material_info": material_info
            }

        except Exception as e:
            print(f"Error in set_texture: {str(e)}")
            traceback.print_exc()
            return {"error": f"Failed to apply texture: {str(e)}"}

    def get_telemetry_consent(self):
        """Get the current telemetry consent status.

        Fails closed: if preferences cannot be read we report no consent. Not
        being able to read the preference means we do not know the user's
        answer, which is not the same as them having said yes.
        """
        try:
            # Get addon preferences - use the module name
            addon_prefs = bpy.context.preferences.addons.get(__name__)
            if addon_prefs:
                consent = bool(addon_prefs.preferences.telemetry_consent)
            else:
                consent = False
        except (AttributeError, KeyError):
            consent = False
        return {"consent": consent}

    def set_telemetry_consent(self, consent=False):
        """Write the telemetry consent preference.

        Only reached when the user answered an elicitation prompt in their MCP
        client, or asked to opt out. Assigning the property in code skips the
        BoolProperty update= callback, so the manual-edit handlers are
        re-synced explicitly.
        """
        try:
            addon_prefs = bpy.context.preferences.addons.get(__name__)
            if not addon_prefs:
                return {"error": "Could not read addon preferences"}
            addon_prefs.preferences.telemetry_consent = bool(consent)
        except (AttributeError, KeyError) as e:
            return {"error": f"Could not set telemetry consent: {e}"}

        try:
            sync_edit_capture_handlers()
        except Exception as e:
            print(f"BlenderMCP: could not sync manual edit handlers: {e}")

        return {"consent": bool(consent)}

    def get_polyhaven_status(self):
        """Get the current status of PolyHaven integration"""
        enabled = bpy.context.scene.blendermcp_use_polyhaven
        if enabled:
            return {"enabled": True, "message": "PolyHaven integration is enabled and ready to use."}
        else:
            return {
                "enabled": False,
                "message": """PolyHaven integration is currently disabled. To enable it:
                            1. In the 3D Viewport, find the MCP for Blender panel in the sidebar (press N if hidden)
                            2. Check the 'Use assets from Poly Haven' checkbox
                            3. Restart the connection to Claude"""
        }

    #region Hyper3D
    def get_hyper3d_status(self):
        """Get the current status of Hyper3D Rodin integration"""
        enabled = bpy.context.scene.blendermcp_use_hyper3d
        hyper3d_api_key = self._get_hyper3d_api_key()
        if enabled:
            if not hyper3d_api_key:
                return {
                    "enabled": False,
                    "message": """Hyper3D Rodin integration is currently enabled, but API key is not given. To enable it:
                                1. In the 3D Viewport, find the MCP for Blender panel in the sidebar (press N if hidden)
                                2. Keep the 'Use Hyper3D Rodin 3D model generation' checkbox checked
                                3. Choose the right plaform and fill in the API Key
                                4. Restart the connection to Claude"""
                }
            mode = bpy.context.scene.blendermcp_hyper3d_mode
            message = f"Hyper3D Rodin integration is enabled and ready to use. Mode: {mode}. " + \
                f"Key type: {'private' if hyper3d_api_key != RODIN_FREE_TRIAL_KEY else 'free_trial'}"
            return {
                "enabled": True,
                "message": message
            }
        else:
            return {
                "enabled": False,
                "message": """Hyper3D Rodin integration is currently disabled. To enable it:
                            1. In the 3D Viewport, find the MCP for Blender panel in the sidebar (press N if hidden)
                            2. Check the 'Use Hyper3D Rodin 3D model generation' checkbox
                            3. Restart the connection to Claude"""
            }

    def create_rodin_job(self, *args, **kwargs):
        match bpy.context.scene.blendermcp_hyper3d_mode:
            case "MAIN_SITE":
                return self.create_rodin_job_main_site(*args, **kwargs)
            case "FAL_AI":
                return self.create_rodin_job_fal_ai(*args, **kwargs)
            case _:
                return f"Error: Unknown Hyper3D Rodin mode!"

    def create_rodin_job_main_site(
            self,
            text_prompt: str=None,
            images: list[tuple[str, str]]=None,
            bbox_condition=None
        ):
        try:
            api_key = self._get_hyper3d_api_key()
            if not api_key:
                return {"error": "Hyper3D API key is not given"}
            if images is None:
                images = []
            """Call Rodin API, get the job uuid and subscription key"""
            files = [
                *[("images", (f"{i:04d}{img_suffix}", base64.b64decode(img) if isinstance(img, str) else img)) for i, (img_suffix, img) in enumerate(images)],
                ("tier", (None, "Sketch")),
                ("mesh_mode", (None, "Raw")),
                ("texture_mode", (None, "high")),
            ]
            if text_prompt:
                files.append(("prompt", (None, text_prompt)))
            if bbox_condition:
                files.append(("bbox_condition", (None, json.dumps(bbox_condition))))
            response = requests.post(
                "https://hyperhuman.deemos.com/api/v2/rodin",
                headers={
                    "Authorization": f"Bearer {api_key}",
                },
                files=files
            )
            data = response.json()
            return data
        except Exception as e:
            return {"error": str(e)}

    def create_rodin_job_fal_ai(
            self,
            text_prompt: str=None,
            images: list[tuple[str, str]]=None,
            bbox_condition=None
        ):
        try:
            api_key = self._get_hyper3d_api_key()
            if not api_key:
                return {"error": "Hyper3D API key is not given"}
            req_data = {
                "tier": "Sketch",
            }
            if images:
                req_data["input_image_urls"] = images
            if text_prompt:
                req_data["prompt"] = text_prompt
            if bbox_condition:
                req_data["bbox_condition"] = bbox_condition
            response = requests.post(
                "https://queue.fal.run/fal-ai/hyper3d/rodin",
                headers={
                    "Authorization": f"Key {api_key}",
                    "Content-Type": "application/json",
                },
                json=req_data
            )
            data = response.json()
            return data
        except Exception as e:
            return {"error": str(e)}

    def poll_rodin_job_status(self, *args, **kwargs):
        match bpy.context.scene.blendermcp_hyper3d_mode:
            case "MAIN_SITE":
                return self.poll_rodin_job_status_main_site(*args, **kwargs)
            case "FAL_AI":
                return self.poll_rodin_job_status_fal_ai(*args, **kwargs)
            case _:
                return f"Error: Unknown Hyper3D Rodin mode!"

    def poll_rodin_job_status_main_site(self, subscription_key: str):
        """Call the job status API to get the job status"""
        api_key = self._get_hyper3d_api_key()
        if not api_key:
            return {"error": "Hyper3D API key is not given"}
        response = requests.post(
            "https://hyperhuman.deemos.com/api/v2/status",
            headers={
                "Authorization": f"Bearer {api_key}",
            },
            json={
                "subscription_key": subscription_key,
            },
        )
        data = response.json()
        return {
            "status_list": [i["status"] for i in data["jobs"]]
        }

    def poll_rodin_job_status_fal_ai(self, request_id: str):
        """Call the job status API to get the job status"""
        api_key = self._get_hyper3d_api_key()
        if not api_key:
            return {"error": "Hyper3D API key is not given"}
        response = requests.get(
            f"https://queue.fal.run/fal-ai/hyper3d/requests/{request_id}/status",
            headers={
                "Authorization": f"KEY {api_key}",
            },
        )
        data = response.json()
        return data

    @staticmethod
    def _clean_imported_glb(filepath, mesh_name=None):
        # Get the set of existing objects before import
        existing_objects = set(bpy.data.objects)

        # Import the GLB file
        bpy.ops.import_scene.gltf(filepath=filepath)

        # Ensure the context is updated
        bpy.context.view_layer.update()

        # Get all imported objects
        imported_objects = list(set(bpy.data.objects) - existing_objects)
        # imported_objects = [obj for obj in bpy.context.view_layer.objects if obj.select_get()]

        if not imported_objects:
            print("Error: No objects were imported.")
            return

        # Identify the mesh object
        mesh_obj = None

        if len(imported_objects) == 1 and imported_objects[0].type == 'MESH':
            mesh_obj = imported_objects[0]
            print("Single mesh imported, no cleanup needed.")
        else:
            if len(imported_objects) == 2:
                empty_objs = [i for i in imported_objects if i.type == "EMPTY"]
                if len(empty_objs) != 1:
                    print("Error: Expected an empty node with one mesh child or a single mesh object.")
                    return
                parent_obj = empty_objs.pop()
                if len(parent_obj.children) == 1:
                    potential_mesh = parent_obj.children[0]
                    if potential_mesh.type == 'MESH':
                        print("GLB structure confirmed: Empty node with one mesh child.")

                        # Unparent the mesh from the empty node
                        potential_mesh.parent = None

                        # Remove the empty node
                        bpy.data.objects.remove(parent_obj)
                        print("Removed empty node, keeping only the mesh.")

                        mesh_obj = potential_mesh
                    else:
                        print("Error: Child is not a mesh object.")
                        return
                else:
                    print("Error: Expected an empty node with one mesh child or a single mesh object.")
                    return
            else:
                print("Error: Expected an empty node with one mesh child or a single mesh object.")
                return

        # Rename the mesh if needed
        try:
            if mesh_obj and mesh_obj.name is not None and mesh_name:
                mesh_obj.name = mesh_name
                if mesh_obj.data.name is not None:
                    mesh_obj.data.name = mesh_name
                print(f"Mesh renamed to: {mesh_name}")
        except Exception as e:
            print("Having issue with renaming, give up renaming.")

        return mesh_obj

    def import_generated_asset(self, *args, **kwargs):
        match bpy.context.scene.blendermcp_hyper3d_mode:
            case "MAIN_SITE":
                return self.import_generated_asset_main_site(*args, **kwargs)
            case "FAL_AI":
                return self.import_generated_asset_fal_ai(*args, **kwargs)
            case _:
                return f"Error: Unknown Hyper3D Rodin mode!"

    def import_generated_asset_main_site(self, task_uuid: str, name: str):
        """Fetch the generated asset, import into blender"""
        api_key = self._get_hyper3d_api_key()
        if not api_key:
            return {"succeed": False, "error": "Hyper3D API key is not given"}
        response = requests.post(
            "https://hyperhuman.deemos.com/api/v2/download",
            headers={
                "Authorization": f"Bearer {api_key}",
            },
            json={
                'task_uuid': task_uuid
            }
        )
        data_ = response.json()
        temp_file = None
        for i in data_["list"]:
            if i["name"].endswith(".glb"):
                temp_file = tempfile.NamedTemporaryFile(
                    delete=False,
                    prefix=task_uuid,
                    suffix=".glb",
                )

                try:
                    # Download the content
                    response = requests.get(i["url"], stream=True)
                    response.raise_for_status()  # Raise an exception for HTTP errors

                    # Write the content to the temporary file
                    for chunk in response.iter_content(chunk_size=8192):
                        temp_file.write(chunk)

                    # Close the file
                    temp_file.close()

                except Exception as e:
                    # Clean up the file if there's an error
                    temp_file.close()
                    os.unlink(temp_file.name)
                    return {"succeed": False, "error": str(e)}

                break
        else:
            return {"succeed": False, "error": "Generation failed. Please first make sure that all jobs of the task are done and then try again later."}

        try:
            obj = self._clean_imported_glb(
                filepath=temp_file.name,
                mesh_name=name
            )
            result = {
                "name": obj.name,
                "type": obj.type,
                "location": [obj.location.x, obj.location.y, obj.location.z],
                "rotation": [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z],
                "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            }

            if obj.type == "MESH":
                bounding_box = self._get_aabb(obj)
                result["world_bounding_box"] = bounding_box

            return {
                "succeed": True, **result
            }
        except Exception as e:
            return {"succeed": False, "error": str(e)}

    def import_generated_asset_fal_ai(self, request_id: str, name: str):
        """Fetch the generated asset, import into blender"""
        api_key = self._get_hyper3d_api_key()
        if not api_key:
            return {"succeed": False, "error": "Hyper3D API key is not given"}
        response = requests.get(
            f"https://queue.fal.run/fal-ai/hyper3d/requests/{request_id}",
            headers={
                "Authorization": f"Key {api_key}",
            }
        )
        data_ = response.json()
        temp_file = None

        temp_file = tempfile.NamedTemporaryFile(
            delete=False,
            prefix=request_id,
            suffix=".glb",
        )

        try:
            # Download the content
            response = requests.get(data_["model_mesh"]["url"], stream=True)
            response.raise_for_status()  # Raise an exception for HTTP errors

            # Write the content to the temporary file
            for chunk in response.iter_content(chunk_size=8192):
                temp_file.write(chunk)

            # Close the file
            temp_file.close()

        except Exception as e:
            # Clean up the file if there's an error
            temp_file.close()
            os.unlink(temp_file.name)
            return {"succeed": False, "error": str(e)}

        try:
            obj = self._clean_imported_glb(
                filepath=temp_file.name,
                mesh_name=name
            )
            result = {
                "name": obj.name,
                "type": obj.type,
                "location": [obj.location.x, obj.location.y, obj.location.z],
                "rotation": [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z],
                "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            }

            if obj.type == "MESH":
                bounding_box = self._get_aabb(obj)
                result["world_bounding_box"] = bounding_box

            return {
                "succeed": True, **result
            }
        except Exception as e:
            return {"succeed": False, "error": str(e)}
    #endregion
 
    #region Sketchfab API
    def get_sketchfab_status(self):
        """Get the current status of Sketchfab integration"""
        enabled = bpy.context.scene.blendermcp_use_sketchfab
        api_key = self._get_sketchfab_api_key()

        # Test the API key if present
        if api_key and enabled:
            try:
                headers = {
                    "Authorization": f"Token {api_key}"
                }

                response = requests.get(
                    "https://api.sketchfab.com/v3/me",
                    headers=headers,
                    timeout=30  # Add timeout of 30 seconds
                )

                if response.status_code == 200:
                    user_data = response.json()
                    username = user_data.get("username", "Unknown user")
                    return {
                        "enabled": True,
                        "message": f"Sketchfab integration is enabled and ready to use. Logged in as: {username}"
                    }
                else:
                    return {
                        "enabled": False,
                        "message": f"Sketchfab API key seems invalid. Status code: {response.status_code}"
                    }
            except requests.exceptions.Timeout:
                return {
                    "enabled": False,
                    "message": "Timeout connecting to Sketchfab API. Check your internet connection."
                }
            except Exception as e:
                return {
                    "enabled": False,
                    "message": f"Error testing Sketchfab API key: {str(e)}"
                }

        if enabled and api_key:
            return {"enabled": True, "message": "Sketchfab integration is enabled and ready to use."}
        elif enabled and not api_key:
            return {
                "enabled": False,
                "message": """Sketchfab integration is currently enabled, but API key is not given. To enable it:
                            1. In the 3D Viewport, find the MCP for Blender panel in the sidebar (press N if hidden)
                            2. Keep the 'Use Sketchfab' checkbox checked
                            3. Enter your Sketchfab API Key
                            4. Restart the connection to Claude"""
            }
        else:
            return {
                "enabled": False,
                "message": """Sketchfab integration is currently disabled. To enable it:
                            1. In the 3D Viewport, find the MCP for Blender panel in the sidebar (press N if hidden)
                            2. Check the 'Use assets from Sketchfab' checkbox
                            3. Enter your Sketchfab API Key
                            4. Restart the connection to Claude"""
            }

    def search_sketchfab_models(self, query, categories=None, count=20, downloadable=True):
        """Search for models on Sketchfab based on query and optional filters"""
        try:
            api_key = self._get_sketchfab_api_key()
            if not api_key:
                return {"error": "Sketchfab API key is not configured"}

            # Build search parameters with exact fields from Sketchfab API docs
            params = {
                "type": "models",
                "q": query,
                "count": count,
                "downloadable": downloadable,
                "archives_flavours": False
            }

            if categories:
                params["categories"] = categories

            # Make API request to Sketchfab search endpoint
            # The proper format according to Sketchfab API docs for API key auth
            headers = {
                "Authorization": f"Token {api_key}"
            }


            # Use the search endpoint as specified in the API documentation
            response = requests.get(
                "https://api.sketchfab.com/v3/search",
                headers=headers,
                params=params,
                timeout=30  # Add timeout of 30 seconds
            )

            if response.status_code == 401:
                return {"error": "Authentication failed (401). Check your API key."}

            if response.status_code != 200:
                return {"error": f"API request failed with status code {response.status_code}"}

            response_data = response.json()

            # Safety check on the response structure
            if response_data is None:
                return {"error": "Received empty response from Sketchfab API"}

            # Handle 'results' potentially missing from response
            results = response_data.get("results", [])
            if not isinstance(results, list):
                return {"error": f"Unexpected response format from Sketchfab API: {response_data}"}

            return response_data

        except requests.exceptions.Timeout:
            return {"error": "Request timed out. Check your internet connection."}
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON response from Sketchfab API: {str(e)}"}
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"error": str(e)}

    def get_sketchfab_model_preview(self, uid):
        """Get thumbnail preview image of a Sketchfab model by its UID"""
        try:
            import base64
            
            api_key = self._get_sketchfab_api_key()
            if not api_key:
                return {"error": "Sketchfab API key is not configured"}

            headers = {"Authorization": f"Token {api_key}"}
            
            # Get model info which includes thumbnails
            response = requests.get(
                f"https://api.sketchfab.com/v3/models/{uid}",
                headers=headers,
                timeout=30
            )
            
            if response.status_code == 401:
                return {"error": "Authentication failed (401). Check your API key."}
            
            if response.status_code == 404:
                return {"error": f"Model not found: {uid}"}
            
            if response.status_code != 200:
                return {"error": f"Failed to get model info: {response.status_code}"}
            
            data = response.json()
            thumbnails = data.get("thumbnails", {}).get("images", [])
            
            if not thumbnails:
                return {"error": "No thumbnail available for this model"}
            
            # Find a suitable thumbnail (prefer medium size ~640px)
            selected_thumbnail = None
            for thumb in thumbnails:
                width = thumb.get("width", 0)
                if 400 <= width <= 800:
                    selected_thumbnail = thumb
                    break
            
            # Fallback to the first available thumbnail
            if not selected_thumbnail:
                selected_thumbnail = thumbnails[0]
            
            thumbnail_url = selected_thumbnail.get("url")
            if not thumbnail_url:
                return {"error": "Thumbnail URL not found"}
            
            # Download the thumbnail image
            img_response = requests.get(thumbnail_url, timeout=30)
            if img_response.status_code != 200:
                return {"error": f"Failed to download thumbnail: {img_response.status_code}"}
            
            # Encode image as base64
            image_data = base64.b64encode(img_response.content).decode('ascii')
            
            # Determine format from content type or URL
            content_type = img_response.headers.get("Content-Type", "")
            if "png" in content_type or thumbnail_url.endswith(".png"):
                img_format = "png"
            else:
                img_format = "jpeg"
            
            # Get additional model info for context
            model_name = data.get("name", "Unknown")
            author = data.get("user", {}).get("username", "Unknown")
            
            return {
                "success": True,
                "image_data": image_data,
                "format": img_format,
                "model_name": model_name,
                "author": author,
                "uid": uid,
                "thumbnail_width": selected_thumbnail.get("width"),
                "thumbnail_height": selected_thumbnail.get("height")
            }
            
        except requests.exceptions.Timeout:
            return {"error": "Request timed out. Check your internet connection."}
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"error": f"Failed to get model preview: {str(e)}"}

    def download_sketchfab_model(self, uid, normalize_size=False, target_size=1.0):
        """Download a model from Sketchfab by its UID
        
        Parameters:
        - uid: The unique identifier of the Sketchfab model
        - normalize_size: If True, scale the model so its largest dimension equals target_size
        - target_size: The target size in Blender units (meters) for the largest dimension
        """
        try:
            api_key = self._get_sketchfab_api_key()
            if not api_key:
                return {"error": "Sketchfab API key is not configured"}

            # Use proper authorization header for API key auth
            headers = {
                "Authorization": f"Token {api_key}"
            }

            # Request download URL using the exact endpoint from the documentation
            download_endpoint = f"https://api.sketchfab.com/v3/models/{uid}/download"

            response = requests.get(
                download_endpoint,
                headers=headers,
                timeout=30  # Add timeout of 30 seconds
            )

            if response.status_code == 401:
                return {"error": "Authentication failed (401). Check your API key."}

            if response.status_code != 200:
                return {"error": f"Download request failed with status code {response.status_code}"}

            data = response.json()

            # Safety check for None data
            if data is None:
                return {"error": "Received empty response from Sketchfab API for download request"}

            # Extract download URL with safety checks
            gltf_data = data.get("gltf")
            if not gltf_data:
                return {"error": "No gltf download URL available for this model. Response: " + str(data)}

            download_url = gltf_data.get("url")
            if not download_url:
                return {"error": "No download URL available for this model. Make sure the model is downloadable and you have access."}

            # Download the model (already has timeout)
            model_response = requests.get(download_url, timeout=60)  # 60 second timeout

            if model_response.status_code != 200:
                return {"error": f"Model download failed with status code {model_response.status_code}"}

            # Save to temporary file
            temp_dir = tempfile.mkdtemp()
            zip_file_path = os.path.join(temp_dir, f"{uid}.zip")

            with open(zip_file_path, "wb") as f:
                f.write(model_response.content)

            # Extract the zip file with enhanced security
            with zipfile.ZipFile(zip_file_path, 'r') as zip_ref:
                # More secure zip slip prevention
                for file_info in zip_ref.infolist():
                    # Get the path of the file
                    file_path = file_info.filename

                    # Convert directory separators to the current OS style
                    # This handles both / and \ in zip entries
                    target_path = os.path.join(temp_dir, os.path.normpath(file_path))

                    # Get absolute paths for comparison
                    abs_temp_dir = os.path.abspath(temp_dir)
                    abs_target_path = os.path.abspath(target_path)

                    # Ensure the normalized path doesn't escape the target directory
                    if not abs_target_path.startswith(abs_temp_dir):
                        with suppress(Exception):
                            shutil.rmtree(temp_dir)
                        return {"error": "Security issue: Zip contains files with path traversal attempt"}

                    # Additional explicit check for directory traversal
                    if ".." in file_path:
                        with suppress(Exception):
                            shutil.rmtree(temp_dir)
                        return {"error": "Security issue: Zip contains files with directory traversal sequence"}

                # If all files passed security checks, extract them
                zip_ref.extractall(temp_dir)

            # Find the main glTF file
            gltf_files = [f for f in os.listdir(temp_dir) if f.endswith('.gltf') or f.endswith('.glb')]

            if not gltf_files:
                with suppress(Exception):
                    shutil.rmtree(temp_dir)
                return {"error": "No glTF file found in the downloaded model"}

            main_file = os.path.join(temp_dir, gltf_files[0])

            # Import the model
            bpy.ops.import_scene.gltf(filepath=main_file)

            # Get the imported objects
            imported_objects = list(bpy.context.selected_objects)
            imported_object_names = [obj.name for obj in imported_objects]

            # Clean up temporary files
            with suppress(Exception):
                shutil.rmtree(temp_dir)

            # Find root objects (objects without parents in the imported set)
            root_objects = [obj for obj in imported_objects if obj.parent is None]

            # Helper function to recursively get all mesh children
            def get_all_mesh_children(obj):
                """Recursively collect all mesh objects in the hierarchy"""
                meshes = []
                if obj.type == 'MESH':
                    meshes.append(obj)
                for child in obj.children:
                    meshes.extend(get_all_mesh_children(child))
                return meshes

            # Collect ALL meshes from the entire hierarchy (starting from roots)
            all_meshes = []
            for obj in root_objects:
                all_meshes.extend(get_all_mesh_children(obj))
            
            if all_meshes:
                # Calculate combined world bounding box for all meshes
                all_min = mathutils.Vector((float('inf'), float('inf'), float('inf')))
                all_max = mathutils.Vector((float('-inf'), float('-inf'), float('-inf')))
                
                for mesh_obj in all_meshes:
                    # Get world-space bounding box corners
                    for corner in mesh_obj.bound_box:
                        world_corner = mesh_obj.matrix_world @ mathutils.Vector(corner)
                        all_min.x = min(all_min.x, world_corner.x)
                        all_min.y = min(all_min.y, world_corner.y)
                        all_min.z = min(all_min.z, world_corner.z)
                        all_max.x = max(all_max.x, world_corner.x)
                        all_max.y = max(all_max.y, world_corner.y)
                        all_max.z = max(all_max.z, world_corner.z)
                
                # Calculate dimensions
                dimensions = [
                    all_max.x - all_min.x,
                    all_max.y - all_min.y,
                    all_max.z - all_min.z
                ]
                max_dimension = max(dimensions)
                
                # Apply normalization if requested
                scale_applied = 1.0
                if normalize_size and max_dimension > 0:
                    scale_factor = target_size / max_dimension
                    scale_applied = scale_factor
                    
                    # ✅ Only apply scale to ROOT objects (not children!)
                    # Child objects inherit parent's scale through matrix_world
                    for root in root_objects:
                        root.scale = (
                            root.scale.x * scale_factor,
                            root.scale.y * scale_factor,
                            root.scale.z * scale_factor
                        )
                    
                    # Update the scene to recalculate matrix_world for all objects
                    bpy.context.view_layer.update()
                    
                    # Recalculate bounding box after scaling
                    all_min = mathutils.Vector((float('inf'), float('inf'), float('inf')))
                    all_max = mathutils.Vector((float('-inf'), float('-inf'), float('-inf')))
                    
                    for mesh_obj in all_meshes:
                        for corner in mesh_obj.bound_box:
                            world_corner = mesh_obj.matrix_world @ mathutils.Vector(corner)
                            all_min.x = min(all_min.x, world_corner.x)
                            all_min.y = min(all_min.y, world_corner.y)
                            all_min.z = min(all_min.z, world_corner.z)
                            all_max.x = max(all_max.x, world_corner.x)
                            all_max.y = max(all_max.y, world_corner.y)
                            all_max.z = max(all_max.z, world_corner.z)
                    
                    dimensions = [
                        all_max.x - all_min.x,
                        all_max.y - all_min.y,
                        all_max.z - all_min.z
                    ]
                
                world_bounding_box = [[all_min.x, all_min.y, all_min.z], [all_max.x, all_max.y, all_max.z]]
            else:
                world_bounding_box = None
                dimensions = None
                scale_applied = 1.0

            result = {
                "success": True,
                "message": "Model imported successfully",
                "imported_objects": imported_object_names
            }
            
            if world_bounding_box:
                result["world_bounding_box"] = world_bounding_box
            if dimensions:
                result["dimensions"] = [round(d, 4) for d in dimensions]
            if normalize_size:
                result["scale_applied"] = round(scale_applied, 6)
                result["normalized"] = True
            
            return result

        except requests.exceptions.Timeout:
            return {"error": "Request timed out. Check your internet connection and try again with a simpler model."}
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON response from Sketchfab API: {str(e)}"}
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"error": f"Failed to download model: {str(e)}"}
    #endregion

    #region Poly Pizza API
    def get_polypizza_status(self):
        """Get the current status of Poly Pizza integration"""
        enabled = bpy.context.scene.blendermcp_use_polypizza
        api_key = self._get_polypizza_api_key()

        if enabled and api_key:
            return {
                "enabled": True,
                "message": "Poly Pizza integration is enabled and ready to use."
            }
        elif enabled and not api_key:
            return {
                "enabled": False,
                "message": """Poly Pizza integration is currently enabled, but API key is not given. To enable it:
                            1. Get a free API key at https://poly.pizza/settings/api
                            2. In the 3D Viewport, find the MCP for Blender panel in the sidebar (press N if hidden)
                            3. Keep the 'Use Poly Pizza' checkbox checked
                            4. Enter your Poly Pizza API Key
                            5. Restart the connection to Claude"""
            }
        else:
            return {
                "enabled": False,
                "message": """Poly Pizza integration is currently disabled. To enable it:
                            1. Get a free API key at https://poly.pizza/settings/api
                            2. In the 3D Viewport, find the MCP for Blender panel in the sidebar (press N if hidden)
                            3. Check the 'Use assets from Poly Pizza' checkbox
                            4. Enter your Poly Pizza API Key
                            5. Restart the connection to Claude"""
            }

    def search_polypizza_models(self, query=None, category=None, licence=None,
                                animated=False, limit=20, page=None):
        """Search for models on Poly Pizza by keyword and/or filters

        Parameters:
        - query: Keyword to search for. When omitted, at least one filter is
                 required: the bare /search endpoint answers 400 without one.
        - category: Numeric category id (0-11); the MCP server resolves names
        - licence: Numeric licence id (0 = CC-BY, 1 = CC0); the MCP server resolves names
        - animated: When True, return only animated models
        - limit: Maximum number of results to return (the API caps a page at 32)
        - page: Optional 0-based page number
        """
        try:
            api_key = self._get_polypizza_api_key()
            if not api_key:
                return {"error": "Poly Pizza API key is not configured"}

            try:
                filters = _polypizza_filter_params(category, licence, animated)
            except ValueError as e:
                return {"error": str(e)}

            keyword = (query or "").strip()
            if not keyword and not filters:
                return {"error": (
                    "Poly Pizza needs a search keyword or at least one filter "
                    "(category, licence, or animated=True). An unfiltered listing of the "
                    "whole catalogue is rejected by the API with HTTP 400."
                )}

            # Limit and Page are Capitalized like the filters: lowercase
            # variants are silently ignored and the API then serves its
            # default page of 32.
            params = dict(filters)
            params["Limit"] = max(1, min(int(limit), 32))
            if page is not None:
                params["Page"] = page

            headers = dict(REQ_HEADERS)
            headers["x-auth-token"] = api_key

            if keyword:
                url = f"{POLYPIZZA_API_BASE}/search/{quote(keyword, safe='')}"
            else:
                url = f"{POLYPIZZA_API_BASE}/search"

            response = requests.get(url, headers=headers, params=params, timeout=30)

            if response.status_code in (401, 403):
                return {"error": f"Poly Pizza authentication failed ({response.status_code}). Check your API key."}

            if response.status_code == 400:
                return {"error": (
                    "Poly Pizza rejected the search parameters (400). Category must be an id in "
                    "0-11 and licence 0 (CC-BY) or 1 (CC0)."
                )}

            if response.status_code == 429:
                return {"error": "Poly Pizza rate limit exceeded (100 requests/second). Try again in a moment."}

            if response.status_code != 200:
                return {"error": f"Poly Pizza API request failed with status code {response.status_code}"}

            response_data = response.json()

            if response_data is None:
                return {"error": "Received empty response from Poly Pizza API"}

            results = response_data.get("results", [])
            if not isinstance(results, list):
                return {"error": f"Unexpected response format from Poly Pizza API: {response_data}"}

            return {
                "total": response_data.get("total", len(results)),
                "results": [_polypizza_summarize_model(m) for m in results if isinstance(m, dict)],
                "filters_applied": filters,
            }

        except requests.exceptions.Timeout:
            return {"error": "Request timed out. Check your internet connection."}
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON response from Poly Pizza API: {str(e)}"}
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"error": str(e)}

    def download_polypizza_model(self, model_id, normalize_size=False, target_size=1.0):
        """Download a model from Poly Pizza by its ID

        Parameters:
        - model_id: The Poly Pizza model ID (from search_polypizza_models)
        - normalize_size: If True, scale the model so its largest dimension equals target_size
        - target_size: The target size in Blender units (meters) for the largest dimension
        """
        temp_dir = None
        try:
            api_key = self._get_polypizza_api_key()
            if not api_key:
                return {"error": "Poly Pizza API key is not configured"}

            headers = dict(REQ_HEADERS)
            headers["x-auth-token"] = api_key

            response = requests.get(
                f"{POLYPIZZA_API_BASE}/model/{quote(str(model_id), safe='')}",
                headers=headers,
                timeout=30
            )

            if response.status_code in (401, 403):
                return {"error": f"Poly Pizza authentication failed ({response.status_code}). Check your API key."}

            if response.status_code == 404:
                return {"error": f"No Poly Pizza model found with ID '{model_id}'"}

            if response.status_code != 200:
                return {"error": f"Poly Pizza model lookup failed with status code {response.status_code}"}

            model = response.json()

            if not isinstance(model, dict):
                return {"error": f"Unexpected response format from Poly Pizza API: {model}"}

            download_url = model.get("Download")
            if not download_url:
                return {"error": f"Poly Pizza model '{model_id}' has no downloadable GLB file"}

            # The CDN takes no API key and must never be sent one: it is a
            # separate host from the API.
            file_response = requests.get(download_url, headers=dict(REQ_HEADERS), timeout=60)

            cdn_error = _polypizza_cdn_error(
                file_response.status_code,
                getattr(file_response, "headers", None),
                file_response.content or b"",
            )
            if cdn_error:
                return {"error": cdn_error}

            # Every Poly Pizza model is a single self-contained .glb - no zip,
            # no sidecar textures - so it goes straight to disk and into glTF import.
            safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(model_id)) or "model"
            temp_dir = tempfile.mkdtemp()
            glb_path = os.path.join(temp_dir, f"{safe_id}.glb")

            with open(glb_path, "wb") as f:
                f.write(file_response.content)

            bpy.ops.import_scene.gltf(filepath=glb_path)

            # Get the imported objects
            imported_objects = list(bpy.context.selected_objects)
            imported_object_names = [obj.name for obj in imported_objects]

            # Clean up temporary files
            with suppress(Exception):
                shutil.rmtree(temp_dir)
            temp_dir = None

            # Find root objects (objects without parents in the imported set)
            root_objects = [obj for obj in imported_objects if obj.parent is None]

            # 69% of the catalogue is CC-BY, so the credit line has to outlive
            # the session. Custom properties are saved into the .blend.
            attribution = model.get("Attribution") or ""
            licence = model.get("Licence") or ""
            for root in root_objects:
                root["polypizza_attribution"] = attribution
                root["polypizza_id"] = model.get("ID") or str(model_id)
                root["polypizza_licence"] = licence

            # Helper function to recursively get all mesh children
            def get_all_mesh_children(obj):
                """Recursively collect all mesh objects in the hierarchy"""
                meshes = []
                if obj.type == 'MESH':
                    meshes.append(obj)
                for child in obj.children:
                    meshes.extend(get_all_mesh_children(child))
                return meshes

            # Collect ALL meshes from the entire hierarchy (starting from roots)
            all_meshes = []
            for obj in root_objects:
                all_meshes.extend(get_all_mesh_children(obj))

            if all_meshes:
                # Calculate combined world bounding box for all meshes
                all_min = mathutils.Vector((float('inf'), float('inf'), float('inf')))
                all_max = mathutils.Vector((float('-inf'), float('-inf'), float('-inf')))

                for mesh_obj in all_meshes:
                    # Get world-space bounding box corners
                    for corner in mesh_obj.bound_box:
                        world_corner = mesh_obj.matrix_world @ mathutils.Vector(corner)
                        all_min.x = min(all_min.x, world_corner.x)
                        all_min.y = min(all_min.y, world_corner.y)
                        all_min.z = min(all_min.z, world_corner.z)
                        all_max.x = max(all_max.x, world_corner.x)
                        all_max.y = max(all_max.y, world_corner.y)
                        all_max.z = max(all_max.z, world_corner.z)

                # Calculate dimensions
                dimensions = [
                    all_max.x - all_min.x,
                    all_max.y - all_min.y,
                    all_max.z - all_min.z
                ]
                max_dimension = max(dimensions)

                # Apply normalization if requested
                scale_applied = 1.0
                if normalize_size and max_dimension > 0:
                    scale_factor = target_size / max_dimension
                    scale_applied = scale_factor

                    # Only apply scale to ROOT objects (not children!)
                    # Child objects inherit parent's scale through matrix_world
                    for root in root_objects:
                        root.scale = (
                            root.scale.x * scale_factor,
                            root.scale.y * scale_factor,
                            root.scale.z * scale_factor
                        )

                    # Update the scene to recalculate matrix_world for all objects
                    bpy.context.view_layer.update()

                    # Recalculate bounding box after scaling
                    all_min = mathutils.Vector((float('inf'), float('inf'), float('inf')))
                    all_max = mathutils.Vector((float('-inf'), float('-inf'), float('-inf')))

                    for mesh_obj in all_meshes:
                        for corner in mesh_obj.bound_box:
                            world_corner = mesh_obj.matrix_world @ mathutils.Vector(corner)
                            all_min.x = min(all_min.x, world_corner.x)
                            all_min.y = min(all_min.y, world_corner.y)
                            all_min.z = min(all_min.z, world_corner.z)
                            all_max.x = max(all_max.x, world_corner.x)
                            all_max.y = max(all_max.y, world_corner.y)
                            all_max.z = max(all_max.z, world_corner.z)

                    dimensions = [
                        all_max.x - all_min.x,
                        all_max.y - all_min.y,
                        all_max.z - all_min.z
                    ]

                world_bounding_box = [[all_min.x, all_min.y, all_min.z], [all_max.x, all_max.y, all_max.z]]
            else:
                world_bounding_box = None
                dimensions = None
                scale_applied = 1.0

            result = {
                "success": True,
                "message": "Model imported successfully",
                "imported_objects": imported_object_names,
                "model_id": model.get("ID") or str(model_id),
                "title": model.get("Title"),
                "licence": licence,
                "attribution": attribution,
                "tri_count": model.get("Tri Count"),
            }

            if world_bounding_box:
                result["world_bounding_box"] = world_bounding_box
            if dimensions:
                result["dimensions"] = [round(d, 4) for d in dimensions]
            if normalize_size:
                result["scale_applied"] = round(scale_applied, 6)
                result["normalized"] = True

            return result

        except requests.exceptions.Timeout:
            return {"error": "Request timed out. Check your internet connection and try again."}
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON response from Poly Pizza API: {str(e)}"}
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"error": f"Failed to download model: {str(e)}"}
        finally:
            if temp_dir:
                with suppress(Exception):
                    shutil.rmtree(temp_dir)
    #endregion

    #region Hunyuan3D
    def get_hunyuan3d_status(self):
        """Get the current status of Hunyuan3D integration"""
        enabled = bpy.context.scene.blendermcp_use_hunyuan3d
        hunyuan3d_mode = bpy.context.scene.blendermcp_hunyuan3d_mode
        secret_id = self._get_hunyuan3d_secret_id()
        secret_key = self._get_hunyuan3d_secret_key()
        api_url = self._get_hunyuan3d_api_url()
        if enabled:
            match hunyuan3d_mode:
                case "OFFICIAL_API":
                    if not secret_id or not secret_key:
                        return {
                            "enabled": False, 
                            "mode": hunyuan3d_mode, 
                            "message": """Hunyuan3D integration is currently enabled, but SecretId or SecretKey is not given. To enable it:
                                1. In the 3D Viewport, find the MCP for Blender panel in the sidebar (press N if hidden)
                                2. Keep the 'Use Tencent Hunyuan 3D model generation' checkbox checked
                                3. Choose the right platform and fill in the SecretId and SecretKey
                                4. Restart the connection to Claude"""
                        }
                case "LOCAL_API":
                    if not api_url:
                        return {
                            "enabled": False, 
                            "mode": hunyuan3d_mode, 
                            "message": """Hunyuan3D integration is currently enabled, but API URL  is not given. To enable it:
                                1. In the 3D Viewport, find the MCP for Blender panel in the sidebar (press N if hidden)
                                2. Keep the 'Use Tencent Hunyuan 3D model generation' checkbox checked
                                3. Choose the right platform and fill in the API URL
                                4. Restart the connection to Claude"""
                        }
                case _:
                    return {
                        "enabled": False, 
                        "message": "Hunyuan3D integration is enabled and mode is not supported."
                    }
            return {
                "enabled": True, 
                "mode": hunyuan3d_mode,
                "message": "Hunyuan3D integration is enabled and ready to use."
            }
        return {
            "enabled": False, 
            "message": """Hunyuan3D integration is currently disabled. To enable it:
                        1. In the 3D Viewport, find the MCP for Blender panel in the sidebar (press N if hidden)
                        2. Check the 'Use Tencent Hunyuan 3D model generation' checkbox
                        3. Restart the connection to Claude"""
        }
    
    @staticmethod
    def get_tencent_cloud_sign_headers(
        method: str,
        path: str,
        headParams: dict,
        data: dict,
        service: str,
        region: str,
        secret_id: str,
        secret_key: str,
        host: str = None
    ):
        """Generate the signature header required for Tencent Cloud API requests headers"""
        # Generate timestamp
        timestamp = int(time.time())
        date = datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d")
        
        # If host is not provided, it is generated based on service and region.
        if not host:
            host = f"{service}.tencentcloudapi.com"
        
        endpoint = f"https://{host}"
        
        # Constructing the request body
        payload_str = json.dumps(data)
        
        # ************* Step 1: Concatenate the canonical request string *************
        canonical_uri = path
        canonical_querystring = ""
        ct = "application/json; charset=utf-8"
        canonical_headers = f"content-type:{ct}\nhost:{host}\nx-tc-action:{headParams.get('Action', '').lower()}\n"
        signed_headers = "content-type;host;x-tc-action"
        hashed_request_payload = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()
        
        canonical_request = (method + "\n" +
                            canonical_uri + "\n" +
                            canonical_querystring + "\n" +
                            canonical_headers + "\n" +
                            signed_headers + "\n" +
                            hashed_request_payload)

        # ************* Step 2: Construct the reception signature string *************
        credential_scope = f"{date}/{service}/tc3_request"
        hashed_canonical_request = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        string_to_sign = ("TC3-HMAC-SHA256" + "\n" +
                        str(timestamp) + "\n" +
                        credential_scope + "\n" +
                        hashed_canonical_request)

        # ************* Step 3: Calculate the signature *************
        def sign(key, msg):
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        secret_date = sign(("TC3" + secret_key).encode("utf-8"), date)
        secret_service = sign(secret_date, service)
        secret_signing = sign(secret_service, "tc3_request")
        signature = hmac.new(
            secret_signing, 
            string_to_sign.encode("utf-8"), 
            hashlib.sha256
        ).hexdigest()

        # ************* Step 4: Connect Authorization *************
        authorization = ("TC3-HMAC-SHA256" + " " +
                        "Credential=" + secret_id + "/" + credential_scope + ", " +
                        "SignedHeaders=" + signed_headers + ", " +
                        "Signature=" + signature)

        # Constructing request headers
        headers = {
            "Authorization": authorization,
            "Content-Type": "application/json; charset=utf-8",
            "Host": host,
            "X-TC-Action": headParams.get("Action", ""),
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": headParams.get("Version", ""),
            "X-TC-Region": region
        }

        return headers, endpoint

    def create_hunyuan_job(self, *args, **kwargs):
        match bpy.context.scene.blendermcp_hunyuan3d_mode:
            case "OFFICIAL_API":
                return self.create_hunyuan_job_main_site(*args, **kwargs)
            case "LOCAL_API":
                return self.create_hunyuan_job_local_site(*args, **kwargs)
            case _:
                return f"Error: Unknown Hunyuan3D mode!"

    def create_hunyuan_job_main_site(
        self,
        text_prompt: str = None,
        image: str = None
    ):
        try:
            secret_id = self._get_hunyuan3d_secret_id()
            secret_key = self._get_hunyuan3d_secret_key()

            if not secret_id or not secret_key:
                return {"error": "SecretId or SecretKey is not given"}

            # Parameter verification
            if not text_prompt and not image:
                return {"error": "Prompt or Image is required"}
            if text_prompt and image:
                return {"error": "Prompt and Image cannot be provided simultaneously"}
            # Updated to Tencent Cloud AI3D API 3.0 (2025-05-13)
            service = "ai3d"
            action = "SubmitHunyuanTo3DProJob"
            version = "2025-05-13"
            region = "ap-guangzhou"

            headParams={
                "Action": action,
                "Version": version,
                "Region": region,
            }

            # Constructing request parameters
            data = {}

            # Handling text prompts
            if text_prompt:
                if len(text_prompt) > 1024:
                    return {"error": "Prompt exceeds 1024 characters limit"}
                data["Prompt"] = text_prompt

            # Handling image
            if image:
                if re.match(r'^https?://', image, re.IGNORECASE) is not None:
                    data["ImageUrl"] = image
                else:
                    try:
                        # Convert to Base64 format
                        with open(image, "rb") as f:
                            image_base64 = base64.b64encode(f.read()).decode("ascii")
                        data["ImageBase64"] = image_base64
                    except Exception as e:
                        return {"error": f"Image encoding failed: {str(e)}"}
            
            # Get signed headers
            headers, endpoint = self.get_tencent_cloud_sign_headers("POST", "/", headParams, data, service, region, secret_id, secret_key)

            response = requests.post(
                endpoint,
                headers = headers,
                data = json.dumps(data)
            )

            if response.status_code == 200:
                return response.json()
            return {
                "error": f"API request failed with status {response.status_code}: {response}"
            }
        except Exception as e:
            return {"error": str(e)}

    def create_hunyuan_job_local_site(
        self,
        text_prompt: str = None,
        image: str = None):
        try:
            base_url = self._get_hunyuan3d_api_url().rstrip('/')
            octree_resolution = bpy.context.scene.blendermcp_hunyuan3d_octree_resolution
            num_inference_steps = bpy.context.scene.blendermcp_hunyuan3d_num_inference_steps
            guidance_scale = bpy.context.scene.blendermcp_hunyuan3d_guidance_scale
            texture = bpy.context.scene.blendermcp_hunyuan3d_texture

            if not base_url:
                return {"error": "API URL is not given"}
            # Parameter verification
            if not text_prompt and not image:
                return {"error": "Prompt or Image is required"}

            # Constructing request parameters
            data = {
                "octree_resolution": octree_resolution,
                "num_inference_steps": num_inference_steps,
                "guidance_scale": guidance_scale,
                "texture": texture,
            }

            # Handling text prompts
            if text_prompt:
                data["text"] = text_prompt

            # Handling image
            if image:
                if re.match(r'^https?://', image, re.IGNORECASE) is not None:
                    try:
                        resImg = requests.get(image)
                        resImg.raise_for_status()
                        image_base64 = base64.b64encode(resImg.content).decode("ascii")
                        data["image"] = image_base64
                    except Exception as e:
                        return {"error": f"Failed to download or encode image: {str(e)}"} 
                else:
                    try:
                        # Convert to Base64 format
                        with open(image, "rb") as f:
                            image_base64 = base64.b64encode(f.read()).decode("ascii")
                        data["image"] = image_base64
                    except Exception as e:
                        return {"error": f"Image encoding failed: {str(e)}"}

            response = requests.post(
                f"{base_url}/generate",
                json = data,
            )

            if response.status_code != 200:
                return {
                    "error": f"Generation failed: {response.text}"
                }
        
            # Decode base64 and save to temporary file
            with tempfile.NamedTemporaryFile(delete=False, suffix=".glb") as temp_file:
                temp_file.write(response.content)
                temp_file_name = temp_file.name

            # Import the GLB file in the main thread
            def import_handler():
                bpy.ops.import_scene.gltf(filepath=temp_file_name)
                os.unlink(temp_file.name)
                return None
            
            bpy.app.timers.register(import_handler)

            return {
                "status": "DONE",
                "message": "Generation and Import glb succeeded"
            }
        except Exception as e:
            print(f"An error occurred: {e}")
            return {"error": str(e)}
        
    
    def texture_hunyuan_job_local(
        self,
        object_name: str,
        text_prompt: str = None,
        image: str = None):
        """Texture an existing mesh through the local Hunyuan3D-2 API.

        Exports the named mesh as a GLB, sends it (base64) together with a text
        prompt and/or reference image to the local /generate endpoint with
        texture=True, then imports the textured result on a Blender timer,
        matching the original object's transform and hiding the original.
        Only valid in LOCAL_API mode (the cloud API does not accept a mesh).
        """
        try:
            base_url = self._get_hunyuan3d_api_url().rstrip('/')
            if not base_url:
                return {"error": "API URL is not given"}

            obj = bpy.data.objects.get(object_name)
            if obj is None or obj.type != 'MESH':
                return {"error": f"Mesh object '{object_name}' not found"}

            octree_resolution = bpy.context.scene.blendermcp_hunyuan3d_octree_resolution
            num_inference_steps = bpy.context.scene.blendermcp_hunyuan3d_num_inference_steps
            guidance_scale = bpy.context.scene.blendermcp_hunyuan3d_guidance_scale

            # Export the mesh to a temporary GLB and base64-encode it
            with tempfile.NamedTemporaryFile(delete=False, suffix=".glb") as tf_in:
                tmp_in = tf_in.name
            try:
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                bpy.context.view_layer.objects.active = obj
                bpy.ops.export_scene.gltf(filepath=tmp_in, use_selection=True)
                with open(tmp_in, "rb") as f:
                    mesh_b64 = base64.b64encode(f.read()).decode()
            finally:
                if os.path.exists(tmp_in):
                    os.unlink(tmp_in)

            data = {
                "mesh": mesh_b64,
                "octree_resolution": octree_resolution,
                "num_inference_steps": num_inference_steps,
                "guidance_scale": guidance_scale,
                "texture": True,
            }
            if text_prompt:
                data["text"] = text_prompt
            if image:
                if re.match(r'^https?://', image, re.IGNORECASE) is not None:
                    try:
                        res_img = requests.get(image)
                        res_img.raise_for_status()
                        data["image"] = base64.b64encode(res_img.content).decode("ascii")
                    except Exception as e:
                        return {"error": f"Failed to download or encode image: {str(e)}"}
                else:
                    try:
                        with open(image, "rb") as f:
                            data["image"] = base64.b64encode(f.read()).decode("ascii")
                    except Exception as e:
                        return {"error": f"Image encoding failed: {str(e)}"}

            response = requests.post(f"{base_url}/generate", json=data)
            if response.status_code != 200:
                return {"error": f"Texturing failed: {response.text}"}

            with tempfile.NamedTemporaryFile(delete=False, suffix=".glb") as temp_file:
                temp_file.write(response.content)
                temp_out = temp_file.name

            # Capture the original transform to apply to the textured result
            src_loc = tuple(obj.location)
            src_rot = tuple(obj.rotation_euler)
            src_scl = tuple(obj.scale)

            def import_handler():
                before = set(bpy.data.objects)
                bpy.ops.import_scene.gltf(filepath=temp_out)
                if os.path.exists(temp_out):
                    os.unlink(temp_out)
                news = [o for o in bpy.data.objects if o not in before]
                roots = [o for o in news if o.parent is None] or news
                if roots:
                    r0 = roots[0]
                    r0.location = src_loc
                    r0.rotation_euler = src_rot
                    r0.scale = src_scl
                src = bpy.data.objects.get(object_name)
                if src is not None:
                    src.hide_set(True)
                    src.hide_render = True
                return None

            bpy.app.timers.register(import_handler)

            return {
                "status": "DONE",
                "message": f"Textured mesh '{object_name}' and imported the result"
            }
        except Exception as e:
            print(f"An error occurred: {e}")
            return {"error": str(e)}

    def poll_hunyuan_job_status(self, *args, **kwargs):
        return self.poll_hunyuan_job_status_ai(*args, **kwargs)
    
    def poll_hunyuan_job_status_ai(self, job_id: str):
        """Call the job status API to get the job status"""
        print(job_id)
        try:
            secret_id = self._get_hunyuan3d_secret_id()
            secret_key = self._get_hunyuan3d_secret_key()

            if not secret_id or not secret_key:
                return {"error": "SecretId or SecretKey is not given"}
            if not job_id:
                return {"error": "JobId is required"}
            
            # Updated to Tencent Cloud AI3D API 3.0 (2025-05-13)
            service = "ai3d"
            action = "QueryHunyuanTo3DProJob"
            version = "2025-05-13"
            region = "ap-guangzhou"

            headParams={
                "Action": action,
                "Version": version,
                "Region": region,
            }

            clean_job_id = job_id.removeprefix("job_")
            data = {
                "JobId": clean_job_id
            }

            headers, endpoint = self.get_tencent_cloud_sign_headers("POST", "/", headParams, data, service, region, secret_id, secret_key)

            response = requests.post(
                endpoint,
                headers=headers,
                data=json.dumps(data)
            )

            if response.status_code == 200:
                return response.json()
            return {
                "error": f"API request failed with status {response.status_code}: {response}"
            }
        except Exception as e:
            return {"error": str(e)}

    def import_generated_asset_hunyuan(self, *args, **kwargs):
        return self.import_generated_asset_hunyuan_ai(*args, **kwargs)
            
    def import_generated_asset_hunyuan_ai(self, name: str, zip_file_url: str):
        if not zip_file_url:
            return {"error": "No file URL provided"}
        
        # Validate URL
        if not re.match(r'^https?://', zip_file_url, re.IGNORECASE):
            return {"error": "Invalid URL format. Must start with http:// or https://"}

        # Prefer GLB (self-contained with materials) over OBJ/ZIP (API 3.0 returns .glb URLs)
        url_path = zip_file_url.split('?', 1)[0].split('#', 1)[0].lower()
        if url_path.endswith('.glb'):
            temp_dir = tempfile.mkdtemp(prefix="hunyuan_glb_")
            glb_path = osp.join(temp_dir, "model.glb")
            try:
                glb_response = requests.get(zip_file_url, stream=True)
                glb_response.raise_for_status()
                with open(glb_path, "wb") as f:
                    for chunk in glb_response.iter_content(chunk_size=8192):
                        f.write(chunk)
                bpy.ops.import_scene.gltf(filepath=glb_path)
                imported_objs = [obj for obj in bpy.context.selected_objects if obj.type == 'MESH']
                if not imported_objs:
                    return {"succeed": False, "error": "No mesh objects imported from GLB"}
                obj = imported_objs[0]
                if name:
                    obj.name = name
                result = {
                    "name": obj.name, "type": obj.type,
                    "location": [obj.location.x, obj.location.y, obj.location.z],
                    "rotation": [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z],
                    "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
                }
                if obj.type == "MESH":
                    result["world_bounding_box"] = self._get_aabb(obj)
                return {"succeed": True, **result}
            except Exception as e:
                return {"succeed": False, "error": str(e)}
            finally:
                with suppress(Exception):
                    shutil.rmtree(temp_dir)

        # Fallback: ZIP/OBJ import (legacy)
        temp_dir = tempfile.mkdtemp(prefix="tencent_obj_")
        zip_file_path = osp.join(temp_dir, "model.zip")
        obj_file_path = osp.join(temp_dir, "model.obj")
        try:
            zip_response = requests.get(zip_file_url, stream=True)
            zip_response.raise_for_status()
            with open(zip_file_path, "wb") as f:
                for chunk in zip_response.iter_content(chunk_size=8192):
                    f.write(chunk)
            with zipfile.ZipFile(zip_file_path, "r") as zip_ref:
                # Mirror the Sketchfab zip-slip checks before extractall.
                abs_temp_dir = os.path.abspath(temp_dir)
                for file_info in zip_ref.infolist():
                    file_path = file_info.filename
                    target_path = os.path.join(temp_dir, os.path.normpath(file_path))
                    abs_target_path = os.path.abspath(target_path)
                    if not abs_target_path.startswith(abs_temp_dir + os.sep) and abs_target_path != abs_temp_dir:
                        return {
                            "succeed": False,
                            "error": "Security issue: Zip contains files with path traversal attempt",
                        }
                    if ".." in file_path:
                        return {
                            "succeed": False,
                            "error": "Security issue: Zip contains files with directory traversal sequence",
                        }
                zip_ref.extractall(temp_dir)
            for file in os.listdir(temp_dir):
                if file.endswith(".obj"):
                    obj_file_path = osp.join(temp_dir, file)
            if not osp.exists(obj_file_path):
                return {"succeed": False, "error": "OBJ file not found after extraction"}
            if bpy.app.version>=(4, 0, 0):
                bpy.ops.wm.obj_import(filepath=obj_file_path)
            else:
                bpy.ops.import_scene.obj(filepath=obj_file_path)
            imported_objs = [obj for obj in bpy.context.selected_objects if obj.type == 'MESH']
            if not imported_objs:
                return {"succeed": False, "error": "No mesh objects imported"}
            obj = imported_objs[0]
            if name:
                obj.name = name
            result = {
                "name": obj.name, "type": obj.type,
                "location": [obj.location.x, obj.location.y, obj.location.z],
                "rotation": [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z],
                "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            }
            if obj.type == "MESH":
                result["world_bounding_box"] = self._get_aabb(obj)
            return {"succeed": True, **result}
        except Exception as e:
            return {"succeed": False, "error": str(e)}
        finally:
            with suppress(Exception):
                shutil.rmtree(temp_dir)
    #endregion

# Blender Addon Preferences
class BLENDERMCP_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__
    
    def _on_telemetry_consent_changed(self, context):
        try:
            sync_edit_capture_handlers()
        except Exception as e:
            print(f"BlenderMCP: could not sync manual edit handlers: {e}")

    telemetry_consent: BoolProperty(
        name="Allow Telemetry",
        description="Allow collection of prompts, code snippets, screenshots, and trajectory data to help improve MCP for Blender",
        default=True,
        update=_on_telemetry_consent_changed,
    )
    hyper3d_api_key: bpy.props.StringProperty(
        name="Hyper3D API Key",
        subtype="PASSWORD",
        description="Persistent Hyper3D API Key",
        default=""
    )
    sketchfab_api_key: bpy.props.StringProperty(
        name="Sketchfab API Key",
        subtype="PASSWORD",
        description="Persistent Sketchfab API Key",
        default=""
    )
    polypizza_api_key: bpy.props.StringProperty(
        name="Poly Pizza API Key",
        subtype="PASSWORD",
        description="Persistent Poly Pizza API Key",
        default=""
    )
    hunyuan3d_secret_id: bpy.props.StringProperty(
        name="Hunyuan3D SecretId",
        description="Persistent Hunyuan3D SecretId",
        default=""
    )
    hunyuan3d_secret_key: bpy.props.StringProperty(
        name="Hunyuan3D SecretKey",
        subtype="PASSWORD",
        description="Persistent Hunyuan3D SecretKey",
        default=""
    )
    hunyuan3d_api_url: bpy.props.StringProperty(
        name="Hunyuan3D API URL",
        description="Persistent Hunyuan3D API URL",
        default=""
    )

    def draw(self, context):
        layout = self.layout
        
        # Telemetry section
        layout.label(text="Telemetry & Privacy:", icon='PREFERENCES')
        
        box = layout.box()
        row = box.row()
        row.prop(self, "telemetry_consent", text="Allow Telemetry")

        # Info text
        box.separator()
        if self.telemetry_consent:
            box.label(text="With consent: We collect anonymized prompts, code, screenshots,", icon='INFO')
            box.label(text="and trajectory data (actions, scene state, feedback).", icon='BLANK1')
        else:
            box.label(text="Without consent: We only collect minimal anonymous usage data", icon='INFO')
            box.label(text="(tool names, success/failure, duration - no prompts or code).", icon='BLANK1')
        box.separator()
        box.label(text="Data is not linked to your name or account. Change this anytime.", icon='CHECKMARK')
        
        # Terms and Conditions link
        box.separator()
        row = box.row()
        row.operator("blendermcp.open_terms", text="View Terms and Conditions", icon='TEXT')

        layout.separator()
        layout.label(text="Persistent API Credentials:", icon='LOCKED')
        cred_box = layout.box()
        cred_box.prop(self, "sketchfab_api_key", text="Sketchfab API Key")
        cred_box.prop(self, "polypizza_api_key", text="Poly Pizza API Key")
        cred_box.prop(self, "hyper3d_api_key", text="Hyper3D API Key")
        cred_box.prop(self, "hunyuan3d_secret_id", text="Hunyuan3D SecretId")
        cred_box.prop(self, "hunyuan3d_secret_key", text="Hunyuan3D SecretKey")
        cred_box.prop(self, "hunyuan3d_api_url", text="Hunyuan3D API URL")

# Blender UI Panel
class BLENDERMCP_PT_Panel(bpy.types.Panel):
    bl_label = "MCP for Blender"
    bl_idname = "BLENDERMCP_PT_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'MCP for Blender'

    def _integration_header(self, layout, scene, prop_name, title, icon):
        """Draw an integration as a box with a checkbox header row.
        Returns the box if the integration is enabled (for settings), else None."""
        box = layout.box()
        row = box.row()
        row.prop(scene, prop_name, text="")
        row.label(text=title, icon=icon)
        return box if getattr(scene, prop_name) else None

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        prefs = get_blendermcp_addon_preferences(context)

        # Connection
        box = layout.box()
        col = box.column()
        if scene.blendermcp_server_running:
            col.label(text=f"Connected on port {scene.blendermcp_port}", icon='CHECKMARK')
            col.operator("blendermcp.stop_server", text="Disconnect", icon='X')
        else:
            col.label(text="Not connected", icon='RADIOBUT_OFF')
            col.prop(scene, "blendermcp_port")
            col.operator("blendermcp.start_server", text="Connect to MCP server", icon='PLAY')

        # Asset libraries
        layout.separator()
        layout.label(text="Asset Libraries", icon='ASSET_MANAGER')

        self._integration_header(
            layout, scene, "blendermcp_use_polyhaven", "Poly Haven", 'WORLD')

        sub = self._integration_header(
            layout, scene, "blendermcp_use_sketchfab", "Sketchfab", 'MESH_MONKEY')
        if sub:
            col = sub.column(align=True)
            if prefs:
                col.prop(prefs, "sketchfab_api_key", text="API Key")
            else:
                col.prop(scene, "blendermcp_sketchfab_api_key", text="API Key")

        sub = self._integration_header(
            layout, scene, "blendermcp_use_polypizza", "Poly Pizza", 'MESH_ICOSPHERE')
        if sub:
            col = sub.column(align=True)
            if prefs:
                col.prop(prefs, "polypizza_api_key", text="API Key")
            else:
                col.prop(scene, "blendermcp_polypizza_api_key", text="API Key")

        # AI model generation
        layout.separator()
        layout.label(text="AI Model Generation", icon='SHADERFX')

        sub = self._integration_header(
            layout, scene, "blendermcp_use_hyper3d", "Hyper3D Rodin", 'MESH_UVSPHERE')
        if sub:
            col = sub.column(align=True)
            col.prop(scene, "blendermcp_hyper3d_mode", text="Mode")
            if prefs:
                col.prop(prefs, "hyper3d_api_key", text="API Key")
            else:
                col.prop(scene, "blendermcp_hyper3d_api_key", text="API Key")
            sub.operator("blendermcp.set_hyper3d_free_trial_api_key",
                         text="Set Free Trial API Key", icon='KEYINGSET')

        sub = self._integration_header(
            layout, scene, "blendermcp_use_hunyuan3d", "Tencent Hunyuan 3D", 'MESH_CUBE')
        if sub:
            col = sub.column(align=True)
            col.prop(scene, "blendermcp_hunyuan3d_mode", text="Mode")
            if scene.blendermcp_hunyuan3d_mode == 'OFFICIAL_API':
                if prefs:
                    col.prop(prefs, "hunyuan3d_secret_id", text="SecretId")
                    col.prop(prefs, "hunyuan3d_secret_key", text="SecretKey")
                else:
                    col.prop(scene, "blendermcp_hunyuan3d_secret_id", text="SecretId")
                    col.prop(scene, "blendermcp_hunyuan3d_secret_key", text="SecretKey")
            if scene.blendermcp_hunyuan3d_mode == 'LOCAL_API':
                if prefs:
                    col.prop(prefs, "hunyuan3d_api_url", text="API URL")
                else:
                    col.prop(scene, "blendermcp_hunyuan3d_api_url", text="API URL")
                col.separator()
                col.prop(scene, "blendermcp_hunyuan3d_octree_resolution", text="Octree Resolution")
                col.prop(scene, "blendermcp_hunyuan3d_num_inference_steps", text="Inference Steps")
                col.prop(scene, "blendermcp_hunyuan3d_guidance_scale", text="Guidance Scale")
                col.prop(scene, "blendermcp_hunyuan3d_texture", text="Generate Texture")

        # Feedback section
        layout.separator()
        feedback_box = layout.box()

        col = feedback_box.column(align=True)
        col.label(text="Schedule a feedback call", icon='URL')
        col.label(text="bit.ly/blender-mcp-call")

# Operator to set Hyper3D API Key
class BLENDERMCP_OT_SetFreeTrialHyper3DAPIKey(bpy.types.Operator):
    bl_idname = "blendermcp.set_hyper3d_free_trial_api_key"
    bl_label = "Set Free Trial API Key"

    def execute(self, context):
        prefs = get_blendermcp_addon_preferences(context)
        if prefs:
            if not prefs.hyper3d_api_key or prefs.hyper3d_api_key == RODIN_FREE_TRIAL_KEY:
                prefs.hyper3d_api_key = RODIN_FREE_TRIAL_KEY
            else:
                self.report(
                    {'INFO'},
                    "Using free trial for this session only; saved private key was kept."
                )
        context.scene.blendermcp_hyper3d_api_key = RODIN_FREE_TRIAL_KEY
        context.scene.blendermcp_hyper3d_mode = 'MAIN_SITE'
        self.report({'INFO'}, "API Key set successfully!")
        return {'FINISHED'}

# Operator to start the server
class BLENDERMCP_OT_StartServer(bpy.types.Operator):
    bl_idname = "blendermcp.start_server"
    bl_label = "Connect to Claude"
    bl_description = "Start the MCP for Blender server to connect with Claude"

    def execute(self, context):
        scene = context.scene

        # Create a new server instance
        if not hasattr(bpy.types, "blendermcp_server") or not bpy.types.blendermcp_server:
            bpy.types.blendermcp_server = BlenderMCPServer(port=scene.blendermcp_port)

        # Start the server
        bpy.types.blendermcp_server.start()
        scene.blendermcp_server_running = bpy.types.blendermcp_server.running

        return {'FINISHED'}

# Operator to stop the server
class BLENDERMCP_OT_StopServer(bpy.types.Operator):
    bl_idname = "blendermcp.stop_server"
    bl_label = "Stop the connection to Claude"
    bl_description = "Stop the connection to Claude"

    def execute(self, context):
        scene = context.scene

        # Stop the server if it exists
        if hasattr(bpy.types, "blendermcp_server") and bpy.types.blendermcp_server:
            bpy.types.blendermcp_server.stop()
            del bpy.types.blendermcp_server

        scene.blendermcp_server_running = False

        return {'FINISHED'}

# Operator to open Terms and Conditions
class BLENDERMCP_OT_OpenTerms(bpy.types.Operator):
    bl_idname = "blendermcp.open_terms"
    bl_label = "View Terms and Conditions"
    bl_description = "Open the Terms and Conditions document"

    def execute(self, context):
        # Open the Terms and Conditions on GitHub
        terms_url = "https://github.com/ahujasid/blender-mcp/blob/main/TERMS_AND_CONDITIONS.md"
        try:
            import webbrowser
            webbrowser.open(terms_url)
            self.report({'INFO'}, "Terms and Conditions opened in browser")
        except Exception as e:
            self.report({'ERROR'}, f"Could not open Terms and Conditions: {str(e)}")
        
        return {'FINISHED'}

# Registration functions
def register():
    bpy.types.Scene.blendermcp_port = IntProperty(
        name="Port",
        description="Port for the MCP for Blender server",
        default=9876,
        min=1024,
        max=65535
    )

    bpy.types.Scene.blendermcp_server_running = bpy.props.BoolProperty(
        name="Server Running",
        default=False
    )

    bpy.types.Scene.blendermcp_auto_start_server = bpy.props.BoolProperty(
        name="Auto-Start Server",
        description="Automatically start the MCP server when Blender loads",
        default=True
    )

    bpy.types.Scene.blendermcp_use_polyhaven = bpy.props.BoolProperty(
        name="Use Poly Haven",
        description="Enable Poly Haven asset integration",
        default=False
    )

    bpy.types.Scene.blendermcp_use_hyper3d = bpy.props.BoolProperty(
        name="Use Hyper3D Rodin",
        description="Enable Hyper3D Rodin generatino integration",
        default=False
    )

    bpy.types.Scene.blendermcp_hyper3d_mode = bpy.props.EnumProperty(
        name="Rodin Mode",
        description="Choose the platform used to call Rodin APIs",
        items=[
            ("MAIN_SITE", "hyper3d.ai", "hyper3d.ai"),
            ("FAL_AI", "fal.ai", "fal.ai"),
        ],
        default="MAIN_SITE"
    )

    bpy.types.Scene.blendermcp_hyper3d_api_key = bpy.props.StringProperty(
        name="Hyper3D API Key",
        subtype="PASSWORD",
        description="API Key provided by Hyper3D",
        default=""
    )

    bpy.types.Scene.blendermcp_use_hunyuan3d = bpy.props.BoolProperty(
        name="Use Hunyuan 3D",
        description="Enable Hunyuan asset integration",
        default=False
    )

    bpy.types.Scene.blendermcp_hunyuan3d_mode = bpy.props.EnumProperty(
        name="Hunyuan3D Mode",
        description="Choose a local or official APIs",
        items=[
            ("LOCAL_API", "local api", "local api"),
            ("OFFICIAL_API", "official api", "official api"),
        ],
        default="LOCAL_API"
    )

    bpy.types.Scene.blendermcp_hunyuan3d_secret_id = bpy.props.StringProperty(
        name="Hunyuan 3D SecretId",
        description="SecretId provided by Hunyuan 3D",
        default=""
    )

    bpy.types.Scene.blendermcp_hunyuan3d_secret_key = bpy.props.StringProperty(
        name="Hunyuan 3D SecretKey",
        subtype="PASSWORD",
        description="SecretKey provided by Hunyuan 3D",
        default=""
    )

    bpy.types.Scene.blendermcp_hunyuan3d_api_url = bpy.props.StringProperty(
        name="API URL",
        description="URL of the Hunyuan 3D API service",
        default="http://localhost:8081"
    )

    bpy.types.Scene.blendermcp_hunyuan3d_octree_resolution = bpy.props.IntProperty(
        name="Octree Resolution",
        description="Octree resolution for the 3D generation",
        default=256,
        min=128,
        max=512,
    )

    bpy.types.Scene.blendermcp_hunyuan3d_num_inference_steps = bpy.props.IntProperty(
        name="Number of Inference Steps",
        description="Number of inference steps for the 3D generation",
        default=20,
        min=20,
        max=50,
    )

    bpy.types.Scene.blendermcp_hunyuan3d_guidance_scale = bpy.props.FloatProperty(
        name="Guidance Scale",
        description="Guidance scale for the 3D generation",
        default=5.5,
        min=1.0,
        max=10.0,
    )

    bpy.types.Scene.blendermcp_hunyuan3d_texture = bpy.props.BoolProperty(
        name="Generate Texture",
        description="Whether to generate texture for the 3D model",
        default=False,
    )
    
    bpy.types.Scene.blendermcp_use_sketchfab = bpy.props.BoolProperty(
        name="Use Sketchfab",
        description="Enable Sketchfab asset integration",
        default=False
    )

    bpy.types.Scene.blendermcp_sketchfab_api_key = bpy.props.StringProperty(
        name="Sketchfab API Key",
        subtype="PASSWORD",
        description="API Key provided by Sketchfab",
        default=""
    )

    bpy.types.Scene.blendermcp_use_polypizza = bpy.props.BoolProperty(
        name="Use Poly Pizza",
        description="Enable Poly Pizza asset integration",
        default=False
    )

    bpy.types.Scene.blendermcp_polypizza_api_key = bpy.props.StringProperty(
        name="Poly Pizza API Key",
        subtype="PASSWORD",
        description="API Key provided by Poly Pizza",
        default=""
    )

    # Register preferences class
    bpy.utils.register_class(BLENDERMCP_AddonPreferences)

    bpy.utils.register_class(BLENDERMCP_PT_Panel)
    bpy.utils.register_class(BLENDERMCP_OT_SetFreeTrialHyper3DAPIKey)
    bpy.utils.register_class(BLENDERMCP_OT_StartServer)
    bpy.utils.register_class(BLENDERMCP_OT_StopServer)
    bpy.utils.register_class(BLENDERMCP_OT_OpenTerms)

    # Auto-start the server so the MCP client can connect without manual UI interaction
    scene = getattr(bpy.context, 'scene', None)
    if scene is not None:
        port = scene.blendermcp_port
        auto_start = scene.blendermcp_auto_start_server
    else:
        port = 9876
        auto_start = True

    if auto_start and (not hasattr(bpy.types, "blendermcp_server") or not bpy.types.blendermcp_server):
        bpy.types.blendermcp_server = BlenderMCPServer(port=port)
    if auto_start and not bpy.types.blendermcp_server.running:
        bpy.types.blendermcp_server.start()
        try:
            bpy.context.scene.blendermcp_server_running = bpy.types.blendermcp_server.running
        except AttributeError:
            pass

    print("BlenderMCP addon registered")

def unregister():
    _unregister_edit_capture_handlers()

    # Stop the server if it's running
    if hasattr(bpy.types, "blendermcp_server") and bpy.types.blendermcp_server:
        bpy.types.blendermcp_server.stop()
        del bpy.types.blendermcp_server

    bpy.utils.unregister_class(BLENDERMCP_PT_Panel)
    bpy.utils.unregister_class(BLENDERMCP_OT_SetFreeTrialHyper3DAPIKey)
    bpy.utils.unregister_class(BLENDERMCP_OT_StartServer)
    bpy.utils.unregister_class(BLENDERMCP_OT_StopServer)
    bpy.utils.unregister_class(BLENDERMCP_OT_OpenTerms)
    bpy.utils.unregister_class(BLENDERMCP_AddonPreferences)

    del bpy.types.Scene.blendermcp_port
    del bpy.types.Scene.blendermcp_server_running
    del bpy.types.Scene.blendermcp_auto_start_server
    del bpy.types.Scene.blendermcp_use_polyhaven
    del bpy.types.Scene.blendermcp_use_hyper3d
    del bpy.types.Scene.blendermcp_hyper3d_mode
    del bpy.types.Scene.blendermcp_hyper3d_api_key
    del bpy.types.Scene.blendermcp_use_sketchfab
    del bpy.types.Scene.blendermcp_sketchfab_api_key
    del bpy.types.Scene.blendermcp_use_polypizza
    del bpy.types.Scene.blendermcp_polypizza_api_key
    del bpy.types.Scene.blendermcp_use_hunyuan3d
    del bpy.types.Scene.blendermcp_hunyuan3d_mode
    del bpy.types.Scene.blendermcp_hunyuan3d_secret_id
    del bpy.types.Scene.blendermcp_hunyuan3d_secret_key
    del bpy.types.Scene.blendermcp_hunyuan3d_api_url
    del bpy.types.Scene.blendermcp_hunyuan3d_octree_resolution
    del bpy.types.Scene.blendermcp_hunyuan3d_num_inference_steps
    del bpy.types.Scene.blendermcp_hunyuan3d_guidance_scale
    del bpy.types.Scene.blendermcp_hunyuan3d_texture

    print("BlenderMCP addon unregistered")

if __name__ == "__main__":
    register()
