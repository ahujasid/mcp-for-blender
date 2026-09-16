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
from urllib.parse import quote, urlencode, urlparse, urlunparse, parse_qsl
from contextlib import contextmanager, redirect_stdout, suppress
from bpy.app.handlers import persistent

bl_info = {
    "name": "MCP for Blender",
    "author": "Siddharth Ahuja",
    "version": (1, 7),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > MCP for Blender",
    "description": "Connect Blender to Claude via MCP",
    "doc_url": "https://mcp-for-blender.com/",
    "category": "Interface",
}

# Keep in sync with blender_mcp.addon_manager.EXPECTED_ADDON_PROTOCOL_VERSION.
ADDON_PROTOCOL_VERSION = 8

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

# Set when the user disconnects so opening another blend file does not restart
# the server behind their back. A manual connect or add-on reload clears it.
_user_stopped_server = False


def _blendermcp_port_has_listener(host, port):
    """Return True when another process already owns the MCP endpoint."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.15)
    try:
        return probe.connect_ex((host, port)) == 0
    finally:
        probe.close()


def _blendermcp_ensure_server_running():
    """Start the bridge after Blender's UI and scene context are ready.

    Returning a delay asks Blender's timer system to retry when a launch-time
    socket or context race prevented the first attempt.
    """
    if bpy.app.background:
        return None

    scene = getattr(bpy.context, "scene", None)
    if scene is None:
        return 0.5

    server = getattr(bpy.types, "blendermcp_server", None)
    if not scene.blendermcp_auto_start_server or _user_stopped_server:
        scene.blendermcp_server_running = bool(server is not None and server.running)
        return None

    port = scene.blendermcp_port
    if server is None:
        if _blendermcp_port_has_listener("localhost", port):
            scene.blendermcp_server_running = False
            print(f"BlenderMCP: port {port} is already in use; auto-start skipped.")
            return None
        server = BlenderMCPServer(port=port)
        bpy.types.blendermcp_server = server

    if not server.running:
        # Safe while stopped and necessary when a newly loaded scene selects a
        # different port. Never retarget an active connection.
        server.port = port
        server.start()

    scene.blendermcp_server_running = server.running

    return None if server.running else 1.0


def _blendermcp_schedule_auto_start(delay=0.5):
    """Schedule one persistent startup callback if none is already pending."""
    if not bpy.app.timers.is_registered(_blendermcp_ensure_server_running):
        bpy.app.timers.register(
            _blendermcp_ensure_server_running,
            first_interval=delay,
            persistent=True,
        )


@persistent
def _blendermcp_load_post(_unused):
    """Retry auto-start after Blender loads a startup file or another blend."""
    _blendermcp_schedule_auto_start()


def _blendermcp_register_auto_start():
    """Install the load handler and defer the initial startup attempt."""
    global _user_stopped_server
    _user_stopped_server = False
    if _blendermcp_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_blendermcp_load_post)
    _blendermcp_schedule_auto_start()


def _blendermcp_unregister_auto_start():
    """Remove callbacks owned by the add-on before it is disabled."""
    if _blendermcp_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_blendermcp_load_post)
    if bpy.app.timers.is_registered(_blendermcp_ensure_server_running):
        bpy.app.timers.unregister(_blendermcp_ensure_server_running)

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

#region Poly Haven constants and helpers

POLYHAVEN_API_BASE = "https://api.polyhaven.com"

# Versioned, so Poly Haven can tell which integration its traffic is coming from
# and how many people it is serving. Kept separate from the shared REQ_HEADERS
# because Poly Pizza sends that one too.
POLYHAVEN_HEADERS = dict(REQ_HEADERS)
POLYHAVEN_HEADERS["User-Agent"] = (
    "blender-mcp/" + ".".join(str(part) for part in bl_info["version"])
    + " (+https://github.com/ahujasid/blender-mcp)"
)

# (connect, read). The read timeout applies per socket read rather than to the
# whole transfer, so streaming a large HDRI never trips it - but a dead
# connection no longer hangs Blender's main thread indefinitely.
POLYHAVEN_API_TIMEOUT = (10, 30)
POLYHAVEN_FILE_TIMEOUT = (10, 60)

POLYHAVEN_CHUNK_SIZE = 1024 * 1024

# What we can actually import, per asset type. Checked BEFORE downloading
# anything: the API lists a `usd` entry for every model, which used to pass the
# "is this format present?" guard, get downloaded in full, and only then be
# rejected as an unsupported format.
POLYHAVEN_SUPPORTED_FORMATS = {
    "hdris": ("hdr", "exr"),
    "textures": ("jpg", "png", "exr"),
    "models": ("blend",),
}

POLYHAVEN_DEFAULT_FORMATS = {"hdris": "hdr", "textures": "jpg", "models": "blend"}

# Models are imported from the .blend and nothing else. Poly Haven authors its
# models in Blender and generates every other format from that file, so glTF and
# FBX are lossy renderings of a material that is sitting right there - node
# groups collapse to a base colour, and procedural setups do not survive at all.
#
# glTF stays as a fallback for one case only: a .blend written by a newer
# Blender than the one running, which cannot be opened at all. See
# _polyhaven_blend_version.
POLYHAVEN_MODEL_FALLBACK_FORMAT = "gltf"

# Poly Haven's /files map keys, and what each map drives. Their casing is
# inconsistent and load-bearing - "Diffuse", "Rough", "Metal" and
# "Displacement" are capitalised while "nor_gl" and "arm" are not - so these
# are matched exactly instead of being lower-cased and guessed at.
#
# This is the set that drives a Principled BSDF directly, and it covers what
# Poly Haven's own .blend materials use for the large majority of the library.
# Of the rest the API offers, "arm" is an ORM repacking of maps already here,
# "rough_ao" is roughness with AO baked in, and "nor_dx" is the other normal map
# convention. "AO", "spec" and "Bump" need extra nodes to be worth anything.
#
# It is not a complete match for every asset: measured across the 860 published
# textures, 114 ship a .blend referencing a map not in this table - 74 use "AO",
# and 30 fabrics drive Anisotropic, Anisotropic Rotation and IOR from
# "anisotropy_strength", "anisotropy_rotation" and "spec_ior". Those materials
# come out flatter here than the artist built them.
#
# Downloading every map and then leaving most of them unconnected is what cost
# 7.4MB to build a 1k material that connected 1.9MB of it. This table brings
# that asset down to 3.9MB, all of it wired.
POLYHAVEN_TEXTURE_MAPS = {
    "Diffuse": "base_color",
    "Rough": "roughness",
    "Metal": "metallic",
    "Displacement": "displacement",
    "nor_gl": "normal",
    "nor_dx": "normal",
}

# Only the albedo is colour data; every other map is values the shader reads.
POLYHAVEN_COLOR_ROLES = {"base_color"}

POLYHAVEN_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

POLYHAVEN_SITE = "https://polyhaven.com"

# `type` is an integer in the API's asset records.
POLYHAVEN_ASSET_TYPES = {0: "hdris", 1: "textures", 2: "models"}

POLYHAVEN_SEARCH_LIMIT = 20
POLYHAVEN_SEARCH_MAX_LIMIT = 50

# Levels of the category tree returned when every asset type is asked for at
# once. Filtering on a category is inclusive, so a parent still selects
# everything nested beneath it.
POLYHAVEN_TAXONOMY_DEPTH_ALL = 2

# Poly Haven publishes thumbnails at 256px. The CDN resizes from the query
# string, so a preview worth looking at costs no stored file.
POLYHAVEN_PREVIEW_SIZE = 512


class PolyHavenAPIError(Exception):
    """A non-2xx from the Poly Haven API, with the status kept.

    Needed because 429 and 503 want different handling from a generic failure:
    one means back off, the other means the search index is unavailable and the
    API is telling us to fall back to matching keywords ourselves.
    """

    def __init__(self, status, retry_after=None):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after


# Poly Haven serves the asset list with `Cache-Control: max-age=43200` and an
# ETag, and both were being discarded: every search re-fetched all 2.44MB of it.
# The TTL here matches theirs, and once it lapses the ETag usually turns the
# refetch into a 304.
POLYHAVEN_CACHE_TTL = 12 * 60 * 60

# Bounded so a session that searches every type and taxonomy cannot grow without
# limit. The asset list is by far the largest entry, and there are four of those.
POLYHAVEN_CACHE_MAX_ENTRIES = 16

_polyhaven_cache = {}

# A counter rather than a clock. time.time() has ~15ms resolution on Windows, so
# entries touched inside one burst of calls tie, and min() then evicts whichever
# happens to come first in the dict - which can be the very entry this is
# protecting.
_polyhaven_cache_clock = 0


def _polyhaven_cache_touch():
    global _polyhaven_cache_clock
    _polyhaven_cache_clock += 1
    return _polyhaven_cache_clock


def _polyhaven_cache_key(path, params):
    return path, tuple(sorted((params or {}).items()))


def _polyhaven_api_get(path, params=None, cache=False):
    """GET a Poly Haven API endpoint, raising on anything but a 2xx.

    With cache=True the response is held for POLYHAVEN_CACHE_TTL, and revalidated
    with If-None-Match after that rather than re-downloaded.
    """
    key = _polyhaven_cache_key(path, params)
    entry = _polyhaven_cache.get(key) if cache else None
    headers = dict(POLYHAVEN_HEADERS)

    if entry is not None:
        if time.time() - entry["fetched"] < POLYHAVEN_CACHE_TTL:
            entry["used"] = _polyhaven_cache_touch()
            return entry["payload"]
        if entry.get("etag"):
            headers["If-None-Match"] = entry["etag"]

    response = requests.get(
        f"{POLYHAVEN_API_BASE}/{path}",
        params=params,
        headers=headers,
        timeout=POLYHAVEN_API_TIMEOUT,
    )

    if entry is not None and response.status_code == 304:
        entry["fetched"] = time.time()
        entry["used"] = _polyhaven_cache_touch()
        return entry["payload"]

    if response.status_code >= 400:
        raise PolyHavenAPIError(
            response.status_code,
            getattr(response, "headers", {}).get("Retry-After"),
        )

    payload = response.json()

    if cache:
        if len(_polyhaven_cache) >= POLYHAVEN_CACHE_MAX_ENTRIES:
            # Least recently USED, not least recently fetched. Evicting on fetch
            # time is strictly FIFO, because a hit never refreshes it - and the
            # asset list is by construction the first thing fetched in a session
            # and then only ever read, so it was always the first entry thrown
            # out, displaced by one-shot search payloads a tenth of a percent its
            # size. Its ETag went with it, so the refetch could not revalidate.
            coldest = min(_polyhaven_cache, key=lambda k: _polyhaven_cache[k]["used"])
            _polyhaven_cache.pop(coldest, None)
        _polyhaven_cache[key] = {
            "payload": payload,
            "etag": getattr(response, "headers", {}).get("ETag"),
            "fetched": time.time(),
            "used": _polyhaven_cache_touch(),
        }

    return payload


def _polyhaven_valid_slug(asset_id):
    """Poly Haven slugs are always [A-Za-z0-9_-].

    Asset ids arrive from the model and are used to build the names of the files
    downloaded into the temporary directory, so they are checked once here
    rather than escaped differently in each place.
    """
    return bool(POLYHAVEN_SLUG_RE.match(asset_id or ""))


def _polyhaven_download(file_info, dest_path):
    """Stream one file to dest_path, verifying the md5 the API published.

    Streaming matters: resolution="24k", file_format="exr" is a valid call and
    that file is 2.4GB, which the previous response.content read materialised
    in memory in full before writing it back out again.
    """
    expected = file_info.get("md5")

    response = requests.get(
        file_info["url"],
        headers=POLYHAVEN_HEADERS,
        stream=True,
        timeout=POLYHAVEN_FILE_TIMEOUT,
    )
    response.raise_for_status()

    digest = hashlib.md5()
    with open(dest_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=POLYHAVEN_CHUNK_SIZE):
            if not chunk:
                continue
            digest.update(chunk)
            f.write(chunk)

    if expected and digest.hexdigest() != expected:
        with suppress(OSError):
            os.unlink(dest_path)
        raise ValueError(
            f"Checksum mismatch for {os.path.basename(dest_path)}: "
            "the download was truncated or corrupted"
        )
    return dest_path


def _polyhaven_uncompress_head(raw):
    """The start of a .blend, which is usually compressed on disk.

    Blender wrote gzip up to 2.93 and zstd from 3.0. Both decompressors are
    incremental, so a truncated prefix decompresses to a shorter prefix rather
    than raising.
    """
    if raw[:7] == b"BLENDER":
        return raw
    if raw[:2] == b"\x1f\x8b":
        with suppress(Exception):
            return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw)
        return None
    try:
        import zstandard
    except ImportError:
        # Not bundled with every Blender build. Without it the version cannot be
        # read, and the import falls back to trying the append and handling the
        # failure - which is the same outcome, one download later.
        return None
    with suppress(Exception):
        return zstandard.ZstdDecompressor().decompressobj().decompress(raw)
    return None


def _polyhaven_blend_version(path):
    """(major, minor) of the Blender that wrote this .blend, or None.

    Blender cannot open a file written by a newer version than itself, and Poly
    Haven's models span 2.93 to 5.0 because each was saved by whichever Blender
    compiled it. The version is in the file header, in one of two layouts:

        up to Blender 4.4:   BLENDER-v293
        from Blender 4.5:    BLENDER17-01v0502

    where the digits straight after BLENDER are the header's own length, and the
    version field grows from three characters to four.
    """
    try:
        with open(path, "rb") as f:
            head = _polyhaven_uncompress_head(f.read(1 << 16))
    except OSError:
        return None

    if not head or not head.startswith(b"BLENDER"):
        return None

    try:
        if head[7:9].isdigit():
            return int(head[13:15]), int(head[15:17])
        return int(head[9:10]), int(head[10:12])
    except (ValueError, IndexError):
        return None


def _polyhaven_category_paths(nodes, depth=None, _level=1):
    """Flatten the category tree to its paths, which is what filters take."""
    paths = []
    for node in nodes or []:
        if node.get("path"):
            paths.append(node["path"])
        if depth is None or _level < depth:
            paths.extend(_polyhaven_category_paths(node.get("children"), depth, _level + 1))
    return paths


def _polyhaven_taxonomy(asset_type, depth=None):
    """The category tree and attribute schema for one asset type, trimmed.

    The raw response is 60-80KB per type, most of it descriptions, UUIDs and
    URL slugs that nothing here uses. The paths are what a `categories` filter
    takes, and matching on them is inclusive, so a parent path selects
    everything beneath it.
    """
    payload = _polyhaven_api_get(f"taxonomy/{quote(asset_type, safe='')}", cache=True)

    attributes = {}
    for key, spec in (payload.get("attributes") or {}).items():
        if isinstance(spec, dict):
            attributes[key] = {
                field: spec[field]
                for field in ("type", "enum", "description")
                if field in spec
            }

    return {
        "type": payload.get("type") or asset_type,
        "categories": _polyhaven_category_paths(payload.get("categories"), depth),
        "attributes": attributes,
    }


def _polyhaven_asset_url(slug):
    return f"{POLYHAVEN_SITE}/a/{quote(slug, safe='')}"


def _polyhaven_asset_record(slug):
    """One asset's metadata, taken from the cached asset list where possible.

    /info/{id} is the same record plus a few internal fields, so it is only
    worth a request when the list has not already been fetched.
    """
    for entry in _polyhaven_cache.values():
        payload = entry.get("payload")
        if isinstance(payload, dict):
            record = payload.get(slug)
            if isinstance(record, dict) and "name" in record:
                return record
    return _polyhaven_api_get(f"info/{quote(slug, safe='')}", cache=True)


def _polyhaven_preview_url(thumbnail_url, size=POLYHAVEN_PREVIEW_SIZE):
    """Resize the published thumbnail without losing its cache-busting `v`.

    Poly Haven's CDN resizes from the query string, so a larger preview costs no
    stored file - but `thumbnail_url` also carries a `v` holding a hash of the
    asset's images, and a URL rebuilt by hand without it can be served a
    year-old thumbnail for an asset whose renders have since been replaced.
    """
    parts = urlparse(thumbnail_url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    if "width" in params or "height" in params:
        params["width"] = str(size)
        params["height"] = str(size)
    return urlunparse(parts._replace(query=urlencode(params)))


def _polyhaven_summarize_asset(slug, record):
    """Trim an /assets record down to what is worth sending back over MCP.

    The full record is around a kilobyte of JSON per asset and the whole page of
    results crosses the socket in one message, so twenty untrimmed records is
    most of what the model then has to read.
    """
    authors = record.get("authors") or {}
    summary = {
        "id": slug,
        "name": record.get("name") or slug,
        "type": POLYHAVEN_ASSET_TYPES.get(record.get("type"), "unknown"),
        "url": _polyhaven_asset_url(slug),
        "authors": sorted(authors) if isinstance(authors, dict) else authors,
        "downloads": record.get("download_count"),
    }

    for key in ("description", "category", "tags", "attributes", "max_resolution"):
        value = record.get(key)
        if value:
            summary[key] = value

    # Real-world size in millimetres, published for every texture. Without it
    # there is no way to know that a wall texture is 1.8m across, and the
    # material gets whatever tiling the object's UVs happen to give it.
    if record.get("dimensions"):
        summary["dimensions_mm"] = record["dimensions"]

    return summary


def _polyhaven_search(query, asset_type):
    """The full ranked list of slugs from Poly Haven's search endpoint.

    The array order IS the ranking - it fuses a vector lane and a keyword lane
    by position - so it must not be re-sorted by `score`, which reports vector
    similarity alone.

    No `limit` is sent. The endpoint returns the whole ranked list by design,
    because callers are expected to intersect it with whatever they already
    hold; asking for the first N and then filtering those would drop matches
    that were simply further down.
    """
    params = {"q": query}
    if asset_type and asset_type != "all":
        params["t"] = asset_type

    payload = _polyhaven_api_get("search", params=params, cache=True)
    return [r["slug"] for r in (payload.get("results") or []) if r.get("slug")]


def _polyhaven_keyword_match(query, assets):
    """The fallback the API asks for when it answers a search with 503."""
    terms = [term for term in query.split() if term]
    scored = []
    for slug, record in assets.items():
        haystack = " ".join([
            slug.replace("_", " "),
            str(record.get("name") or ""),
            " ".join(record.get("tags") or []),
            str(record.get("category") or ""),
        ]).lower()
        hits = sum(1 for term in terms if term in haystack)
        if hits:
            scored.append((hits, record.get("download_count", 0), slug))

    scored.sort(reverse=True)
    return [slug for _hits, _downloads, slug in scored]


def _polyhaven_resolution_rank(resolution):
    """"4k" -> 4, so resolutions sort numerically rather than as strings."""
    try:
        return int(str(resolution).rstrip("k"))
    except (TypeError, ValueError):
        return -1


def _polyhaven_sorted_resolutions(resolutions):
    return sorted(resolutions, key=lambda res: (_polyhaven_resolution_rank(res) < 0,
                                                _polyhaven_resolution_rank(res)))


def _polyhaven_available(files_data, asset_type):
    """Describe what an asset actually offers, for use in error messages.

    The three "not available" errors this replaces were f-strings with nothing
    interpolated into them, so an agent that guessed a resolution wrong had no
    way to correct itself except to guess again - and each guess cost another
    round trip.
    """
    supported = POLYHAVEN_SUPPORTED_FORMATS.get(asset_type, ())
    resolutions, formats = set(), set()
    for by_resolution in files_data.values():
        if not isinstance(by_resolution, dict):
            continue
        for resolution, by_format in by_resolution.items():
            if not isinstance(by_format, dict):
                continue
            present = {fmt for fmt in by_format if fmt in supported}
            if present:
                resolutions.add(resolution)
                formats |= present

    return (
        "available resolutions: "
        + (", ".join(_polyhaven_sorted_resolutions(resolutions)) or "none")
        + "; formats: "
        + (", ".join(sorted(formats)) or "none")
    )


def _polyhaven_select_texture_maps(files_data, resolution, file_format):
    """The map keys worth downloading, in the order they should be laid out."""
    selected = {}
    for key, role in POLYHAVEN_TEXTURE_MAPS.items():
        by_resolution = files_data.get(key)
        if not isinstance(by_resolution, dict):
            continue
        if file_format in by_resolution.get(resolution, {}):
            selected[key] = role

    # OpenGL-convention normals are what Blender's Normal Map node expects.
    # nor_dx is the same map with the green channel flipped, and is only worth
    # fetching for the few assets that ship no nor_gl.
    if "nor_gl" in selected:
        selected.pop("nor_dx", None)

    # A handful of textures name their albedo something other than "Diffuse" -
    # the multi-variant fabrics ship col_1/col_2/col_03 instead of one map.
    # Taking the first is a guess, but a material with no base colour at all is
    # the failure this whole table exists to prevent.
    if "base_color" not in selected.values():
        for key in sorted(files_data):
            if not key.lower().startswith(("col", "diff")):
                continue
            by_resolution = files_data.get(key)
            if isinstance(by_resolution, dict) and file_format in by_resolution.get(resolution, {}):
                selected[key] = "base_color"
                break

    return selected


def _polyhaven_set_colorspace(image, is_color_data):
    """Set a colorspace that exists on this Blender build.

    The names moved around in 4.0, so each candidate is tried in turn rather
    than assuming any one of them is present.
    """
    candidates = ("sRGB",) if is_color_data else ("Non-Color", "Linear Rec.709", "Linear")
    for name in candidates:
        try:
            image.colorspace_settings.name = name
            return name
        except Exception:
            continue
    return image.colorspace_settings.name


def _polyhaven_authors(asset_id):
    """Author names for an asset. Best effort - never fails an import."""
    with suppress(Exception):
        record = _polyhaven_asset_record(asset_id)
        authors = record.get("authors") or {}
        return sorted(authors) if isinstance(authors, dict) else list(authors)
    return []


def _polyhaven_tag(datablocks, asset_id, resolution=None, authors=None):
    """Record where a datablock came from, in the file that keeps it.

    Two jobs. It is the lookup key between downloading a texture and applying
    it - the old code recovered the map type by parsing the image's name, taking
    the last underscore-separated token, which turned "nor_gl" into "gl" and
    left the download path and set_texture disagreeing about what a map was
    called.

    It is also where the asset came from, in the same shape the Poly Pizza
    integration writes its polypizza_* properties. Poly Haven's assets are CC0
    and require no attribution, ever - but custom properties are saved into the
    .blend, so whoever opens the file in a year can still find the asset's page,
    who made it, and the resolutions they did not download.
    """
    for block in datablocks:
        if block is None:
            continue
        with suppress(Exception):
            block["polyhaven_id"] = asset_id
            block["polyhaven_url"] = _polyhaven_asset_url(asset_id)
            block["polyhaven_licence"] = "CC0"
            if resolution:
                block["polyhaven_resolution"] = resolution
            if authors:
                block["polyhaven_authors"] = ", ".join(authors)
            elif "polyhaven_authors" in block.keys():
                # The lookup is best-effort and comes back empty on any API
                # failure. Every other field is overwritten regardless, so
                # leaving a previous asset's artist behind on a datablock that
                # is being re-tagged would credit them for somebody else's work.
                del block["polyhaven_authors"]

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

# Tencent Cloud exposes Hunyuan-to-3D through two different services depending on where the
# account was created. Mainland accounts (cloud.tencent.com) use the AI3D 3.0 API. Tencent Cloud
# International accounts (tencentcloud.com) use the "Hunyuan-to-3D (Professional)" service on the
# older hunyuan API in ap-singapore; it rejects the mainland body fields and expects EnablePBR.
# Sending International credentials to the mainland endpoint fails with
# AuthFailure.SignatureFailure / ResourceUnavailable.
HUNYUAN_API_PROFILES = {
    "mainland": {
        "service": "ai3d",
        "version": "2025-05-13",
        "region": "ap-guangzhou",
        "submit_action": "SubmitHunyuanTo3DProJob",
        "query_action": "QueryHunyuanTo3DProJob",
        "submit_body": {},
    },
    "international_pro": {
        "service": "hunyuan",
        "version": "2023-09-01",
        "region": "ap-singapore",
        "submit_action": "SubmitHunyuanTo3DProJob",
        "query_action": "QueryHunyuanTo3DProJob",
        "submit_body": {"EnablePBR": True},
    },
}


def hunyuan_api_profile(international_pro: bool) -> dict:
    """Return a copy of the Tencent Cloud API profile for the selected account type."""
    profile = HUNYUAN_API_PROFILES["international_pro" if international_pro else "mainland"]
    return {**profile, "submit_body": dict(profile["submit_body"])}


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
            "describe_node_type": self.describe_node_type,
            "bpy_api_lookup": self.bpy_api_lookup,
            "drain_human_activity": self.drain_human_activity,
            "get_telemetry_consent": self.get_telemetry_consent,
            "set_telemetry_consent": self.set_telemetry_consent,
            "get_polyhaven_status": self.get_polyhaven_status,
            "get_hyper3d_status": self.get_hyper3d_status,
            "get_sketchfab_status": self.get_sketchfab_status,
            "get_polypizza_status": self.get_polypizza_status,
            "get_hunyuan3d_status": self.get_hunyuan3d_status,
            "export_scene": self.export_scene,
        }

        # Add Polyhaven handlers only if enabled
        if bpy.context.scene.blendermcp_use_polyhaven:
            polyhaven_handlers = {
                "get_polyhaven_categories": self.get_polyhaven_categories,
                "search_polyhaven_assets": self.search_polyhaven_assets,
                "download_polyhaven_asset": self.download_polyhaven_asset,
                "get_polyhaven_asset_preview": self.get_polyhaven_asset_preview,
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
                "import_generated_asset_hunyuan": self.import_generated_asset_hunyuan
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
                "describe_node_type",
                "bpy_api_lookup",
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
            # Give the caller the same detail we have: exception type, message,
            # and a full traceback (with line numbers into the submitted code),
            # instead of collapsing everything into one string. Callers that ran
            # a multi-line script otherwise cannot tell which line failed.
            tb = traceback.format_exc()
            raise Exception(
                json.dumps({
                    "exception_type": type(e).__name__,
                    "message": str(e),
                    "traceback": tb,
                })
            )

    # ------------------------------------------------------------------
    # Documentation / introspection helpers.
    #
    # These never touch the current scene or node tree - they exist purely
    # to answer "what does this thing look like" questions (property names,
    # types, enum values, socket order, function/operator signatures) so an
    # LLM can get a structured answer in one call instead of guessing and
    # discovering the shape of things via a chain of failed execute_code
    # attempts.
    # ------------------------------------------------------------------

    @staticmethod
    def _describe_property(prop):
        """Structured description of a single bpy RNA property."""
        entry = {
            "identifier": prop.identifier,
            "name": prop.name,
            "type": prop.type,  # FLOAT, INT, BOOLEAN, STRING, ENUM, POINTER, COLLECTION
            "description": prop.description,
        }
        for attr in ("is_required", "is_readonly", "is_argument_optional", "array_length"):
            value = getattr(prop, attr, None)
            if value is not None:
                entry[attr] = value

        if prop.type == 'ENUM':
            try:
                entry["enum_items"] = [item.identifier for item in prop.enum_items]
            except Exception:
                pass
            try:
                entry["default"] = prop.default
            except Exception:
                pass
        elif prop.type in ('FLOAT', 'INT'):
            try:
                entry["default"] = (
                    list(prop.default_array) if getattr(prop, "array_length", 0) else prop.default
                )
            except Exception:
                pass
            for attr in ("hard_min", "hard_max", "soft_min", "soft_max", "subtype", "unit", "step"):
                value = getattr(prop, attr, None)
                if value is not None:
                    entry[attr] = value
        elif prop.type == 'BOOLEAN':
            try:
                entry["default"] = prop.default
            except Exception:
                pass
        elif prop.type == 'STRING':
            try:
                entry["default"] = prop.default
            except Exception:
                pass
            max_length = getattr(prop, "max_length", None)
            if max_length:
                entry["max_length"] = max_length
        elif prop.type == 'POINTER':
            fixed_type = getattr(prop, "fixed_type", None)
            if fixed_type is not None:
                entry["pointer_type"] = fixed_type.identifier
        elif prop.type == 'COLLECTION':
            fixed_type = getattr(prop, "fixed_type", None)
            if fixed_type is not None:
                entry["collection_type"] = fixed_type.identifier
        return entry

    def describe_node_type(self, bl_idname, property_overrides=None):
        """Describe a node type's properties and socket schema.

        This is the fix for the single most common failure mode: guessing
        socket names/indices and enum values instead of looking them up.
        Since a node's sockets are only known once instantiated (and can
        depend on mode-like properties, e.g. Mix's `data_type`), this
        creates a throwaway node in a scratch node tree, optionally applies
        `property_overrides` first (e.g. {"data_type": "RGBA"}) so the
        caller can see the exact socket layout for the mode they intend to
        use, then reports its properties/inputs/outputs, and finally
        deletes the scratch tree. Nothing in the user's actual scene is
        touched.
        """
        node_cls = getattr(bpy.types, bl_idname, None)
        if node_cls is None or not (isinstance(node_cls, type) and issubclass(node_cls, bpy.types.Node)):
            candidates = [
                name for name in dir(bpy.types)
                if "Node" in name and bl_idname.lower() in name.lower()
            ]
            return {
                "error": f"Unknown node type: {bl_idname}",
                "did_you_mean": sorted(candidates)[:15],
            }

        tree_type_candidates = [
            "ShaderNodeTree", "GeometryNodeTree", "CompositorNodeTree", "TextureNodeTree",
        ]
        node = None
        tree = None
        used_tree_type = None
        attempts = []
        for tree_type in tree_type_candidates:
            tmp_tree = None
            try:
                tmp_tree = bpy.data.node_groups.new(name="__mcp_introspect_tmp__", type=tree_type)
                node = tmp_tree.nodes.new(type=bl_idname)
                tree = tmp_tree
                used_tree_type = tree_type
                break
            except Exception as e:
                attempts.append(f"{tree_type}: {e}")
                if tmp_tree is not None:
                    try:
                        bpy.data.node_groups.remove(tmp_tree)
                    except Exception:
                        pass

        if node is None:
            return {
                "error": f"Could not instantiate node '{bl_idname}' in any node tree type",
                "attempts": attempts,
            }

        try:
            warnings = []
            if property_overrides:
                for key, value in property_overrides.items():
                    try:
                        setattr(node, key, value)
                    except Exception as e:
                        warnings.append(f"Could not set property '{key}' = {value!r}: {e}")

            base_props = set(bpy.types.Node.bl_rna.properties.keys())
            properties = [
                self._describe_property(prop)
                for prop in node.bl_rna.properties
                if prop.identifier not in base_props
            ]

            def describe_sockets(sockets):
                out = []
                for index, socket in enumerate(sockets):
                    entry = {
                        "index": index,
                        "identifier": socket.identifier,
                        "name": socket.name,
                        "type": socket.type,
                        "is_multi_input": getattr(socket, "is_multi_input", False),
                        "hide_value": getattr(socket, "hide_value", False),
                        "is_linked": socket.is_linked,
                    }
                    if hasattr(socket, "default_value"):
                        try:
                            default_value = socket.default_value
                            if hasattr(default_value, "__len__") and not isinstance(default_value, str):
                                entry["default_value"] = list(default_value)
                            else:
                                entry["default_value"] = default_value
                        except Exception:
                            pass
                    out.append(entry)
                return out

            result = {
                "bl_idname": bl_idname,
                "label": node.bl_label,
                "instantiated_in": used_tree_type,
                "properties": properties,
                "inputs": describe_sockets(node.inputs),
                "outputs": describe_sockets(node.outputs),
                "applied_property_overrides": property_overrides or {},
                "note": (
                    "Sockets reflect the node's current property values (after any "
                    "property_overrides applied above). Enum/mode-like properties "
                    "(e.g. data_type, blend_type) can add, remove or reorder sockets - "
                    "pass the mode you intend to use via property_overrides to see the "
                    "real layout before writing code that indexes these sockets."
                ),
            }
            if warnings:
                result["warnings"] = warnings
            return result
        finally:
            try:
                bpy.data.node_groups.remove(tree)
            except Exception:
                pass

    def bpy_api_lookup(self, query):
        """Structured RNA reference lookup: types, properties, functions, operators.

        Accepts things like:
          - "ShaderNodeTexSky" or "bpy.types.ShaderNodeTexSky"       -> full type schema
          - "ShaderNodeTexSky.sky_type"                              -> one property, with enum items
          - "Object.ray_cast"                                        -> one method's parameters/returns
          - "bpy.ops.mesh.primitive_cube_add"                        -> operator parameters
        This replaces scraping `help()` text: every answer is structured
        JSON with real type names, enum identifiers, and required/optional
        flags, not something that has to be re-parsed out of a text blob.
        """
        query = (query or "").strip()
        if not query:
            return {"error": "Empty query"}

        q = query[4:] if query.startswith("bpy.") else query

        # bpy.ops.<category>.<operator_name>
        if q.startswith("ops."):
            op_parts = q[len("ops."):].split(".")
            op_parts = [p.split("(")[0] for p in op_parts if p]
            if len(op_parts) < 2:
                return {"error": f"Incomplete operator path: bpy.{q}. Expected bpy.ops.<category>.<name>"}
            category, op_name = op_parts[0], op_parts[1]
            op_group = getattr(bpy.ops, category, None)
            op = getattr(op_group, op_name, None) if op_group is not None else None
            if op is None:
                return {"error": f"Unknown operator: bpy.ops.{category}.{op_name}"}
            try:
                rna = op.get_rna_type()
            except Exception as e:
                return {"error": f"Could not introspect operator bpy.ops.{category}.{op_name}: {e}"}
            parameters = [
                self._describe_property(prop)
                for prop in rna.properties
                if prop.identifier != "rna_type"
            ]
            return {
                "kind": "operator",
                "idname": f"bpy.ops.{category}.{op_name}",
                "label": rna.name,
                "description": rna.description,
                "parameters": parameters,
            }

        parts = [p for p in q.split(".") if p and p != "types"]
        if not parts:
            return {"error": "Empty query"}

        type_name = parts[0]
        node_cls = getattr(bpy.types, type_name, None)
        if node_cls is None:
            matches = sorted(
                name for name in dir(bpy.types)
                if type_name.lower() in name.lower()
            )
            return {
                "error": f"Unknown type: {type_name}",
                "did_you_mean": matches[:15],
            }

        if len(parts) == 1:
            properties = [
                self._describe_property(prop)
                for prop in node_cls.bl_rna.properties
                if prop.identifier != "rna_type"
            ]
            functions = []
            for func in node_cls.bl_rna.functions:
                functions.append({
                    "identifier": func.identifier,
                    "description": func.description,
                    "parameters": [
                        self._describe_property(p) for p in func.parameters if not p.is_output
                    ],
                    "returns": [
                        self._describe_property(p) for p in func.parameters if p.is_output
                    ],
                })
            return {
                "kind": "type",
                "bl_idname": type_name,
                "description": node_cls.bl_rna.description,
                "properties": properties,
                "functions": functions,
            }

        # Type.member - could be a property or a function/method
        member_name = parts[1]
        prop = node_cls.bl_rna.properties.get(member_name)
        if prop is not None:
            entry = self._describe_property(prop)
            entry["kind"] = "property"
            entry["owner_type"] = type_name
            return entry

        func = node_cls.bl_rna.functions.get(member_name)
        if func is not None:
            return {
                "kind": "function",
                "owner_type": type_name,
                "identifier": func.identifier,
                "description": func.description,
                "parameters": [self._describe_property(p) for p in func.parameters if not p.is_output],
                "returns": [self._describe_property(p) for p in func.parameters if p.is_output],
            }

        available = sorted(
            list(node_cls.bl_rna.properties.keys()) + list(node_cls.bl_rna.functions.keys())
        )
        return {
            "error": f"'{type_name}' has no property or function named '{member_name}'",
            "did_you_mean": [name for name in available if member_name.lower() in name.lower()][:15],
        }

    def export_scene(self, filepath, format="glb", object_names=None, selection_only=False, apply_modifiers=True):
        """Export the whole scene, the current selection, or the named objects to a GLB or FBX file.

        Named objects are exported together with their children. GLB carries PBR
        materials, emission, skins, shape keys and animation; FBX is the fallback for
        tools that need Unity's built-in importer. apply_modifiers=False keeps rigs
        and shape keys intact. The file is written where the caller asked, so other
        applications (game engines, viewers) can pick it up without going through
        execute_code.
        """
        if not filepath:
            return {"error": "filepath is required"}
        fmt = (format or "glb").lower()
        if fmt not in ("glb", "fbx"):
            return {"error": f"format must be glb or fbx, got '{format}'"}

        names = [n for n in (object_names or []) if n]
        use_selection = False
        exported = []
        if names:
            missing = [n for n in names if bpy.data.objects.get(n) is None]
            if missing:
                return {"error": "Objects not found in Blender: " + ", ".join(missing)}
            bpy.ops.object.select_all(action='DESELECT')
            for n in names:
                obj = bpy.data.objects[n]
                for o in [obj, *obj.children_recursive]:
                    o.select_set(True)
                    if o.name not in exported:
                        exported.append(o.name)
            bpy.context.view_layer.objects.active = bpy.data.objects[names[0]]
            use_selection = True
        elif selection_only:
            if not bpy.context.selected_objects:
                return {"error": "Nothing is selected in Blender and no object_names were given"}
            exported = [o.name for o in bpy.context.selected_objects]
            use_selection = True
        else:
            exported = [o.name for o in bpy.context.scene.objects]

        try:
            if bpy.context.object and getattr(bpy.context.object, "mode", 'OBJECT') != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
        except Exception:
            pass

        directory = os.path.dirname(filepath)
        if directory:
            os.makedirs(directory, exist_ok=True)

        if fmt == "glb":
            bpy.ops.export_scene.gltf(
                filepath=filepath, export_format='GLB', use_selection=use_selection,
                use_active_scene=True, export_apply=apply_modifiers,
                export_animations=True, export_skins=True, export_morph=True, export_yup=True)
        else:
            bpy.ops.export_scene.fbx(
                filepath=filepath, use_selection=use_selection, apply_unit_scale=True,
                bake_space_transform=apply_modifiers, use_mesh_modifiers=apply_modifiers,
                path_mode='COPY', embed_textures=True)

        return {
            "path": filepath,
            "bytes": os.path.getsize(filepath),
            "selection_only": use_selection,
            "exported": exported,
        }

    def get_polyhaven_categories(self, asset_type):
        """Get the category taxonomy and attribute schema for an asset type."""
        try:
            if asset_type not in ["hdris", "textures", "models", "all"]:
                return {"error": f"Invalid asset type: {asset_type}. Must be one of: hdris, textures, models, all"}

            if asset_type == "all":
                # Three full trees at once is 30KB of paths, so this one is cut
                # to the top two levels. Filtering is inclusive, so those still
                # select everything beneath them.
                return {
                    "taxonomy": [
                        _polyhaven_taxonomy(one, depth=POLYHAVEN_TAXONOMY_DEPTH_ALL)
                        for one in ("hdris", "textures", "models")
                    ],
                    "truncated": True,
                }

            return {"taxonomy": [_polyhaven_taxonomy(asset_type)], "truncated": False}
        except Exception as e:
            return {"error": str(e)}

    def search_polyhaven_assets(self, asset_type=None, category=None, attributes=None,
                                query=None, limit=None):
        """Search for assets from Polyhaven with optional filtering"""
        try:
            params = {}

            if asset_type and asset_type != "all":
                if asset_type not in ["hdris", "textures", "models"]:
                    return {"error": f"Invalid asset type: {asset_type}. Must be one of: hdris, textures, models, all"}
                params["type"] = asset_type

            # `category`, not `categories`. The two are different filters over
            # different vocabularies: `categories` is the legacy flat tag list
            # ("outdoor", "man made", "floor"), while `category` takes the
            # single-path taxonomy that get_polyhaven_categories now returns
            # ("Metal/Sheet & Corrugated") and matches it inclusively, so a
            # parent selects everything beneath it. Sending a path to the legacy
            # parameter is answered with 200 and an empty object rather than an
            # error, so every filtered search came back silently empty.
            if category:
                params["category"] = category

            for key, value in (attributes or {}).items():
                if value is None or value == "":
                    continue
                if isinstance(value, bool):
                    value = "true" if value else "false"
                elif isinstance(value, (list, tuple)):
                    # Comma-separated values are OR'd together by the API.
                    value = ",".join(str(v) for v in value)
                params[str(key)] = str(value)

            try:
                limit = int(limit) if limit else POLYHAVEN_SEARCH_LIMIT
            except (TypeError, ValueError):
                limit = POLYHAVEN_SEARCH_LIMIT
            limit = max(1, min(limit, POLYHAVEN_SEARCH_MAX_LIMIT))

            try:
                assets = _polyhaven_api_get("assets", params=params, cache=True)
            except PolyHavenAPIError as e:
                if e.status == 400:
                    # The category and attribute filters answer an unrecognised
                    # value with 400 precisely so it is not a silent empty page.
                    return {"error": "Poly Haven did not recognise that category or attribute "
                                     "filter. Call get_polyhaven_categories for the values each "
                                     "asset type accepts."}
                raise

            # Trimmed and lower-cased so equivalent queries share a cache entry,
            # both here and at Poly Haven's edge.
            query = (query or "").strip().lower()
            note = None

            if query:
                try:
                    ranked = _polyhaven_search(query, asset_type)
                except PolyHavenAPIError as e:
                    if e.status == 429:
                        wait = f" Retry in {e.retry_after}s." if e.retry_after else ""
                        return {"error": f"Poly Haven is rate limiting searches from this "
                                         f"address.{wait}"}
                    if e.status != 503:
                        raise
                    # The API documents a 503 as "the query could not be
                    # embedded, fall back to your own keyword matching".
                    ranked = _polyhaven_keyword_match(query, assets)
                    note = ("Poly Haven's semantic search was unavailable, so these are plain "
                            "keyword matches and the ranking is weaker than usual.")

                # /search knows nothing about the category and attribute filters,
                # so its ranking is intersected with the filtered list here. That
                # is why the whole ranked list is asked for rather than the first
                # `limit` of it: filtering a page that the server already cut can
                # only shrink it, and the matches would be the ones further down.
                ordered = [slug for slug in ranked if slug in assets]
            else:
                # Rank before truncating. The previous order was whatever the API
                # happened to return, which is sorted by slug - and because models
                # are the only assets with capitalised slugs, the first 20 of an
                # unfiltered list were 20 models. asset_type="all" could not return
                # a single HDRI or texture, and the library's most downloaded assets
                # were unreachable by any call.
                ordered = sorted(
                    assets, key=lambda slug: assets[slug].get("download_count", 0), reverse=True)

            selected = ordered[:limit]

            return {
                "assets": [_polyhaven_summarize_asset(slug, assets[slug]) for slug in selected],
                # Everything matching every filter, so the count and the page it
                # heads describe the same population.
                "total_count": len(ordered),
                "returned_count": len(selected),
                "query": query or None,
                "note": note,
            }
        except Exception as e:
            return {"error": str(e)}
    def get_polyhaven_asset_preview(self, asset_id):
        """Fetch an asset's thumbnail, so it can be looked at before downloading.

        A thumbnail is a few hundred kilobytes against a 4k texture's 24MB, so
        checking one first is cheaper for everybody than importing the wrong rock.
        """
        try:
            if not _polyhaven_valid_slug(asset_id):
                return {"error": f"Invalid asset id: {asset_id!r}. Poly Haven slugs are "
                                 "letters, digits, underscores and hyphens."}

            record = _polyhaven_asset_record(asset_id)
            thumbnail_url = record.get("thumbnail_url")
            if not thumbnail_url:
                return {"error": f"No thumbnail is published for '{asset_id}'"}

            response = requests.get(
                _polyhaven_preview_url(thumbnail_url),
                headers=POLYHAVEN_HEADERS,
                timeout=POLYHAVEN_API_TIMEOUT,
            )
            if response.status_code >= 400:
                return {"error": f"Failed to fetch the thumbnail: HTTP {response.status_code}"}

            content_type = getattr(response, "headers", {}).get("Content-Type", "")
            image_format = "png" if "png" in content_type or ".png" in thumbnail_url else "jpeg"

            authors = record.get("authors") or {}
            return {
                "success": True,
                "image_data": base64.b64encode(response.content).decode("ascii"),
                "format": image_format,
                "asset_id": asset_id,
                "name": record.get("name") or asset_id,
                "authors": sorted(authors) if isinstance(authors, dict) else authors,
                "url": _polyhaven_asset_url(asset_id),
            }
        except Exception as e:
            traceback.print_exc()
            return {"error": f"Failed to get asset preview: {str(e)}"}

    def download_polyhaven_asset(self, asset_id, asset_type, resolution="1k", file_format=None):
        try:
            if asset_type not in POLYHAVEN_SUPPORTED_FORMATS:
                return {"error": f"Unsupported asset type: {asset_type}. Must be one of: hdris, textures, models"}

            if not _polyhaven_valid_slug(asset_id):
                return {"error": f"Invalid asset id: {asset_id!r}. Poly Haven slugs are "
                                 "letters, digits, underscores and hyphens."}

            supported = POLYHAVEN_SUPPORTED_FORMATS[asset_type]
            file_format = (file_format or POLYHAVEN_DEFAULT_FORMATS[asset_type]).lower()
            if file_format not in supported:
                # Rejected before any transfer. `usd` is listed for every model
                # and used to be downloaded in full before reaching the
                # "unsupported format" branch at the end of the import.
                return {
                    "error": f"Unsupported {asset_type} format: {file_format}. "
                             f"Supported formats: {', '.join(supported)}"
                }

            try:
                files_data = _polyhaven_api_get(f"files/{quote(asset_id, safe='')}")
            except Exception as e:
                return {"error": f"Failed to get asset files for '{asset_id}': {str(e)}"}

            if asset_type == "hdris":
                return self._polyhaven_import_hdri(asset_id, files_data, resolution, file_format)
            if asset_type == "textures":
                return self._polyhaven_import_texture(asset_id, files_data, resolution, file_format)
            return self._polyhaven_import_model(asset_id, files_data, resolution, file_format)

        except Exception as e:
            traceback.print_exc()
            return {"error": f"Failed to download asset: {str(e)}"}

    def _polyhaven_import_hdri(self, asset_id, files_data, resolution, file_format):
        """Download an HDRI and set it up as the scene's world."""
        file_info = files_data.get("hdri", {}).get(resolution, {}).get(file_format)
        if not file_info:
            return {
                "error": f"HDRI '{asset_id}' has no {resolution} {file_format} - "
                         f"{_polyhaven_available(files_data, 'hdris')}"
            }

        dest_dir = tempfile.mkdtemp(prefix="blender_mcp_polyhaven_")
        dest_path = os.path.join(dest_dir, f"{asset_id}_{resolution}.{file_format}")

        try:
            _polyhaven_download(file_info, dest_path)
        except Exception as e:
            shutil.rmtree(dest_dir, ignore_errors=True)
            return {"error": f"Failed to download HDRI: {str(e)}"}

        try:
            # A new world every time, rather than clearing the nodes of whatever
            # world is already there. The old code took bpy.data.worlds[0] - the
            # alphabetically first world datablock, very often somebody else's -
            # wiped its nodes and made it active, destroying hand-built setups
            # with no undo step to recover them. Using the scene's own world
            # instead would still have wiped it. This leaves the previous world
            # intact and simply unused; without a fake user Blender clears it up
            # on save if nothing else references it, and it is recoverable from
            # the outliner's orphan data until then.
            world = bpy.data.worlds.new(f"PolyHaven {asset_id}")
            bpy.context.scene.world = world

            world.use_nodes = True
            node_tree = world.node_tree
            node_tree.nodes.clear()

            tex_coord = node_tree.nodes.new(type='ShaderNodeTexCoord')
            tex_coord.location = (-800, 0)

            mapping = node_tree.nodes.new(type='ShaderNodeMapping')
            mapping.location = (-600, 0)

            env_tex = node_tree.nodes.new(type='ShaderNodeTexEnvironment')
            env_tex.location = (-400, 0)
            env_tex.image = bpy.data.images.load(dest_path, check_existing=True)
            env_tex.image.name = f"{asset_id}_{resolution}"
            # Colorspace is deliberately left as Blender's loader set it. It
            # already tags .hdr/.exr as scene-linear, and forcing "Non-Color"
            # here would mark radiance data as raw - identical under the stock
            # OCIO config, a colour shift under any config whose working space
            # is not Linear Rec.709.

            # Pack before anything can remove the file underneath it. Without
            # this the world points at a path in the OS temp directory for the
            # life of the .blend: it renders now, and is a missing image the
            # next time the file is opened here - or the first time it is opened
            # anywhere else.
            env_tex.image.pack()

            background = node_tree.nodes.new(type='ShaderNodeBackground')
            background.location = (-200, 0)

            output = node_tree.nodes.new(type='ShaderNodeOutputWorld')
            output.location = (0, 0)

            node_tree.links.new(tex_coord.outputs['Generated'], mapping.inputs['Vector'])
            node_tree.links.new(mapping.outputs['Vector'], env_tex.inputs['Vector'])
            node_tree.links.new(env_tex.outputs['Color'], background.inputs['Color'])
            node_tree.links.new(background.outputs['Background'], output.inputs['Surface'])

            bpy.context.scene.world = world

            authors = _polyhaven_authors(asset_id)
            _polyhaven_tag([world, env_tex.image], asset_id, resolution, authors)

            return {
                "success": True,
                "message": f"HDRI {asset_id} imported successfully",
                "image_name": env_tex.image.name,
                "world": world.name,
                "authors": authors,
                "url": _polyhaven_asset_url(asset_id),
            }
        except Exception as e:
            traceback.print_exc()
            return {"error": f"Failed to set up HDRI in Blender: {str(e)}"}
        finally:
            # The image is packed, so nothing needs the file any more.
            shutil.rmtree(dest_dir, ignore_errors=True)

    def _polyhaven_build_material(self, asset_id, maps):
        """Build a Principled material from {map_key: (role, image)}.

        Shared by download_polyhaven_asset and set_texture so there is exactly
        one place that decides which map drives which input - set_texture used
        to build its own tree in two passes over the same maps, silently
        replacing every link it had just made and leaving the first pass's
        Normal Map and Displacement nodes orphaned in the tree.
        """
        mat = bpy.data.materials.new(name=asset_id)
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        nodes.clear()

        output = nodes.new(type='ShaderNodeOutputMaterial')
        output.location = (600, 0)

        principled = nodes.new(type='ShaderNodeBsdfPrincipled')
        principled.location = (300, 0)
        links.new(principled.outputs[0], output.inputs['Surface'])

        tex_coord = nodes.new(type='ShaderNodeTexCoord')
        tex_coord.location = (-1000, 0)

        mapping = nodes.new(type='ShaderNodeMapping')
        mapping.location = (-800, 0)
        mapping.vector_type = 'TEXTURE'
        links.new(tex_coord.outputs['UV'], mapping.inputs['Vector'])

        y_pos = 300
        wired = []

        for map_key, (role, image) in maps.items():
            tex_node = nodes.new(type='ShaderNodeTexImage')
            tex_node.location = (-500, y_pos)
            tex_node.image = image
            _polyhaven_set_colorspace(image, is_color_data=role in POLYHAVEN_COLOR_ROLES)
            links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])
            y_pos -= 300

            if role == "base_color":
                links.new(tex_node.outputs['Color'], principled.inputs['Base Color'])
            elif role == "roughness":
                links.new(tex_node.outputs['Color'], principled.inputs['Roughness'])
            elif role == "metallic":
                links.new(tex_node.outputs['Color'], principled.inputs['Metallic'])
            elif role == "normal":
                normal_map = nodes.new(type='ShaderNodeNormalMap')
                normal_map.location = (-200, tex_node.location[1])
                links.new(tex_node.outputs['Color'], normal_map.inputs['Color'])
                links.new(normal_map.outputs['Normal'], principled.inputs['Normal'])
            elif role == "displacement":
                disp_node = nodes.new(type='ShaderNodeDisplacement')
                disp_node.location = (300, tex_node.location[1])
                # Poly Haven's displacement maps are centred on 0.5, and the
                # output is only used at all once the material is told to
                # displace - otherwise the node sits there connected and inert.
                disp_node.inputs['Midlevel'].default_value = 0.5
                disp_node.inputs['Scale'].default_value = 0.1
                links.new(tex_node.outputs['Color'], disp_node.inputs['Height'])
                links.new(disp_node.outputs['Displacement'], output.inputs['Displacement'])
                # Moved off material.cycles in Blender 4.1; try both so the
                # node is not left connected but inert on older versions.
                if hasattr(mat, "displacement_method"):
                    mat.displacement_method = 'BOTH'
                else:
                    with suppress(Exception):
                        mat.cycles.displacement_method = 'BOTH'
            else:
                continue

            wired.append(map_key)

        return mat, wired

    def _polyhaven_import_texture(self, asset_id, files_data, resolution, file_format):
        """Download a texture's maps and build a material from them."""
        wanted = _polyhaven_select_texture_maps(files_data, resolution, file_format)
        if not wanted:
            return {
                "error": f"Texture '{asset_id}' has no maps at {resolution} {file_format} - "
                         f"{_polyhaven_available(files_data, 'textures')}"
            }

        dest_dir = tempfile.mkdtemp(prefix="blender_mcp_polyhaven_")
        maps = {}

        try:
            for map_key, role in wanted.items():
                file_info = files_data[map_key][resolution][file_format]
                dest_path = os.path.join(
                    dest_dir, f"{asset_id}_{map_key}_{resolution}.{file_format}"
                )
                _polyhaven_download(file_info, dest_path)

                image = bpy.data.images.load(dest_path, check_existing=True)
                image.name = f"{asset_id}_{map_key}"
                _polyhaven_set_colorspace(image, is_color_data=role in POLYHAVEN_COLOR_ROLES)
                image.pack()
                maps[map_key] = (role, image)
        except Exception as e:
            traceback.print_exc()
            return {"error": f"Failed to download texture maps: {str(e)}"}
        finally:
            # Every image is packed, so nothing needs the files any more.
            shutil.rmtree(dest_dir, ignore_errors=True)

        try:
            mat, wired = self._polyhaven_build_material(asset_id, maps)

            # Deliberately no fake user. A material nothing has been applied to
            # is not being used, and Blender discarding it on save is the
            # correct outcome rather than a leak to guard against - the same
            # reasoning as the world this no longer keeps alive either. Call
            # set_texture to give it a real user.

            authors = _polyhaven_authors(asset_id)
            _polyhaven_tag(
                [mat] + [image for _role, image in maps.values()],
                asset_id,
                resolution=resolution,
                authors=authors,
            )
            for map_key, (role, image) in maps.items():
                with suppress(Exception):
                    image["polyhaven_map"] = map_key
                    image["polyhaven_role"] = role

            return {
                "success": True,
                "message": f"Texture {asset_id} imported as material",
                "material": mat.name,
                "maps": wired,
                "authors": authors,
                "url": _polyhaven_asset_url(asset_id),
            }
        except Exception as e:
            traceback.print_exc()
            return {"error": f"Failed to build material: {str(e)}"}

    def _polyhaven_fetch_model_files(self, files_data, resolution, file_format, dest_dir):
        """Download a model's main file and its sidecar textures into dest_dir."""
        file_info = files_data.get(file_format, {}).get(resolution, {}).get(file_format)
        if not file_info:
            return None

        main_file_path = os.path.join(dest_dir, os.path.basename(file_info["url"].split("?")[0]))
        _polyhaven_download(file_info, main_file_path)

        for include_path, include_info in (file_info.get("include") or {}).items():
            # Validate include_path - the API response controls these
            # dict keys; a malicious or MITM'd response could request an
            # absolute path or one containing ".." to escape dest_dir
            # and write arbitrary files (e.g. ~/.bashrc, authorized_keys).
            # Mirrors the zip-slip check in download_sketchfab_model.
            target_path = os.path.join(dest_dir, os.path.normpath(include_path))
            abs_dest_dir = os.path.abspath(dest_dir)
            abs_target_path = os.path.abspath(target_path)
            if (os.path.isabs(include_path)
                    or ".." in include_path
                    or not abs_target_path.startswith(abs_dest_dir + os.sep)):
                print(f"Skipping include with unsafe path: {include_path}")
                continue

            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            _polyhaven_download(include_info, target_path)

        return main_file_path

    def _polyhaven_append_blend(self, blend_path, asset_id):
        """Append the asset's own collection out of a Poly Haven model .blend.

        Every published model holds a collection named exactly the slug - it is
        an error in Poly Haven's own asset checker if it does not - and models
        with levels of detail carry them as `<slug>_LOD0`, `_LOD1` and so on
        beneath it. Appending `data_from.objects` wholesale, as this used to,
        linked every LOD on top of each other plus whatever else the file
        happened to hold, which for some assets is a second model.
        """
        with bpy.data.libraries.load(blend_path, link=False) as (data_from, data_to):
            available = list(data_from.collections)
            # LOD0 is the full-detail version. Taking it directly leaves the
            # coarser ones in the file rather than in the scene.
            wanted = next(
                (name for name in (f"{asset_id}_LOD0", asset_id) if name in available), None)
            if wanted:
                data_to.collections = [wanted]
            else:
                # Nothing to key off. Fall back to the old behaviour rather than
                # importing nothing at all.
                data_to.objects = data_from.objects

        linked = []
        for collection in data_to.collections:
            if collection is not None:
                bpy.context.scene.collection.children.link(collection)
                linked.append(collection)
        if not linked:
            for obj in data_to.objects:
                if obj is not None:
                    bpy.context.collection.objects.link(obj)
        return linked

    def _polyhaven_import_model(self, asset_id, files_data, resolution, file_format):
        """Download a model and its textures, then import it."""
        if not files_data.get(file_format, {}).get(resolution, {}).get(file_format):
            return {
                "error": f"Model {asset_id!r} has no {resolution} {file_format} - "
                         f"{_polyhaven_available(files_data, 'models')}"
            }

        dest_dir = tempfile.mkdtemp(prefix="blender_mcp_polyhaven_")
        fallback_note = ""
        collections = []

        try:
            main_file_path = self._polyhaven_fetch_model_files(
                files_data, resolution, file_format, dest_dir)
        except Exception as e:
            traceback.print_exc()
            shutil.rmtree(dest_dir, ignore_errors=True)
            return {"error": f"Failed to download model: {str(e)}"}

        # By name: bpy hands out a fresh Python wrapper per access, so holding on
        # to the datablocks themselves invites identity bugs.
        before = {obj.name for obj in bpy.data.objects}

        try:
            if file_format == "blend":
                written_by = _polyhaven_blend_version(main_file_path)
                if written_by and written_by > bpy.app.version[:2]:
                    raise RuntimeError("written by Blender %d.%d" % written_by)
                collections = self._polyhaven_append_blend(main_file_path, asset_id)
            else:
                bpy.ops.import_scene.gltf(filepath=main_file_path)
        except Exception as blend_error:
            if file_format != "blend":
                traceback.print_exc()
                shutil.rmtree(dest_dir, ignore_errors=True)
                return {"error": f"Failed to import model: {str(blend_error)}"}

            # A .blend written by a newer Blender than this one cannot be opened
            # at all, and Poly Haven's oldest models were saved in 2.93 while its
            # newest were saved in 5.0. glTF is a poorer record of the material,
            # but it is the difference between a worse model and no model.
            print(f"Poly Haven: .blend import failed ({blend_error}), falling back to glTF")
            shutil.rmtree(dest_dir, ignore_errors=True)
            dest_dir = tempfile.mkdtemp(prefix="blender_mcp_polyhaven_")
            fallback_note = (
                f" Imported from glTF rather than .blend, because the .blend was {blend_error}"
                f" and this is Blender {bpy.app.version_string.split()[0]}. Its materials are a"
                " conversion rather than the ones the artist built."
            )
            try:
                before = {obj.name for obj in bpy.data.objects}
                fallback_path = self._polyhaven_fetch_model_files(
                    files_data, resolution, POLYHAVEN_MODEL_FALLBACK_FORMAT, dest_dir)
                if not fallback_path:
                    raise RuntimeError(f"no {resolution} glTF is published for it")
                bpy.ops.import_scene.gltf(filepath=fallback_path)
            except Exception as e:
                traceback.print_exc()
                shutil.rmtree(dest_dir, ignore_errors=True)
                return {
                    "error": f"Model {asset_id!r} is {blend_error}, which this Blender cannot "
                             f"open, and the glTF fallback failed too: {str(e)}"
                }

        try:
            imported = [obj for obj in bpy.data.objects if obj.name not in before]
            imported_objects = [obj.name for obj in imported]
            if not imported_objects:
                return {"error": f"Imported {asset_id} but nothing arrived in the scene. "
                                 "The .blend may not hold the collection this expects."}

            # Appended and glTF-imported images still reference the files in the
            # temporary directory this deletes on the way out. A .glb carries its
            # textures inside it, but a .gltf with sidecar files does not, and an
            # appended .blend never does.
            materials = []
            for obj in imported:
                for slot in getattr(obj, "material_slots", []):
                    if slot.material is None:
                        continue
                    if slot.material not in materials:
                        materials.append(slot.material)
                    if not slot.material.use_nodes:
                        continue
                    for node in slot.material.node_tree.nodes:
                        if node.type == 'TEX_IMAGE' and node.image and not node.image.packed_file:
                            with suppress(Exception):
                                node.image.pack()

            authors = _polyhaven_authors(asset_id)
            _polyhaven_tag(imported + collections + materials, asset_id, resolution, authors)

            return {
                "success": True,
                "message": f"Model {asset_id} imported successfully.{fallback_note}",
                "imported_objects": imported_objects,
                "authors": authors,
                "url": _polyhaven_asset_url(asset_id),
            }
        except Exception as e:
            traceback.print_exc()
            return {"error": f"Failed to import model: {str(e)}"}
        finally:
            shutil.rmtree(dest_dir, ignore_errors=True)
    def _polyhaven_material_info(self, mat):
        """Summarise a material's node tree for the caller."""
        texture_nodes = []
        for node in mat.node_tree.nodes:
            if node.type != 'TEX_IMAGE' or node.image is None:
                continue
            connections = []
            for link in mat.node_tree.links:
                if link.from_node == node:
                    connections.append(
                        f"{link.from_socket.name} -> {link.to_node.name}.{link.to_socket.name}"
                    )
            texture_nodes.append({
                "name": node.name,
                "image": node.image.name,
                "colorspace": node.image.colorspace_settings.name,
                "connections": connections,
            })

        return {
            "has_nodes": mat.use_nodes,
            "node_count": len(mat.node_tree.nodes),
            "texture_nodes": texture_nodes,
        }

    def set_texture(self, object_name, texture_id):
        """Apply a previously downloaded Polyhaven texture to an object by creating a new material"""
        try:
            obj = bpy.data.objects.get(object_name)
            if not obj:
                return {"error": f"Object not found: {object_name}"}

            if not hasattr(obj, 'data') or not hasattr(obj.data, 'materials'):
                return {"error": f"Object {object_name} cannot accept materials"}

            if not _polyhaven_valid_slug(texture_id):
                return {"error": f"Invalid texture id: {texture_id!r}"}

            # Identified by the custom property stamped at download time rather
            # than by parsing the image's name. The old parser took the last
            # underscore-separated token, which turned "nor_gl" into "gl" and
            # left the two functions disagreeing about what a map was called.
            maps = {}
            for img in bpy.data.images:
                if img.get("polyhaven_id") != texture_id:
                    continue
                map_key = img.get("polyhaven_map")
                # Role first: assets whose albedo is not called "Diffuse" are
                # not in the table, but were resolved at download time.
                role = img.get("polyhaven_role") or POLYHAVEN_TEXTURE_MAPS.get(map_key)
                if not role:
                    continue
                if not img.packed_file:
                    img.pack()

                # An asset downloaded at more than one resolution leaves several
                # images per map, all carrying the same id. Take the largest
                # rather than whichever happened to come last.
                existing = maps.get(map_key)
                if existing and _polyhaven_resolution_rank(
                        existing[1].get("polyhaven_resolution")) >= _polyhaven_resolution_rank(
                        img.get("polyhaven_resolution")):
                    continue
                maps[map_key] = (role, img)

            if not maps:
                return {
                    "error": f"No texture images found for: {texture_id}. "
                             "Download it first with download_polyhaven_asset."
                }

            new_mat_name = f"{texture_id}_material_{object_name}"
            existing_mat = bpy.data.materials.get(new_mat_name)
            if existing_mat:
                bpy.data.materials.remove(existing_mat)

            new_mat, wired = self._polyhaven_build_material(texture_id, maps)
            new_mat.name = new_mat_name

            authors = _polyhaven_authors(texture_id)
            _polyhaven_tag([new_mat], texture_id, authors=authors)

            # Note: this replaces every material slot on the object.
            replaced = len(obj.data.materials)
            while len(obj.data.materials) > 0:
                obj.data.materials.pop(index=0)
            obj.data.materials.append(new_mat)

            bpy.context.view_layer.objects.active = obj
            obj.select_set(True)
            bpy.context.view_layer.update()

            message = f"Created new material and applied texture {texture_id} to {object_name}"
            if replaced:
                message += f" (replaced {replaced} existing material slot{'s' if replaced != 1 else ''})"

            return {
                "success": True,
                "message": message,
                "material": new_mat.name,
                "maps": wired,
                "material_info": self._polyhaven_material_info(new_mat),
                "authors": authors,
                "url": _polyhaven_asset_url(texture_id),
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
            profile = hunyuan_api_profile(
                getattr(bpy.context.scene, "blendermcp_hunyuan3d_intl_pro", False))
            service = profile["service"]
            action = profile["submit_action"]
            version = profile["version"]
            region = profile["region"]

            headParams={
                "Action": action,
                "Version": version,
                "Region": region,
            }

            # Constructing request parameters
            data = profile["submit_body"]

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
            
            profile = hunyuan_api_profile(
                getattr(bpy.context.scene, "blendermcp_hunyuan3d_intl_pro", False))
            service = profile["service"]
            action = profile["query_action"]
            version = profile["version"]
            region = profile["region"]

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
            server = getattr(bpy.types, "blendermcp_server", None)
            running_port = getattr(server, "port", scene.blendermcp_port)
            col.label(text=f"Connected on port {running_port}", icon='CHECKMARK')
            col.operator("blendermcp.stop_server", text="Disconnect", icon='X')
        else:
            col.label(text="Not connected", icon='RADIOBUT_OFF')
            col.prop(scene, "blendermcp_port")
            col.operator("blendermcp.start_server", text="Connect to MCP server", icon='PLAY')

        # Asset libraries
        layout.separator()
        layout.label(text="Asset Libraries", icon='ASSET_MANAGER')

        sub = self._integration_header(
            layout, scene, "blendermcp_use_polyhaven", "Poly Haven", 'WORLD')
        if sub:
            col = sub.column(align=True)
            col.label(text="Free CC0 HDRIs, textures and models")
            col.operator("wm.url_open", text="polyhaven.com", icon='URL').url = POLYHAVEN_SITE

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
                col.prop(scene, "blendermcp_hunyuan3d_intl_pro", text="International (Pro) account")
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
        global _user_stopped_server
        _user_stopped_server = False
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
        global _user_stopped_server
        _user_stopped_server = True
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

    bpy.types.Scene.blendermcp_hunyuan3d_intl_pro = bpy.props.BoolProperty(
        name="International (Pro)",
        description="Use the Tencent Cloud International 'Hunyuan-to-3D (Professional)' service "
                    "(hunyuan API, region ap-singapore, PBR enabled). Enable this when your SecretId/"
                    "SecretKey come from tencentcloud.com; leave it off for mainland AI3D 3.0 accounts",
        default=False
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

    # Add-on registration can run before Blender has a stable UI/scene context.
    # Defer socket startup and retry after startup-file or .blend loads.
    _blendermcp_register_auto_start()

    print("BlenderMCP addon registered")

def unregister():
    _blendermcp_unregister_auto_start()

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
    del bpy.types.Scene.blendermcp_hunyuan3d_intl_pro
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
