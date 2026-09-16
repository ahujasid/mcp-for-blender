"""Regression coverage for the Poly Haven integration.

The traps this file guards against were all found against the live API:

* The /files map keys are inconsistently cased and the casing is load-bearing -
  "Diffuse", "Rough" and "Displacement" are capitalised while "nor_gl" and "arm"
  are not. Matching them with `.lower() in ['normal', 'nor']` connected nothing,
  so every texture imported perfectly flat, and a sibling branch that forgot
  `.lower()` altogether did the same to displacement.
* Every map an asset offers used to be downloaded whether or not anything
  consumed it, costing roughly four times the bandwidth the material needed.
* The API lists a `usd` entry for every model. It passed the "is this format
  present?" guard, downloaded in full, and only then was rejected.
* HDRI images were never packed, so the world pointed at a file in the OS temp
  directory and the .blend lost its lighting the next time it was opened.

Every request here is mocked; the suite never touches the network.
"""
import hashlib
import importlib.util
import sys
import types

import pytest

from conftest import ROOT_ADDON as ADDON


# --- a fake Blender good enough to inspect a node tree -----------------------

# bl_idname -> node.type, for the nodes the Poly Haven paths create.
NODE_TYPES = {
    "ShaderNodeOutputMaterial": "OUTPUT_MATERIAL",
    "ShaderNodeBsdfPrincipled": "BSDF_PRINCIPLED",
    "ShaderNodeTexCoord": "TEX_COORD",
    "ShaderNodeMapping": "MAPPING",
    "ShaderNodeTexImage": "TEX_IMAGE",
    "ShaderNodeNormalMap": "NORMAL_MAP",
    "ShaderNodeDisplacement": "DISPLACEMENT",
    "ShaderNodeTexEnvironment": "TEX_ENVIRONMENT",
    "ShaderNodeBackground": "BACKGROUND",
    "ShaderNodeOutputWorld": "OUTPUT_WORLD",
}

NODE_INPUTS = {
    "ShaderNodeOutputMaterial": ["Surface", "Volume", "Displacement"],
    "ShaderNodeBsdfPrincipled": ["Base Color", "Metallic", "Roughness", "IOR", "Alpha", "Normal"],
    "ShaderNodeMapping": ["Vector", "Location", "Rotation", "Scale"],
    "ShaderNodeTexImage": ["Vector"],
    "ShaderNodeNormalMap": ["Strength", "Color"],
    "ShaderNodeDisplacement": ["Height", "Midlevel", "Scale", "Normal"],
    "ShaderNodeTexEnvironment": ["Vector"],
    "ShaderNodeBackground": ["Color", "Strength"],
    "ShaderNodeOutputWorld": ["Surface", "Volume"],
}

NODE_OUTPUTS = {
    "ShaderNodeBsdfPrincipled": ["BSDF"],
    "ShaderNodeTexCoord": ["Generated", "Normal", "UV", "Object", "Camera", "Window"],
    "ShaderNodeMapping": ["Vector"],
    "ShaderNodeTexImage": ["Color", "Alpha"],
    "ShaderNodeNormalMap": ["Normal"],
    "ShaderNodeDisplacement": ["Displacement"],
    "ShaderNodeTexEnvironment": ["Color"],
    "ShaderNodeBackground": ["Background"],
}

# Colorspaces a modern Blender build actually offers. _polyhaven_set_colorspace
# walks a list of candidates, so anything outside this set has to raise.
VALID_COLORSPACES = {"sRGB", "Non-Color", "Linear Rec.709", "ACEScg"}


class FakeSocket:
    def __init__(self, node, name):
        self.node = node
        self.name = name
        self.default_value = None


class FakeSocketCollection:
    def __init__(self, node, names):
        self.node = node
        self._order = list(names)
        self._sockets = {name: FakeSocket(node, name) for name in names}

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._sockets[self._order[key]]
        if key not in self._sockets:
            # Blender raises for a socket that does not exist, and so must this.
            # Inventing one on demand meant a typo'd socket name passed here
            # while every texture import failed in real Blender.
            raise KeyError(f"{self.node.bl_idname} has no socket {key!r}")
        return self._sockets[key]

    def __contains__(self, key):
        return key in self._sockets

    def __iter__(self):
        return iter(self._sockets[name] for name in self._order)


class FakeNode:
    def __init__(self, bl_idname):
        self.bl_idname = bl_idname
        self.type = NODE_TYPES.get(bl_idname, "UNKNOWN")
        self.name = bl_idname
        self.location = (0, 0)
        self.image = None
        self.vector_type = "POINT"
        self.inputs = FakeSocketCollection(self, NODE_INPUTS.get(bl_idname, []))
        self.outputs = FakeSocketCollection(self, NODE_OUTPUTS.get(bl_idname, []))


class FakeLink:
    def __init__(self, from_socket, to_socket):
        self.from_socket = from_socket
        self.to_socket = to_socket
        self.from_node = from_socket.node
        self.to_node = to_socket.node


class FakeNodes(list):
    def new(self, type=None):
        node = FakeNode(type)
        self.append(node)
        return node

    def clear(self):
        del self[:]


class FakeLinks(list):
    def new(self, from_socket, to_socket):
        # Blender allows one link per input socket: linking to an input that is
        # already connected replaces the existing link rather than adding to it.
        for existing in list(self):
            if existing.to_socket is to_socket:
                self.remove(existing)
        link = FakeLink(from_socket, to_socket)
        self.append(link)
        return link


class FakeNodeTree:
    def __init__(self):
        self.nodes = FakeNodes()
        self.links = FakeLinks()


class CustomPropMixin:
    def __setitem__(self, key, value):
        self.custom_properties[key] = value

    def __delitem__(self, key):
        del self.custom_properties[key]

    def keys(self):
        return self.custom_properties.keys()

    def __getitem__(self, key):
        return self.custom_properties[key]

    def get(self, key, default=None):
        return self.custom_properties.get(key, default)


class FakeColorspace:
    def __init__(self):
        self._name = "sRGB"

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        if value not in VALID_COLORSPACES:
            raise TypeError(f"enum {value!r} not found")
        self._name = value


class FakeImage(CustomPropMixin):
    def __init__(self, filepath):
        self.filepath = filepath
        self.name = filepath.replace("\\", "/").rsplit("/", 1)[-1]
        self.colorspace_settings = FakeColorspace()
        self.packed_file = None
        self.custom_properties = {}

    def pack(self):
        self.packed_file = object()


class FakeMaterial(CustomPropMixin):
    def __init__(self, name):
        self.name = name
        self.use_nodes = False
        self.use_fake_user = False
        self.displacement_method = "BUMP"
        self.node_tree = FakeNodeTree()
        self.custom_properties = {}


class FakeWorld(CustomPropMixin):
    def __init__(self, name):
        self.name = name
        self.use_nodes = False
        self.use_fake_user = False
        self.node_tree = FakeNodeTree()
        self.custom_properties = {}


class FakeImages(list):
    def load(self, filepath, check_existing=False):
        if check_existing:
            for image in self:
                if image.filepath == filepath:
                    return image
        image = FakeImage(filepath)
        self.append(image)
        return image


class FakeMaterials(list):
    def new(self, name):
        material = FakeMaterial(name)
        self.append(material)
        return material

    def get(self, name):
        return next((m for m in self if m.name == name), None)


class FakeWorlds(list):
    def new(self, name):
        world = FakeWorld(name)
        self.append(world)
        return world


class FakeMaterialSlots(list):
    def pop(self, index=0):
        # bpy collections take index as a keyword; a plain list does not.
        return super().pop(index)


class FakeCollection(CustomPropMixin):
    def __init__(self, name):
        self.name = name
        self.objects = FakeObjects()
        self.children = FakeCollections()
        self.custom_properties = {}


class FakeCollections(list):
    def get(self, name):
        return next((c for c in self if c.name == name), None)

    def new(self, name):
        collection = FakeCollection(name)
        self.append(collection)
        return collection

    def link(self, collection):
        self.append(collection)


class FakeLibraryLoad:
    """bpy.data.libraries.load: names go in, appended datablocks come out.

    Inside the `with` block data_from lists what the file holds, by name. You
    assign the names you want to data_to, and on exit Blender has replaced them
    with the datablocks it appended.
    """

    def __init__(self, data, contents):
        self._data = data
        self._contents = contents

    def __enter__(self):
        self.data_from = types.SimpleNamespace(
            collections=list(self._contents.get("collections", {})),
            objects=list(self._contents.get("objects", [])),
        )
        self.data_to = types.SimpleNamespace(collections=[], objects=[])
        return self.data_from, self.data_to

    def __exit__(self, *_exc):
        appended = []
        for name in self.data_to.collections:
            collection = self._data.collections.new(name)
            for obj_name in self._contents["collections"][name]:
                obj = FakeObject(obj_name)
                self._data.objects.append(obj)
                collection.objects.append(obj)
            appended.append(collection)
        self.data_to.collections = appended

        objects = []
        for name in self.data_to.objects:
            obj = FakeObject(name)
            self._data.objects.append(obj)
            objects.append(obj)
        self.data_to.objects = objects
        return False


class FakeLibraries:
    """Serves whatever BLEND_CONTENTS says the downloaded file holds."""

    def __init__(self, data):
        self._data = data
        self.contents = {}

    def load(self, filepath, link=False):
        assert link is False, "Poly Haven models are appended, not linked"
        return FakeLibraryLoad(self._data, self.contents)


class FakeMesh:
    def __init__(self):
        self.materials = FakeMaterialSlots()


class FakeObject(CustomPropMixin):
    def __init__(self, name):
        self.name = name
        self.type = "MESH"
        self.data = FakeMesh()
        self.custom_properties = {}
        self.selected = False

    def select_set(self, value):
        self.selected = value


class FakeObjects(list):
    def get(self, name):
        return next((o for o in self if o.name == name), None)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b"", streamed=False, headers=None):
        self.status_code = status_code
        self._payload = payload
        self._content = content
        self._streamed = streamed
        self.headers = dict(headers or {})

    @property
    def content(self):
        # A streamed body must be consumed with iter_content. Serving .content
        # here too would let the whole-file-into-memory regression - a 24k EXR
        # is 2.4GB - pass the suite unnoticed.
        if self._streamed:
            raise AssertionError(
                "a streamed download must use iter_content(), not response.content"
            )
        return self._content

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self._content), chunk_size):
            yield self._content[start:start + chunk_size]


def _load_addon(monkeypatch):
    bpy = types.ModuleType("bpy")

    bpy.data = types.SimpleNamespace(
        images=FakeImages(),
        materials=FakeMaterials(),
        worlds=FakeWorlds(),
        objects=FakeObjects(),
        collections=FakeCollections(),
    )
    bpy.data.libraries = FakeLibraries(bpy.data)
    scene = types.SimpleNamespace(
        world=None,
        collection=types.SimpleNamespace(children=FakeCollections(), objects=FakeObjects()),
        blendermcp_use_polyhaven=True,
        blendermcp_use_hyper3d=False,
        blendermcp_use_hunyuan3d=False,
        blendermcp_use_sketchfab=False,
        blendermcp_use_polypizza=False,
    )
    bpy.context = types.SimpleNamespace(
        scene=scene,
        selected_objects=[],
        collection=types.SimpleNamespace(objects=types.SimpleNamespace(link=lambda _o: None)),
        view_layer=types.SimpleNamespace(
            update=lambda: None,
            objects=types.SimpleNamespace(active=None),
        ),
    )
    bpy.ops = types.SimpleNamespace(
        import_scene=types.SimpleNamespace(
            gltf=lambda **_kwargs: None, fbx=lambda **_kwargs: None
        )
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

    spec = importlib.util.spec_from_file_location("blender_mcp_polyhaven_test", ADDON)
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    return addon


# --- fixtures in the shape the live API returns ------------------------------

CDN = "https://dl.polyhaven.org/file/ph-assets"

# Filled in by _file(): every fixture URL mapped to the bytes it serves, so the
# md5 verification in _polyhaven_download is exercised for real.
BODY_BY_URL = {}


def _file(url, payload):
    BODY_BY_URL[url] = payload
    return {"url": url, "md5": hashlib.md5(payload).hexdigest(), "size": len(payload)}


def _texture_map(slug, name):
    # Distinct bodies per map, so a mixed-up map surfaces as a checksum failure
    # rather than quietly passing.
    payload = f"{slug}-{name}-bytes".encode()
    return {"1k": {"jpg": _file(f"{CDN}/Textures/jpg/1k/{slug}/{slug}_{name}_1k.jpg", payload)}}


METAL_SLUG = "metal_plate"
METAL_FILES = {
    "Diffuse": _texture_map(METAL_SLUG, "diff"),
    "Rough": _texture_map(METAL_SLUG, "rough"),
    "Metal": _texture_map(METAL_SLUG, "metal"),
    "nor_gl": _texture_map(METAL_SLUG, "nor_gl"),
    "arm": _texture_map(METAL_SLUG, "arm"),
}

TEXTURE_SLUG = "rock_wall_10"
TEXTURE_FILES = {
    "Diffuse": _texture_map(TEXTURE_SLUG, "diff"),
    "nor_gl": _texture_map(TEXTURE_SLUG, "nor_gl"),
    "nor_dx": _texture_map(TEXTURE_SLUG, "nor_dx"),
    "Rough": _texture_map(TEXTURE_SLUG, "rough"),
    "Displacement": _texture_map(TEXTURE_SLUG, "disp"),
    "AO": _texture_map(TEXTURE_SLUG, "ao"),
    "arm": _texture_map(TEXTURE_SLUG, "arm"),
    # Containers, which carry their own format key rather than jpg/png/exr.
    "blend": {"1k": {"blend": _file(f"{CDN}/Textures/blend/1k/x.blend", b"blend")}},
    "gltf": {"1k": {"gltf": _file(f"{CDN}/Textures/gltf/1k/x.gltf", b"gltf")}},
    "mtlx": {"1k": {"mtlx": _file(f"{CDN}/Textures/mtlx/1k/x.mtlx", b"mtlx")}},
}

# A multi-variant fabric: three colour options and no map called "Diffuse".
VARIANT_FILES = {
    "col_1": _texture_map("fabric_pattern_07", "col_1"),
    "col_2": _texture_map("fabric_pattern_07", "col_2"),
    "col_03": _texture_map("fabric_pattern_07", "col_03"),
    "nor_gl": _texture_map("fabric_pattern_07", "nor_gl"),
    "Rough": _texture_map("fabric_pattern_07", "rough"),
    "AO": _texture_map("fabric_pattern_07", "ao"),
}

# An older asset that only ever shipped DirectX-convention normals.
DX_ONLY_FILES = {
    "Diffuse": _texture_map("dx_only", "diff"),
    "nor_dx": _texture_map("dx_only", "nor_dx"),
    "Rough": _texture_map("dx_only", "rough"),
}

HDRI_SLUG = "kloofendal_43d_clear_puresky"
HDRI_FILES = {
    "hdri": {
        "1k": {"hdr": _file(f"{CDN}/HDRIs/hdr/1k/{HDRI_SLUG}_1k.hdr", b"fake-radiance-1k")},
        "4k": {"hdr": _file(f"{CDN}/HDRIs/hdr/4k/{HDRI_SLUG}_4k.hdr", b"fake-radiance-4k")},
        "8k": {"hdr": _file(f"{CDN}/HDRIs/hdr/8k/{HDRI_SLUG}_8k.hdr", b"fake-radiance-8k")},
    },
    # A flat file object, not a resolution map - it must not be mistaken for one.
    "tonemapped": _file(f"{CDN}/HDRIs/extra/tonemapped.jpg", b"tonemapped"),
}

MODEL_SLUG = "potted_plant_02"
def _blend_with_includes(includes):
    entry = _file(f"{CDN}/Models/blend/1k/{MODEL_SLUG}_hostile.blend",
                  _blend_bytes(b"BLENDER-v293"))
    entry["include"] = {
        path: _file(f"{CDN}/Models/blend/1k/{MODEL_SLUG}/{i}.png", f"include-{i}".encode())
        for i, path in enumerate(includes)
    }
    return {"1k": {"blend": entry}}


def _blend_bytes(header):
    """A .blend prefix the header parser can read. Published files are
    compressed, but the parser handles that separately and this keeps the
    fixture legible."""
    return header + b"REND" + b"\x00" * 32


# Up to Blender 4.4 the version is three characters, from 4.5 it is four and the
# header carries its own length. Both layouts are in the published library:
# the oldest models were saved in 2.93, the newest in 5.0.
BLEND_HEADER_293 = b"BLENDER-v293"
BLEND_HEADER_500 = b"BLENDER17-01v0500"

MODEL_FILES = {
    "blend": {"1k": {"blend": _file(f"{CDN}/Models/blend/1k/{MODEL_SLUG}.blend",
                                    _blend_bytes(BLEND_HEADER_293))}},
    "gltf": {"1k": {"gltf": _file(f"{CDN}/Models/gltf/1k/{MODEL_SLUG}.gltf", b"gltf-bytes")}},
    "fbx": {"1k": {"fbx": _file(f"{CDN}/Models/fbx/1k/{MODEL_SLUG}.fbx", b"fbx-bytes")}},
    # Listed by the API for every model, and not something we can import.
    "usd": {"1k": {"usd": _file(f"{CDN}/Models/usd/1k/{MODEL_SLUG}.usdc", b"usd-bytes" * 500)}},
}

# A model saved by a Blender newer than the one running: unopenable, so the
# import falls back to glTF rather than failing.
FUTURE_MODEL_FILES = {
    "blend": {"1k": {"blend": _file(f"{CDN}/Models/blend/1k/future.blend",
                                    _blend_bytes(BLEND_HEADER_500))}},
    "gltf": {"1k": {"gltf": _file(f"{CDN}/Models/gltf/1k/future.gltf", b"future-gltf-bytes")}},
}

# The same, for the one published model that has no glTF at all.
FUTURE_ONLY_BLEND_FILES = {
    "blend": {"1k": {"blend": _file(f"{CDN}/Models/blend/1k/lonely.blend",
                                    _blend_bytes(BLEND_HEADER_500))}},
}

# What the downloaded .blend is pretending to contain. Every published model has
# a collection named exactly the slug, and models with levels of detail carry
# them beneath it.
MODEL_WITH_LODS = {
    "collections": {
        MODEL_SLUG: [],
        f"{MODEL_SLUG}_LOD0": [f"{MODEL_SLUG}_pot_LOD0", f"{MODEL_SLUG}_leaves_LOD0"],
        f"{MODEL_SLUG}_LOD1": [f"{MODEL_SLUG}_pot_LOD1", f"{MODEL_SLUG}_leaves_LOD1"],
        f"{MODEL_SLUG}_LOD2": [f"{MODEL_SLUG}_pot_LOD2", f"{MODEL_SLUG}_leaves_LOD2"],
    },
    "objects": [f"{MODEL_SLUG}_pot_LOD0", f"{MODEL_SLUG}_leaves_LOD0",
                f"{MODEL_SLUG}_pot_LOD1", f"{MODEL_SLUG}_leaves_LOD1",
                f"{MODEL_SLUG}_pot_LOD2", f"{MODEL_SLUG}_leaves_LOD2"],
}

# Several published models ship a second, unrelated model alongside the asset -
# wooden_ladder's file also holds wooden_step_ladder.
MODEL_WITH_A_STOWAWAY = {
    "collections": {
        MODEL_SLUG: [f"{MODEL_SLUG}_pot", f"{MODEL_SLUG}_leaves"],
        "someone_elses_model": ["someone_elses_model"],
    },
    "objects": [f"{MODEL_SLUG}_pot", f"{MODEL_SLUG}_leaves", "someone_elses_model"],
}

# A response whose include keys try to escape the download directory - the
# arbitrary-file-write reported as issue #257.
HOSTILE_MODEL_FILES = {
    "blend": _blend_with_includes([
        "textures/fine.png",
        "../../../../evil.png",
        "/tmp/absolute.png",
        "textures/../../escape.png",
    ]),
}

INFO = {"authors": {"Rob Tuytel": "All"}, "name": "Rock Wall 10"}


ASSETS_ETAG = 'W/"a1b2c3"'


def _install_requests(monkeypatch, addon, files=None, info=None, assets=None, corrupt=False):
    """Route requests by URL, and record every one of them.

    /assets answers with an ETag and honours If-None-Match, the way the real API
    does, so the cache is exercised rather than assumed.
    """
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None, stream=False):
        calls.append({
            "url": url,
            "params": dict(params or {}),
            "stream": stream,
            "timeout": timeout,
            "headers": dict(headers or {}),
        })
        if stream:
            body = BODY_BY_URL.get(url)
            if body is None:
                return FakeResponse(status_code=404)
            return FakeResponse(content=b"truncated" if corrupt else body, streamed=True)
        if "/files/" in url:
            return FakeResponse(payload=files if files is not None else {})
        if "/info/" in url:
            return FakeResponse(payload=info if info is not None else INFO)
        if url.endswith("/assets"):
            if (headers or {}).get("If-None-Match") == ASSETS_ETAG:
                return FakeResponse(status_code=304, headers={"ETag": ASSETS_ETAG})
            return FakeResponse(payload=assets if assets is not None else {},
                                headers={"ETag": ASSETS_ETAG})
        return FakeResponse(payload={})

    monkeypatch.setattr(addon.requests, "get", fake_get, raising=False)
    return calls


@pytest.fixture
def server(monkeypatch, tmp_path):
    addon = _load_addon(monkeypatch)
    # Downloads go to a fresh mkdtemp that the addon removes on the way out;
    # rooting it under tmp_path makes a leaked directory visible to the test.
    monkeypatch.setattr(addon.tempfile, "gettempdir", lambda: str(tmp_path))
    return addon, addon.BlenderMCPServer()


def _downloaded(calls):
    return [call["url"] for call in calls if call["stream"]]


def _node_of_type(tree, node_type):
    return next((n for n in tree.nodes if n.type == node_type), None)


def _link_into(tree, node, socket_name):
    """The single link feeding `socket_name` on `node`, or None."""
    matches = [l for l in tree.links if l.to_node is node and l.to_socket.name == socket_name]
    assert len(matches) <= 1, f"{socket_name} has {len(matches)} links; Blender allows one"
    return matches[0] if matches else None


def _material(addon, result):
    return addon.bpy.data.materials.get(result["material"])


# --- texture maps: what actually gets connected ------------------------------

def test_normal_map_is_connected_to_the_principled_normal_input(server, monkeypatch):
    """The API names these maps nor_gl and nor_dx, so a branch matching 'nor' or
    'normal' never fired: the image was downloaded, a texture node was created
    and wired to the Mapping node, and then nothing consumed it."""
    addon, srv = server
    _install_requests(monkeypatch, addon, files=TEXTURE_FILES)

    result = srv.download_polyhaven_asset(TEXTURE_SLUG, "textures", "1k", "jpg")
    assert result.get("success"), result

    tree = _material(addon, result).node_tree
    principled = _node_of_type(tree, "BSDF_PRINCIPLED")

    normal_link = _link_into(tree, principled, "Normal")
    assert normal_link is not None, "the normal map is not connected to anything"
    assert normal_link.from_node.type == "NORMAL_MAP"

    feeding_the_normal_map = _link_into(tree, normal_link.from_node, "Color")
    assert feeding_the_normal_map.from_node.image.name.endswith("nor_gl")


def test_displacement_is_connected_to_the_material_output(server, monkeypatch):
    """The displacement branch was the one `elif` in the chain missing .lower(),
    and the API's key is "Displacement"."""
    addon, srv = server
    _install_requests(monkeypatch, addon, files=TEXTURE_FILES)

    result = srv.download_polyhaven_asset(TEXTURE_SLUG, "textures", "1k", "jpg")
    tree = _material(addon, result).node_tree
    output = _node_of_type(tree, "OUTPUT_MATERIAL")

    disp_link = _link_into(tree, output, "Displacement")
    assert disp_link is not None, "the displacement map is not connected to anything"
    assert disp_link.from_node.type == "DISPLACEMENT"
    assert disp_link.from_node.inputs["Midlevel"].default_value == 0.5
    # A Displacement node connected to a material that is not told to displace
    # does nothing at all.
    assert _material(addon, result).displacement_method == "BOTH"


def test_base_colour_roughness_and_metallic_are_connected(server, monkeypatch):
    addon, srv = server
    _install_requests(monkeypatch, addon, files=TEXTURE_FILES)

    result = srv.download_polyhaven_asset(TEXTURE_SLUG, "textures", "1k", "jpg")
    tree = _material(addon, result).node_tree
    principled = _node_of_type(tree, "BSDF_PRINCIPLED")

    assert _link_into(tree, principled, "Base Color").from_node.image.name.endswith("Diffuse")
    assert _link_into(tree, principled, "Roughness").from_node.image.name.endswith("Rough")
    assert set(result["maps"]) == {"Diffuse", "Rough", "Displacement", "nor_gl"}


# --- what gets downloaded ----------------------------------------------------

def test_only_the_maps_the_material_uses_are_downloaded(server, monkeypatch):
    """AO, arm, nor_dx and the blend/gltf/mtlx containers were all fetched and
    then left unconnected. For a real 1k texture that was 7.4MB of transfer to
    build a material that needed 1.9MB."""
    addon, srv = server
    calls = _install_requests(monkeypatch, addon, files=TEXTURE_FILES)

    srv.download_polyhaven_asset(TEXTURE_SLUG, "textures", "1k", "jpg")

    downloaded = _downloaded(calls)
    assert len(downloaded) == 4
    for unused in ("_ao_", "_arm_", "_nor_dx_", ".blend", ".gltf", ".mtlx"):
        assert not any(unused in url for url in downloaded), f"{unused} should not be fetched"


def test_nor_dx_is_used_only_when_the_asset_has_no_nor_gl(server, monkeypatch):
    """OpenGL-convention normals are what Blender's Normal Map node expects, so
    nor_dx is a fallback rather than a second download."""
    addon, srv = server
    calls = _install_requests(monkeypatch, addon, files=DX_ONLY_FILES)

    result = srv.download_polyhaven_asset("dx_only", "textures", "1k", "jpg")
    assert result.get("success"), result

    tree = _material(addon, result).node_tree
    principled = _node_of_type(tree, "BSDF_PRINCIPLED")
    normal_link = _link_into(tree, principled, "Normal")
    assert normal_link is not None
    assert any("_nor_dx_" in url for url in _downloaded(calls))


def test_an_albedo_not_called_diffuse_is_still_connected(server, monkeypatch):
    """A few textures ship col_1/col_2/col_03 variants instead of one Diffuse.
    Exact-matching the map table would leave those materials with no base
    colour at all - the exact failure the table exists to prevent."""
    addon, srv = server
    calls = _install_requests(monkeypatch, addon, files=VARIANT_FILES)

    result = srv.download_polyhaven_asset("fabric_pattern_07", "textures", "1k", "jpg")
    assert result.get("success"), result

    tree = _material(addon, result).node_tree
    principled = _node_of_type(tree, "BSDF_PRINCIPLED")
    base_colour = _link_into(tree, principled, "Base Color")
    assert base_colour is not None, "no base colour was connected"

    # One variant, not all three.
    assert len([url for url in _downloaded(calls) if "_col" in url]) == 1


def test_downloads_are_cleaned_up(server, monkeypatch, tmp_path):
    """Every image is packed into the .blend, so nothing needs the files
    afterwards. The old code leaked them: its cleanup called tempfile._cleanup(),
    which does not exist in Python 3."""
    addon, srv = server
    _install_requests(monkeypatch, addon, files=TEXTURE_FILES)

    assert srv.download_polyhaven_asset(TEXTURE_SLUG, "textures", "1k", "jpg").get("success")
    left = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert left == [], f"texture import left files behind: {left}"

    _install_requests(monkeypatch, addon, files=HDRI_FILES)
    assert srv.download_polyhaven_asset(HDRI_SLUG, "hdris", "1k", "hdr").get("success")
    left = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert left == [], f"hdri import left files behind: {left}"


def test_every_request_carries_a_timeout(server, monkeypatch):
    """Without one, a stalled connection hangs Blender's main thread forever -
    there is no progress bar and no way to cancel."""
    addon, srv = server
    calls = _install_requests(monkeypatch, addon, files=TEXTURE_FILES)

    srv.download_polyhaven_asset(TEXTURE_SLUG, "textures", "1k", "jpg")

    assert calls
    assert all(call["timeout"] is not None for call in calls)


def test_a_corrupt_download_is_rejected(server, monkeypatch):
    addon, srv = server
    _install_requests(monkeypatch, addon, files=TEXTURE_FILES, corrupt=True)

    result = srv.download_polyhaven_asset(TEXTURE_SLUG, "textures", "1k", "jpg")

    assert "error" in result
    assert "Checksum mismatch" in result["error"]


# --- colour management -------------------------------------------------------

def test_only_the_albedo_is_treated_as_colour(server, monkeypatch):
    addon, srv = server
    _install_requests(monkeypatch, addon, files=TEXTURE_FILES)

    srv.download_polyhaven_asset(TEXTURE_SLUG, "textures", "1k", "jpg")

    by_name = {img.name: img.colorspace_settings.name for img in addon.bpy.data.images}
    assert by_name[f"{TEXTURE_SLUG}_Diffuse"] == "sRGB"
    for map_key in ("Rough", "Displacement", "nor_gl"):
        assert by_name[f"{TEXTURE_SLUG}_{map_key}"] == "Non-Color"


# --- the material has to survive a save --------------------------------------

def test_a_downloaded_material_is_not_given_a_fake_user(server, monkeypatch):
    """A material nothing has been applied to is not in use, so Blender
    discarding it on save is the right outcome. Pinning it with a fake user
    would keep every downloaded-but-unused texture, and its packed images, in
    the file forever - 24MB apiece at 4k, invisible to whoever saved it."""
    addon, srv = server
    _install_requests(monkeypatch, addon, files=TEXTURE_FILES)

    result = srv.download_polyhaven_asset(TEXTURE_SLUG, "textures", "1k", "jpg")

    assert _material(addon, result).use_fake_user is False


def test_texture_images_are_packed(server, monkeypatch):
    addon, srv = server
    _install_requests(monkeypatch, addon, files=TEXTURE_FILES)

    srv.download_polyhaven_asset(TEXTURE_SLUG, "textures", "1k", "jpg")

    assert all(img.packed_file is not None for img in addon.bpy.data.images)


# --- provenance --------------------------------------------------------------

def test_images_are_tagged_so_set_texture_can_find_them(server, monkeypatch):
    """The lookup key between downloading a texture and applying it. The old
    code parsed it out of the image name, which turned "nor_gl" into "gl"."""
    addon, srv = server
    _install_requests(monkeypatch, addon, files=TEXTURE_FILES)

    result = srv.download_polyhaven_asset(TEXTURE_SLUG, "textures", "1k", "jpg")
    mat = _material(addon, result)

    assert mat["polyhaven_id"] == TEXTURE_SLUG
    assert mat["polyhaven_resolution"] == "1k"

    for image in addon.bpy.data.images:
        assert image["polyhaven_id"] == TEXTURE_SLUG
        assert image["polyhaven_map"] in addon.POLYHAVEN_TEXTURE_MAPS


# --- HDRIs -------------------------------------------------------------------

def test_hdri_image_is_packed_so_the_blend_survives_reopening(server, monkeypatch):
    """The image used to be left pointing at a file in the OS temp directory,
    which made the .blend render correctly now and lose its lighting later."""
    addon, srv = server
    _install_requests(monkeypatch, addon, files=HDRI_FILES)

    result = srv.download_polyhaven_asset(HDRI_SLUG, "hdris", "1k", "hdr")
    assert result.get("success"), result

    image = next(img for img in addon.bpy.data.images if img.name == result["image_name"])
    assert image.packed_file is not None


def test_hdri_builds_a_new_world_and_wipes_nobodys(server, monkeypatch):
    """The old code took bpy.data.worlds[0] - the alphabetically first world
    datablock, very often somebody else's - cleared its nodes and made it
    active, destroying hand-built setups with no undo step. Using the scene's
    own world instead would still have wiped that one, so a new world is built
    each time and the existing ones are left exactly as they were."""
    addon, srv = server
    _install_requests(monkeypatch, addon, files=HDRI_FILES)

    someone_elses = addon.bpy.data.worlds.new("Aurora Studio Setup")
    someone_elses.node_tree.nodes.new(type="ShaderNodeBackground")
    scene_world = addon.bpy.data.worlds.new("Scene World")
    scene_world.node_tree.nodes.new(type="ShaderNodeBackground")
    addon.bpy.context.scene.world = scene_world

    srv.download_polyhaven_asset(HDRI_SLUG, "hdris", "1k", "hdr")

    assert len(someone_elses.node_tree.nodes) == 1, "an unrelated world was wiped"
    assert len(scene_world.node_tree.nodes) == 1, "the scene's own world was wiped"
    new_world = addon.bpy.context.scene.world
    assert new_world is not scene_world and new_world is not someone_elses
    assert _node_of_type(new_world.node_tree, "TEX_ENVIRONMENT") is not None
    # No fake user: Blender clears the displaced world up on save if nothing
    # else references it, rather than accumulating one per import.
    assert getattr(new_world, "use_fake_user", False) is False


def test_hdri_creates_a_world_when_the_scene_has_none(server, monkeypatch):
    addon, srv = server
    _install_requests(monkeypatch, addon, files=HDRI_FILES)
    addon.bpy.context.scene.world = None

    result = srv.download_polyhaven_asset(HDRI_SLUG, "hdris", "1k", "hdr")

    assert result.get("success"), result
    assert addon.bpy.context.scene.world is not None


def test_hdri_world_is_fully_wired(server, monkeypatch):
    addon, srv = server
    _install_requests(monkeypatch, addon, files=HDRI_FILES)

    srv.download_polyhaven_asset(HDRI_SLUG, "hdris", "1k", "hdr")
    tree = addon.bpy.context.scene.world.node_tree

    output = _node_of_type(tree, "OUTPUT_WORLD")
    background_link = _link_into(tree, output, "Surface")
    assert background_link.from_node.type == "BACKGROUND"
    colour_link = _link_into(tree, background_link.from_node, "Color")
    assert colour_link.from_node.type == "TEX_ENVIRONMENT"


# --- formats and error messages ----------------------------------------------

def test_formats_other_than_blend_are_rejected_before_downloading(server, monkeypatch):
    """`usd` is listed for every model, so it passed the "is this format
    present?" guard, downloaded in full, and only then hit the unsupported
    branch at the end of the import. glTF and FBX are refused for a different
    reason: both are generated from the .blend and lose material detail that
    ships with the asset."""
    addon, srv = server

    for fmt in ("usd", "gltf", "fbx"):
        calls = _install_requests(monkeypatch, addon, files=MODEL_FILES)
        result = srv.download_polyhaven_asset(MODEL_SLUG, "models", "1k", fmt)

        assert "error" in result, fmt
        assert fmt in result["error"]
        assert "blend" in result["error"], "the error should name what is supported"
        assert _downloaded(calls) == [], f"{fmt} was transferred before being rejected"


def test_models_default_to_blend(server, monkeypatch):
    addon, srv = server
    calls = _install_requests(monkeypatch, addon, files=MODEL_FILES)
    addon.bpy.data.libraries.contents = MODEL_WITH_A_STOWAWAY

    result = srv.download_polyhaven_asset(MODEL_SLUG, "models", "1k")

    assert result.get("success"), result
    assert _downloaded(calls) == [MODEL_FILES["blend"]["1k"]["blend"]["url"]]


def test_model_imports_and_reports_its_objects(server, monkeypatch):
    """The .blend branch read selected_objects, which it never populates, so it
    reported an empty list for every appended model."""
    addon, srv = server
    _install_requests(monkeypatch, addon, files=MODEL_FILES)
    addon.bpy.data.libraries.contents = MODEL_WITH_A_STOWAWAY

    result = srv.download_polyhaven_asset(MODEL_SLUG, "models", "1k", "blend")

    assert result.get("success"), result
    assert set(result["imported_objects"]) == {
        f"{MODEL_SLUG}_pot", f"{MODEL_SLUG}_leaves"}


def test_only_the_assets_own_collection_is_appended(server, monkeypatch):
    """Several published .blends hold a second, unrelated model beside the asset
    - wooden_ladder's file also contains wooden_step_ladder. Appending
    data_from.objects wholesale brought it along."""
    addon, srv = server
    _install_requests(monkeypatch, addon, files=MODEL_FILES)
    addon.bpy.data.libraries.contents = MODEL_WITH_A_STOWAWAY

    result = srv.download_polyhaven_asset(MODEL_SLUG, "models", "1k", "blend")

    assert result.get("success"), result
    assert "someone_elses_model" not in result["imported_objects"]
    assert [c.name for c in addon.bpy.context.scene.collection.children] == [MODEL_SLUG]


def test_only_lod0_is_linked_into_the_scene(server, monkeypatch):
    """A model with levels of detail carries every one of them in the same file.
    Appending them all put three copies of the model on top of each other."""
    addon, srv = server
    _install_requests(monkeypatch, addon, files=MODEL_FILES)
    addon.bpy.data.libraries.contents = MODEL_WITH_LODS

    result = srv.download_polyhaven_asset(MODEL_SLUG, "models", "1k", "blend")

    assert result.get("success"), result
    assert set(result["imported_objects"]) == {
        f"{MODEL_SLUG}_pot_LOD0", f"{MODEL_SLUG}_leaves_LOD0"}
    assert [c.name for c in addon.bpy.context.scene.collection.children] == [
        f"{MODEL_SLUG}_LOD0"]


def test_a_blend_with_no_matching_collection_still_imports(server, monkeypatch):
    """Every published model has a collection named after its slug, but the
    import should not return an empty scene if one ever does not."""
    addon, srv = server
    _install_requests(monkeypatch, addon, files=MODEL_FILES)
    addon.bpy.data.libraries.contents = {
        "collections": {"Collection": ["loose_mesh"]},
        "objects": ["loose_mesh"],
    }

    result = srv.download_polyhaven_asset(MODEL_SLUG, "models", "1k", "blend")

    assert result.get("success"), result
    assert result["imported_objects"] == ["loose_mesh"]


def test_a_blend_newer_than_this_blender_falls_back_to_gltf(server, monkeypatch):
    """Poly Haven's models were each saved by whichever Blender compiled them,
    from 2.93 to 5.0, and Blender cannot open a file newer than itself. glTF is
    a worse record of the material, but it beats no model at all."""
    addon, srv = server
    calls = _install_requests(monkeypatch, addon, files=FUTURE_MODEL_FILES)
    assert addon.bpy.app.version[:2] < (5, 0), "fixture assumes an older Blender"

    imported = []
    monkeypatch.setattr(
        addon.bpy.ops.import_scene, "gltf",
        lambda filepath=None, **_k: (imported.append(filepath),
                                     addon.bpy.data.objects.append(FakeObject("from_gltf"))))

    result = srv.download_polyhaven_asset(MODEL_SLUG, "models", "1k", "blend")

    assert result.get("success"), result
    assert result["imported_objects"] == ["from_gltf"]
    assert "5.0" in result["message"] and "glTF" in result["message"], result["message"]
    assert _downloaded(calls) == [
        FUTURE_MODEL_FILES["blend"]["1k"]["blend"]["url"],
        FUTURE_MODEL_FILES["gltf"]["1k"]["gltf"]["url"],
    ]


def test_a_newer_blend_with_no_gltf_says_so(server, monkeypatch):
    addon, srv = server
    _install_requests(monkeypatch, addon, files=FUTURE_ONLY_BLEND_FILES)

    result = srv.download_polyhaven_asset(MODEL_SLUG, "models", "1k", "blend")

    assert "error" in result
    assert "5.0" in result["error"], result["error"]
    assert "glTF" in result["error"], result["error"]


@pytest.mark.parametrize("header, expected", [
    (b"BLENDER-v293", (2, 93)),       # the oldest published models
    (b"BLENDER-v302", (3, 2)),
    (b"BLENDER-v402", (4, 2)),
    (b"BLENDER17-01v0500", (5, 0)),   # the header grew in Blender 4.5
    (b"BLENDER17-01v0502", (5, 2)),
    (b"not a blend file at all", None),
])
def test_blend_header_versions_are_read(server, tmp_path, header, expected):
    addon, _srv = server
    path = tmp_path / "sample.blend"
    path.write_bytes(_blend_bytes(header))

    assert addon._polyhaven_blend_version(str(path)) == expected


def test_a_compressed_blend_header_is_read(server, tmp_path):
    """Published .blend files are compressed - gzip up to 2.93, zstd after - so
    the version is not sitting in the first bytes of the file."""
    import zlib

    addon, _srv = server
    path = tmp_path / "compressed.blend"
    compressor = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    path.write_bytes(compressor.compress(_blend_bytes(b"BLENDER-v293")) + compressor.flush())

    assert addon._polyhaven_blend_version(str(path)) == (2, 93)


def test_model_includes_cannot_escape_the_download_directory(server, monkeypatch, tmp_path):
    """The API response controls these keys, so a malicious or MITM'd one could
    write outside the download directory - issue #257. Mirrors
    test_hunyuan_import_security.py for the sibling code path.

    The download directory is nested deliberately deep: a "../../.." that got
    through would then still land inside tmp_path, where this test can see it.
    Asserting only that the escaped name is absent from the download directory
    would pass whether the guard works or not, because a successful escape puts
    the file somewhere the assertion never looks."""
    addon, srv = server
    sandbox = _mkdir(tmp_path / "a" / "b" / "c" / "d" / "e")
    monkeypatch.setattr(addon.tempfile, "gettempdir", lambda: str(sandbox))
    calls = _install_requests(monkeypatch, addon, files=HOSTILE_MODEL_FILES)
    addon.bpy.data.libraries.contents = MODEL_WITH_A_STOWAWAY

    result = srv.download_polyhaven_asset(MODEL_SLUG, "models", "1k", "blend")
    assert result.get("success"), result

    # The guard skips before downloading, so only the safe include is fetched.
    includes = [url for url in _downloaded(calls) if url.endswith(".png")]
    assert len(includes) == 1, f"expected 1 safe include, fetched {len(includes)}"

    # The download directory is removed on the way out, so anything still on
    # disk is a file that was written outside it.
    leaked = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert leaked == [], f"files written outside the download directory: {leaked}"
def _mkdir(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_error_names_the_resolutions_that_do_exist(server, monkeypatch):
    """The three errors this replaces were f-strings with nothing interpolated,
    so an agent that guessed wrong could only guess again."""
    addon, srv = server
    _install_requests(monkeypatch, addon, files=HDRI_FILES)

    result = srv.download_polyhaven_asset(HDRI_SLUG, "hdris", "16k", "hdr")

    assert "error" in result
    assert "1k, 4k, 8k" in result["error"]


def test_resolutions_are_listed_in_numeric_order(server, monkeypatch):
    addon, _ = server
    assert addon._polyhaven_sorted_resolutions({"8k", "1k", "16k", "2k"}) == ["1k", "2k", "8k", "16k"]


def test_unknown_asset_type_is_rejected(server, monkeypatch):
    addon, srv = server
    calls = _install_requests(monkeypatch, addon, files=TEXTURE_FILES)

    result = srv.download_polyhaven_asset(TEXTURE_SLUG, "furniture", "1k", None)

    assert "Unsupported asset type" in result["error"]
    assert calls == [], "an unknown asset type must not reach the network"


def test_an_asset_id_cannot_escape_the_cache_directory(server, monkeypatch):
    """asset_id arrives from the model and lands in a filesystem path. Upstream
    used mkdtemp, whose name no caller can influence; a stable cache directory
    has to check the slug instead."""
    addon, srv = server
    calls = _install_requests(monkeypatch, addon, files=TEXTURE_FILES)

    for hostile in ("../../pwned", "/etc/cron.d/x", "a/../../b", "rock wall"):
        result = srv.download_polyhaven_asset(hostile, "textures", "1k", "jpg")
        assert "Invalid asset id" in result.get("error", ""), hostile

    assert calls == [], "a rejected asset id must not reach the network"
    # Real slugs still pass.
    assert addon._polyhaven_valid_slug("rock_wall_10")
    assert addon._polyhaven_valid_slug("kloofendal_43d_clear_puresky")


# --- search ------------------------------------------------------------------

def _asset(name, asset_type, downloads, **extra):
    """An /assets record, in the shape the live API returns one."""
    record = {
        "name": name,
        "type": asset_type,
        "categories": [],
        "download_count": downloads,
        "authors": {"Rob Tuytel": "All"},
    }
    record.update(extra)
    return record


def test_search_ranks_before_truncating(server, monkeypatch):
    """The list arrives sorted by slug, and models are the only assets with
    capitalised slugs - so the first 20 of an unfiltered list were 20 models,
    and the library's most downloaded assets were unreachable by any call."""
    addon, srv = server
    assets = {f"AModel_{i:02d}": _asset(f"Model {i}", 2, i) for i in range(20)}
    assets["kloofendal_puresky"] = _asset("Kloofendal", 0, 865144)
    assets["rock_wall_10"] = _asset("Rock Wall 10", 1, 50826)
    _install_requests(monkeypatch, addon, assets=assets)

    result = srv.search_polyhaven_assets(asset_type="all")

    returned = [asset["id"] for asset in result["assets"]]
    assert result["total_count"] == 22
    assert result["returned_count"] == 20
    assert "kloofendal_puresky" in returned, "the most downloaded asset was truncated away"
    assert "rock_wall_10" in returned
    assert returned[0] == "kloofendal_puresky"


def test_search_rejects_an_unknown_type_without_a_request(server, monkeypatch):
    addon, srv = server
    calls = _install_requests(monkeypatch, addon, assets={})

    result = srv.search_polyhaven_assets(asset_type="furniture")

    assert "error" in result
    assert calls == []


# --- set_texture -------------------------------------------------------------

def test_set_texture_wires_each_input_exactly_once(server, monkeypatch):
    """set_texture used to build its tree in two passes over the same maps.
    Blender allows one link per input, so the second pass replaced every link
    the first had made, leaving the first pass's Normal Map and Displacement
    nodes orphaned in the tree."""
    addon, srv = server
    _install_requests(monkeypatch, addon, files=TEXTURE_FILES)
    srv.download_polyhaven_asset(TEXTURE_SLUG, "textures", "1k", "jpg")

    obj = FakeObject("Cube")
    addon.bpy.data.objects.append(obj)

    result = srv.set_texture("Cube", TEXTURE_SLUG)
    assert result.get("success"), result

    tree = addon.bpy.data.materials.get(result["material"]).node_tree
    principled = _node_of_type(tree, "BSDF_PRINCIPLED")

    # _link_into asserts at most one link per input.
    assert _link_into(tree, principled, "Normal") is not None
    assert _link_into(tree, principled, "Base Color") is not None

    normal_maps = [n for n in tree.nodes if n.type == "NORMAL_MAP"]
    displacements = [n for n in tree.nodes if n.type == "DISPLACEMENT"]
    assert len(normal_maps) == 1, "an orphaned Normal Map node was left in the tree"
    assert len(displacements) == 1, "an orphaned Displacement node was left in the tree"

    assert obj.data.materials == [tree_material(addon, result)]


def tree_material(addon, result):
    return addon.bpy.data.materials.get(result["material"])


def test_set_texture_reports_the_node_tree_it_built(server, monkeypatch):
    """The MCP layer renders this back to the caller, so an empty summary would
    read as "no texture nodes found" on a material that has four."""
    addon, srv = server
    _install_requests(monkeypatch, addon, files=TEXTURE_FILES)
    srv.download_polyhaven_asset(TEXTURE_SLUG, "textures", "1k", "jpg")
    addon.bpy.data.objects.append(FakeObject("Cube"))

    info = srv.set_texture("Cube", TEXTURE_SLUG)["material_info"]

    assert info["has_nodes"] is True
    assert info["node_count"] > 0
    assert len(info["texture_nodes"]) == 4
    assert all(node["connections"] for node in info["texture_nodes"])


def test_set_texture_reports_the_slots_it_replaced(server, monkeypatch):
    """It clears every material slot on the object, which cannot be undone -
    the agent had no way to know that from the old message."""
    addon, srv = server
    _install_requests(monkeypatch, addon, files=TEXTURE_FILES)
    srv.download_polyhaven_asset(TEXTURE_SLUG, "textures", "1k", "jpg")

    obj = FakeObject("Cube")
    obj.data.materials.extend([object(), object()])
    addon.bpy.data.objects.append(obj)

    result = srv.set_texture("Cube", TEXTURE_SLUG)

    assert "replaced 2 existing material slots" in result["message"]


def test_set_texture_needs_the_texture_downloaded_first(server, monkeypatch):
    addon, srv = server
    _install_requests(monkeypatch, addon, files=TEXTURE_FILES)
    addon.bpy.data.objects.append(FakeObject("Cube"))

    result = srv.set_texture("Cube", TEXTURE_SLUG)

    assert "error" in result
    assert "download_polyhaven_asset" in result["error"]




# --- the asset list is 2.44MB, and used to be re-fetched every call ----------

SEARCH_ASSETS = {
    # The API returns assets sorted by slug, and models are the only ones with a
    # capitalised slug, so an unranked first-20 slice is 20 models.
    "ArmChair_01": {"name": "Arm Chair 01", "type": 2, "download_count": 900,
                    "categories": ["furniture"]},
    "Barrel_01": {"name": "Barrel 01", "type": 2, "download_count": 800,
                  "categories": ["furniture"]},
    "moonless_golf": {"name": "Moonless Golf", "type": 0, "download_count": 779145,
                      "categories": ["night"]},
    "rock_wall_10": {"name": "Rock Wall 10", "type": 1, "download_count": 5000,
                     "categories": ["rock"]},
}


def test_the_asset_list_is_not_refetched_within_the_ttl(server, monkeypatch):
    addon, srv = server
    calls = _install_requests(monkeypatch, addon, assets=SEARCH_ASSETS)

    first = srv.search_polyhaven_assets(asset_type="all")
    second = srv.search_polyhaven_assets(asset_type="all")

    assert first["assets"] == second["assets"]
    assert len([c for c in calls if c["url"].endswith("/assets")]) == 1


def test_a_lapsed_cache_revalidates_instead_of_refetching(server, monkeypatch):
    """Once the TTL is up, If-None-Match turns the refetch into a 304 rather
    than another 2.44MB."""
    addon, srv = server
    calls = _install_requests(monkeypatch, addon, assets=SEARCH_ASSETS)

    first = srv.search_polyhaven_assets(asset_type="all")
    for entry in addon._polyhaven_cache.values():
        entry["fetched"] -= addon.POLYHAVEN_CACHE_TTL + 1
    second = srv.search_polyhaven_assets(asset_type="all")

    assert first["assets"] == second["assets"]
    asset_calls = [c for c in calls if c["url"].endswith("/assets")]
    assert len(asset_calls) == 2
    assert asset_calls[1]["headers"].get("If-None-Match") == ASSETS_ETAG


def test_each_asset_type_is_cached_separately(server, monkeypatch):
    """Asking for one type must not be served the whole library, or vice versa."""
    addon, srv = server
    calls = _install_requests(monkeypatch, addon, assets=SEARCH_ASSETS)

    srv.search_polyhaven_assets(asset_type="hdris")
    srv.search_polyhaven_assets(asset_type="textures")
    srv.search_polyhaven_assets(asset_type="hdris")

    asset_calls = [c for c in calls if c["url"].endswith("/assets")]
    assert [c["params"].get("type") for c in asset_calls] == ["hdris", "textures"]


def test_the_cache_does_not_grow_without_limit(server, monkeypatch):
    addon, srv = server
    _install_requests(monkeypatch, addon, assets=SEARCH_ASSETS)

    for i in range(addon.POLYHAVEN_CACHE_MAX_ENTRIES + 5):
        addon._polyhaven_api_get("assets", params={"categories": f"c{i}"}, cache=True)

    assert len(addon._polyhaven_cache) <= addon.POLYHAVEN_CACHE_MAX_ENTRIES


# --- search: the tool could not search --------------------------------------

def _install_search(monkeypatch, addon, assets, results=None, status=None, retry_after=None):
    """Serve /search alongside the other endpoints."""
    calls = _install_requests(monkeypatch, addon, assets=assets)
    inner = addon.requests.get

    def fake_get(url, headers=None, params=None, timeout=None, stream=False):
        if url.endswith("/search"):
            calls.append({"url": url, "params": dict(params or {}), "stream": False,
                          "timeout": timeout, "headers": dict(headers or {})})
            if status:
                return FakeResponse(status_code=status,
                                    headers={"Retry-After": retry_after} if retry_after else {})
            return FakeResponse(payload={
                "query": (params or {}).get("q"),
                "total": len(results),
                "considered": len(assets),
                "hybrid": True,
                "results": [{"slug": slug, "score": score} for slug, score in results],
            })
        return inner(url, headers=headers, params=params, timeout=timeout, stream=stream)

    monkeypatch.setattr(addon.requests, "get", fake_get, raising=False)
    return calls


SEARCHABLE = {
    "rusty_metal": _asset("Rusty Metal", 1, 300000, tags=["rust", "metal"],
                          category="Metal/Sheet & Corrugated", dimensions=[1000, 1000]),
    "rusty_metal_03": _asset("Rusty Metal 03", 1, 200000),
    "metal_plate_02": _asset("Metal Plate 02", 1, 100000),
    "moonless_golf": _asset("Moonless Golf", 0, 779145),
}


def test_a_query_is_answered_by_the_search_endpoint(server, monkeypatch):
    """There was no query parameter at all: the tool fetched the whole asset
    list and returned a slice of it, so "find me a rusty metal texture" could
    only ever be answered by whatever happened to be popular."""
    addon, srv = server
    calls = _install_search(monkeypatch, addon, SEARCHABLE, results=[
        ("rusty_metal", 0.69), ("rusty_metal_03", 0.67), ("metal_plate_02", 0.61)])

    result = srv.search_polyhaven_assets(query="Rusty Metal ", asset_type="textures")

    assert [asset["id"] for asset in result["assets"]] == [
        "rusty_metal", "rusty_metal_03", "metal_plate_02"]
    search = next(c for c in calls if c["url"].endswith("/search"))
    assert search["params"]["q"] == "rusty metal", "queries are trimmed and lower-cased"
    assert search["params"]["t"] == "textures"


def test_search_order_is_the_ranking_and_is_not_re_sorted(server, monkeypatch):
    """Two rankings are fused by position, so the array order is the answer.
    `score` is the vector lane alone and does not explain the order once a
    keyword match has lifted something."""
    addon, srv = server
    _install_search(monkeypatch, addon, SEARCHABLE, results=[
        ("rusty_metal", 0.69), ("rusty_metal_03", 0.67), ("metal_plate_02", 0.71)])

    result = srv.search_polyhaven_assets(query="rusty metal")

    assert [asset["id"] for asset in result["assets"]] == [
        "rusty_metal", "rusty_metal_03", "metal_plate_02"]


def test_search_results_carry_the_metadata_the_api_already_returned(server, monkeypatch):
    """Every /assets record holds the author, tags, category and real-world size,
    and all of it was being downloaded and then thrown away."""
    addon, srv = server
    _install_search(monkeypatch, addon, SEARCHABLE, results=[("rusty_metal", 0.69)])

    asset = srv.search_polyhaven_assets(query="rusty metal")["assets"][0]

    assert asset["url"] == "https://polyhaven.com/a/rusty_metal"
    assert asset["authors"] == ["Rob Tuytel"]
    assert asset["tags"] == ["rust", "metal"]
    assert asset["category"] == "Metal/Sheet & Corrugated"
    assert asset["dimensions_mm"] == [1000, 1000]
    assert asset["type"] == "textures"


def test_a_rate_limited_search_says_how_long_to_wait(server, monkeypatch):
    addon, srv = server
    _install_search(monkeypatch, addon, SEARCHABLE, status=429, retry_after="30")

    result = srv.search_polyhaven_assets(query="rusty metal")

    assert "error" in result
    assert "30s" in result["error"], result["error"]


def test_an_unavailable_search_index_falls_back_to_keywords(server, monkeypatch):
    """The API documents a 503 as "the query could not be embedded, fall back to
    your own keyword matching"."""
    addon, srv = server
    _install_search(monkeypatch, addon, SEARCHABLE, status=503)

    result = srv.search_polyhaven_assets(query="rusty metal")

    assert [asset["id"] for asset in result["assets"]][:2] == ["rusty_metal", "rusty_metal_03"]
    assert "moonless_golf" not in [asset["id"] for asset in result["assets"]]
    assert "keyword" in (result.get("note") or "").lower()


def test_a_search_hit_outside_the_category_filter_is_dropped(server, monkeypatch):
    """/search does not know about the category filter, so its ranking has to be
    intersected with the filtered list rather than trusted wholesale."""
    addon, srv = server
    filtered = {"rusty_metal": SEARCHABLE["rusty_metal"]}
    _install_search(monkeypatch, addon, filtered, results=[
        ("rusty_metal", 0.69), ("metal_plate_02", 0.61)])

    result = srv.search_polyhaven_assets(query="rusty metal", category="Metal")

    assert [asset["id"] for asset in result["assets"]] == ["rusty_metal"]


def test_the_result_limit_is_capped(server, monkeypatch):
    addon, srv = server
    assets = {f"asset_{i:03d}": _asset(f"Asset {i}", 1, i) for i in range(120)}
    _install_requests(monkeypatch, addon, assets=assets)

    result = srv.search_polyhaven_assets(limit=1000)

    assert result["returned_count"] == addon.POLYHAVEN_SEARCH_MAX_LIMIT


# --- taxonomy: the flat category list is deprecated --------------------------

TAXONOMY = {
    "type": "textures",
    "categories": [
        {"name": "Metal", "path": "Metal", "slugPath": "metal", "id": "uuid-1",
         "description": "Metallic surfaces.", "children": [
             {"name": "Sheet", "path": "Metal/Sheet", "slugPath": "metal/sheet",
              "id": "uuid-2", "description": "", "children": [
                  {"name": "Flat Sheet", "path": "Metal/Sheet/Flat Sheet",
                   "slugPath": "metal/sheet/flat-sheet", "id": "uuid-3", "children": []}]}]},
        {"name": "Stone", "path": "Stone", "slugPath": "stone", "id": "uuid-4", "children": []},
    ],
    "attributes": {
        "condition": {"type": "string[]", "enum": ["clean", "rusted"],
                      "description": "How worn the surface is."},
        "aerial": {"type": "boolean", "description": "Captured by drone."},
    },
}


def _install_taxonomy(monkeypatch, addon):
    calls = _install_requests(monkeypatch, addon)
    inner = addon.requests.get

    def fake_get(url, headers=None, params=None, timeout=None, stream=False):
        if "/taxonomy/" in url:
            calls.append({"url": url, "params": dict(params or {}), "stream": False,
                          "timeout": timeout, "headers": dict(headers or {})})
            return FakeResponse(payload=dict(TAXONOMY, type=url.rsplit("/", 1)[-1]))
        return inner(url, headers=headers, params=params, timeout=timeout, stream=stream)

    monkeypatch.setattr(addon.requests, "get", fake_get, raising=False)
    return calls


def test_categories_come_from_the_taxonomy_endpoint(server, monkeypatch):
    """The flat /categories list is deprecated. /taxonomy carries the single-path
    tree an asset's `category` field actually uses, plus the attribute schema."""
    addon, srv = server
    calls = _install_taxonomy(monkeypatch, addon)

    result = srv.get_polyhaven_categories("textures")

    assert [c["url"] for c in calls] == ["https://api.polyhaven.com/taxonomy/textures"]
    taxonomy = result["taxonomy"][0]
    assert taxonomy["categories"] == ["Metal", "Metal/Sheet", "Metal/Sheet/Flat Sheet", "Stone"]
    assert taxonomy["attributes"]["condition"]["enum"] == ["clean", "rusted"]
    assert taxonomy["attributes"]["aerial"]["type"] == "boolean"


def test_the_taxonomy_is_trimmed_to_what_a_filter_needs(server, monkeypatch):
    """The raw response is 60-80KB per type, most of it UUIDs and URL slugs that
    nothing here uses."""
    addon, srv = server
    _install_taxonomy(monkeypatch, addon)

    taxonomy = srv.get_polyhaven_categories("textures")["taxonomy"][0]

    assert all(isinstance(path, str) for path in taxonomy["categories"])
    assert set(taxonomy["attributes"]["condition"]) <= {"type", "enum", "description"}


def test_asking_for_every_type_returns_only_the_top_levels(server, monkeypatch):
    """Three full trees at once is 30KB of paths. Category filtering is
    inclusive, so the top two levels still select everything beneath them."""
    addon, srv = server
    _install_taxonomy(monkeypatch, addon)

    result = srv.get_polyhaven_categories("all")

    assert result["truncated"] is True
    assert [t["type"] for t in result["taxonomy"]] == ["hdris", "textures", "models"]
    for taxonomy in result["taxonomy"]:
        assert "Metal/Sheet/Flat Sheet" not in taxonomy["categories"]
        assert "Metal/Sheet" in taxonomy["categories"]


def test_an_unknown_type_is_rejected_without_a_request(server, monkeypatch):
    addon, srv = server
    calls = _install_taxonomy(monkeypatch, addon)

    result = srv.get_polyhaven_categories("hdri")

    assert "error" in result
    assert calls == []


def test_the_taxonomy_is_cached(server, monkeypatch):
    addon, srv = server
    calls = _install_taxonomy(monkeypatch, addon)

    srv.get_polyhaven_categories("textures")
    srv.get_polyhaven_categories("textures")

    assert len([c for c in calls if "/taxonomy/" in c["url"]]) == 1


# --- previews: looking before downloading ------------------------------------

THUMB = ("https://cdn.polyhaven.com/asset_img/thumbs/rusty_metal.png"
         "?width=256&height=256&v=d9ab12e4")

PREVIEWABLE = {"rusty_metal": _asset("Rusty Metal", 1, 300000, thumbnail_url=THUMB)}


def _install_preview(monkeypatch, addon, assets=None, info=None, body=b"\x89PNG-bytes"):
    calls = _install_requests(monkeypatch, addon, assets=assets, info=info)
    inner = addon.requests.get

    def fake_get(url, headers=None, params=None, timeout=None, stream=False):
        if "cdn.polyhaven.com" in url:
            calls.append({"url": url, "params": dict(params or {}), "stream": False,
                          "timeout": timeout, "headers": dict(headers or {})})
            return FakeResponse(content=body, headers={"Content-Type": "image/png"})
        return inner(url, headers=headers, params=params, timeout=timeout, stream=stream)

    monkeypatch.setattr(addon.requests, "get", fake_get, raising=False)
    return calls


def test_a_preview_keeps_the_thumbnails_cache_busting_version(server, monkeypatch):
    """thumbnail_url carries a `v` holding a hash of the asset's images. Bunny
    serves images with a year-long max-age, so a URL rebuilt without it can be
    answered from cache with a thumbnail for renders that have been replaced
    since."""
    addon, srv = server
    calls = _install_preview(monkeypatch, addon, info=PREVIEWABLE["rusty_metal"])

    result = srv.get_polyhaven_asset_preview("rusty_metal")

    assert result["success"]
    requested = next(c["url"] for c in calls if "cdn.polyhaven.com" in c["url"])
    assert "v=d9ab12e4" in requested
    assert "width=512" in requested and "height=512" in requested


def test_a_preview_reuses_the_cached_asset_list(server, monkeypatch):
    """/info is the same record plus a few internal fields, so it is only worth
    a request when the list has not already been fetched."""
    addon, srv = server
    calls = _install_preview(monkeypatch, addon, assets=PREVIEWABLE)

    srv.search_polyhaven_assets(asset_type="textures")
    result = srv.get_polyhaven_asset_preview("rusty_metal")

    assert result["success"]
    assert [c for c in calls if "/info/" in c["url"]] == []


def test_a_preview_reports_the_asset_it_is_showing(server, monkeypatch):
    addon, srv = server
    _install_preview(monkeypatch, addon, info=PREVIEWABLE["rusty_metal"])

    result = srv.get_polyhaven_asset_preview("rusty_metal")

    assert result["name"] == "Rusty Metal"
    assert result["authors"] == ["Rob Tuytel"]
    assert result["url"] == "https://polyhaven.com/a/rusty_metal"
    assert result["format"] == "png"


def test_a_preview_rejects_a_bad_slug_without_a_request(server, monkeypatch):
    addon, srv = server
    calls = _install_preview(monkeypatch, addon, info=PREVIEWABLE["rusty_metal"])

    result = srv.get_polyhaven_asset_preview("../../etc/passwd")

    assert "error" in result
    assert calls == []


def test_an_asset_with_no_thumbnail_says_so(server, monkeypatch):
    addon, srv = server
    _install_preview(monkeypatch, addon, info=_asset("No Thumb", 1, 1))

    result = srv.get_polyhaven_asset_preview("rock_wall_10")

    assert "error" in result
    assert "thumbnail" in result["error"]


# --- provenance: where the asset came from, kept in the file -----------------

def _props(block):
    return block.custom_properties


def test_a_texture_records_where_it_came_from(server, monkeypatch):
    """Mirrors the polypizza_* properties the sibling integration writes. Poly
    Haven's assets are CC0 and need no attribution, but custom properties are
    saved into the .blend, so whoever opens it later can still find the asset
    and the artist."""
    addon, srv = server
    _install_requests(monkeypatch, addon, files=TEXTURE_FILES)

    result = srv.download_polyhaven_asset(TEXTURE_SLUG, "textures", "1k", "jpg")
    material = _material(addon, result)

    assert _props(material)["polyhaven_id"] == TEXTURE_SLUG
    assert _props(material)["polyhaven_url"] == f"https://polyhaven.com/a/{TEXTURE_SLUG}"
    assert _props(material)["polyhaven_licence"] == "CC0"
    assert _props(material)["polyhaven_resolution"] == "1k"
    assert _props(material)["polyhaven_authors"] == "Rob Tuytel"
    assert result["url"] == f"https://polyhaven.com/a/{TEXTURE_SLUG}"
    for image in addon.bpy.data.images:
        assert _props(image)["polyhaven_url"]


def test_an_hdri_records_where_it_came_from(server, monkeypatch):
    addon, srv = server
    _install_requests(monkeypatch, addon, files=HDRI_FILES)

    result = srv.download_polyhaven_asset(HDRI_SLUG, "hdris", "1k", "hdr")

    world = addon.bpy.context.scene.world
    assert _props(world)["polyhaven_id"] == HDRI_SLUG
    assert _props(world)["polyhaven_licence"] == "CC0"
    assert _props(addon.bpy.data.images[0])["polyhaven_resolution"] == "1k"
    assert result["authors"] == ["Rob Tuytel"]


def test_a_model_records_where_it_came_from(server, monkeypatch):
    addon, srv = server
    _install_requests(monkeypatch, addon, files=MODEL_FILES)
    addon.bpy.data.libraries.contents = MODEL_WITH_A_STOWAWAY

    result = srv.download_polyhaven_asset(MODEL_SLUG, "models", "1k", "blend")

    imported = [o for o in addon.bpy.data.objects if o.name in result["imported_objects"]]
    assert imported
    for obj in imported:
        assert _props(obj)["polyhaven_id"] == MODEL_SLUG
        assert _props(obj)["polyhaven_licence"] == "CC0"
    linked = addon.bpy.context.scene.collection.children
    assert _props(linked[0])["polyhaven_url"].endswith(MODEL_SLUG)


def test_a_model_does_not_tag_collections_it_did_not_import(server, monkeypatch):
    addon, srv = server
    _install_requests(monkeypatch, addon, files=MODEL_FILES)
    addon.bpy.data.libraries.contents = MODEL_WITH_A_STOWAWAY
    mine = addon.bpy.data.collections.new("my_own_collection")
    addon.bpy.context.scene.collection.children.link(mine)

    srv.download_polyhaven_asset(MODEL_SLUG, "models", "1k", "blend")

    assert _props(mine) == {}


def test_provenance_survives_a_failed_metadata_lookup(server, monkeypatch):
    """The author lookup is a nicety. It must never be the reason an import
    fails."""
    addon, srv = server
    calls = _install_requests(monkeypatch, addon, files=TEXTURE_FILES)
    inner = addon.requests.get

    def fake_get(url, headers=None, params=None, timeout=None, stream=False):
        if "/info/" in url or url.endswith("/assets"):
            raise RuntimeError("metadata is down")
        return inner(url, headers=headers, params=params, timeout=timeout, stream=stream)

    monkeypatch.setattr(addon.requests, "get", fake_get, raising=False)

    result = srv.download_polyhaven_asset(TEXTURE_SLUG, "textures", "1k", "jpg")

    assert result.get("success"), result
    assert _props(_material(addon, result))["polyhaven_id"] == TEXTURE_SLUG
    assert "polyhaven_authors" not in _props(_material(addon, result))


def test_poly_haven_requests_identify_the_integration(server, monkeypatch):
    """Extends the User-Agent added in #147. Kept off the shared REQ_HEADERS
    because Poly Pizza sends that one too."""
    addon, srv = server
    calls = _install_requests(monkeypatch, addon, files=TEXTURE_FILES)

    srv.download_polyhaven_asset(TEXTURE_SLUG, "textures", "1k", "jpg")

    agents = {c["headers"].get("User-Agent") for c in calls}
    assert agents, "no requests were made"
    for agent in agents:
        assert agent.startswith("blender-mcp/"), agent
        assert "github.com/ahujasid/blender-mcp" in agent
    assert addon.REQ_HEADERS["User-Agent"] == "blender-mcp", "Poly Pizza's header is unchanged"


def test_the_tool_response_says_where_the_asset_came_from():
    """The sidebar checkbox names Poly Haven, but in an agentic session nobody
    opens the sidebar - the chat is the only place the person receiving the
    asset can see whose it is."""
    import asyncio

    from blender_mcp import server

    class FakeBlender:
        def send_command(self, command, params=None):
            if command == "get_polyhaven_status":
                return {"enabled": True}
            return {
                "success": True,
                "message": "Texture rock_wall_10 imported as material",
                "material": "rock_wall_10",
                "maps": ["Diffuse", "Rough"],
                "authors": ["Rob Tuytel"],
                "url": "https://polyhaven.com/a/rock_wall_10",
            }

    original = server.get_blender_connection
    server.get_blender_connection = lambda: FakeBlender()
    try:
        out = asyncio.run(server.download_polyhaven_asset(
            None, asset_id="rock_wall_10", asset_type="textures", user_prompt=""))
    finally:
        server.get_blender_connection = original

    assert "Poly Haven" in out
    assert "https://polyhaven.com/a/rock_wall_10" in out
    assert "Rob Tuytel" in out
    assert "CC0" in out


# --- what the adversarial review caught --------------------------------------

def test_a_category_filter_uses_the_taxonomy_parameter_not_the_legacy_one(server, monkeypatch):
    """`categories` and `category` are different filters over disjoint
    vocabularies. `categories` is the legacy flat tag list ("outdoor", "floor");
    `category` takes the single-path taxonomy get_polyhaven_categories now hands
    out ("Metal/Sheet & Corrugated"). Measured live: ?categories=Metal returns 0
    assets and ?category=Metal returns 26 - and the legacy filter answers an
    unknown value with 200 and an empty object, so every filtered search came
    back silently empty rather than erroring."""
    addon, srv = server
    calls = _install_requests(monkeypatch, addon, assets=SEARCHABLE)

    srv.search_polyhaven_assets(category="Metal/Sheet & Corrugated")

    params = next(c["params"] for c in calls if c["url"].endswith("/assets"))
    assert params.get("category") == "Metal/Sheet & Corrugated"
    assert "categories" not in params, "the legacy flat-tag filter matches no taxonomy path"


def test_attribute_filters_reach_the_api(server, monkeypatch):
    """The taxonomy response advertises attributes as filters, so something has
    to be able to send one."""
    addon, srv = server
    calls = _install_requests(monkeypatch, addon, assets=SEARCHABLE)

    srv.search_polyhaven_assets(attributes={"weather": "clear",
                                            "material": ["wood", "metal"],
                                            "rigged": True,
                                            "ignored": None})

    params = next(c["params"] for c in calls if c["url"].endswith("/assets"))
    assert params["weather"] == "clear"
    assert params["material"] == "wood,metal", "a list is OR'd with commas by the API"
    assert params["rigged"] == "true"
    assert "ignored" not in params


def test_an_unrecognised_filter_is_reported_rather_than_shown_as_empty(server, monkeypatch):
    addon, srv = server
    _install_requests(monkeypatch, addon, assets=SEARCHABLE)
    inner = addon.requests.get
    monkeypatch.setattr(addon.requests, "get", lambda url, **kw: (
        FakeResponse(status_code=400) if url.endswith("/assets") else inner(url, **kw)),
        raising=False)

    result = srv.search_polyhaven_assets(category="Not A Real Category")

    assert "error" in result
    assert "get_polyhaven_categories" in result["error"]


def test_search_asks_for_the_whole_ranking_not_the_first_page(server, monkeypatch):
    """/search returns the full ranked list by design, because callers are meant
    to intersect it with what they already hold. Asking for `limit` slugs and
    then filtering those can only shrink the page - the matches are the ones
    further down the ranking."""
    addon, srv = server
    matching = {"rusty_metal": SEARCHABLE["rusty_metal"]}
    calls = _install_search(monkeypatch, addon, matching, results=[
        ("metal_plate_02", 0.9), ("moonless_golf", 0.8), ("rusty_metal", 0.1)])

    result = srv.search_polyhaven_assets(query="rusty metal", limit=2)

    search = next(c for c in calls if c["url"].endswith("/search"))
    assert "limit" not in search["params"], "the server must not cut the list before we filter it"
    assert [a["id"] for a in result["assets"]] == ["rusty_metal"]


def test_total_count_describes_the_page_it_heads(server, monkeypatch):
    """total_count used to be /search's pre-filter count in one branch and the
    post-filter count in another, so the same field meant different things."""
    addon, srv = server
    matching = {"rusty_metal": SEARCHABLE["rusty_metal"],
                "rusty_metal_03": SEARCHABLE["rusty_metal_03"]}
    _install_search(monkeypatch, addon, matching, results=[
        ("rusty_metal", 0.9), ("metal_plate_02", 0.8), ("rusty_metal_03", 0.7),
        ("moonless_golf", 0.6)])

    result = srv.search_polyhaven_assets(query="rusty metal", limit=1)

    assert result["total_count"] == 2, "only the assets that survived every filter"
    assert result["returned_count"] == 1


def test_the_asset_list_outlives_one_shot_search_payloads(server, monkeypatch):
    """Eviction by fetch time is FIFO, not LRU: a hit never refreshed it, so the
    asset list - fetched first and then only ever read - was always the oldest
    key and the first thing discarded, displaced by search payloads a fraction
    of its size. Its ETag went with it, so the refetch could not revalidate."""
    addon, srv = server
    calls = _install_requests(monkeypatch, addon, assets=SEARCHABLE)

    srv.search_polyhaven_assets(asset_type="all")
    for i in range(addon.POLYHAVEN_CACHE_MAX_ENTRIES * 2):
        addon._polyhaven_api_get("search", params={"q": f"one shot {i}"}, cache=True)
        srv.search_polyhaven_assets(asset_type="all")

    assert len([c for c in calls if c["url"].endswith("/assets")]) == 1


def test_a_failed_author_lookup_does_not_leave_the_previous_artist_behind(server, monkeypatch):
    """The lookup is best-effort and comes back empty on any API failure. Every
    other property is overwritten regardless, so a stale name would credit one
    artist for another's asset."""
    addon, srv = server
    _install_requests(monkeypatch, addon, files=TEXTURE_FILES)

    block = addon.bpy.data.materials.new("reused")
    addon._polyhaven_tag([block], "first_asset", "1k", ["Rob Tuytel"])
    assert block.custom_properties["polyhaven_authors"] == "Rob Tuytel"

    addon._polyhaven_tag([block], "second_asset", "1k", [])

    assert block.custom_properties["polyhaven_id"] == "second_asset"
    assert "polyhaven_authors" not in block.custom_properties


def test_set_texture_credits_the_artist(server, monkeypatch):
    """It looked the authors up and stamped them on the material, then omitted
    them from the response, so the chat credit always degraded to no name."""
    addon, srv = server
    _install_requests(monkeypatch, addon, files=TEXTURE_FILES)
    srv.download_polyhaven_asset(TEXTURE_SLUG, "textures", "1k", "jpg")
    obj = FakeObject("Cube")
    addon.bpy.data.objects.append(obj)

    result = srv.set_texture("Cube", TEXTURE_SLUG)

    assert result.get("success"), result
    assert result["authors"] == ["Rob Tuytel"]


def test_the_metallic_map_is_connected(server, monkeypatch):
    """No fixture contained a Metal key, so `elif role == "metallic"` was never
    executed by any test - and a wrong socket name there aborts the whole
    material build rather than degrading it."""
    addon, srv = server
    _install_requests(monkeypatch, addon, files=METAL_FILES)

    result = srv.download_polyhaven_asset(METAL_SLUG, "textures", "1k", "jpg")
    material = _material(addon, result)

    link = _link_into(material.node_tree, _node_of_type(material.node_tree, "BSDF_PRINCIPLED"),
                      "Metallic")
    assert link is not None, "the Metal map was downloaded but never connected"
    assert link.from_node.image.get("polyhaven_map") == "Metal"


def test_an_append_that_lands_nothing_is_an_error(server, monkeypatch):
    """Reporting success with an empty object list leaves the model believing a
    model is in the scene when nothing is."""
    addon, srv = server
    _install_requests(monkeypatch, addon, files=MODEL_FILES)
    addon.bpy.data.libraries.contents = {"collections": {}, "objects": []}

    result = srv.download_polyhaven_asset(MODEL_SLUG, "models", "1k", "blend")

    assert "error" in result
    assert "nothing arrived" in result["error"]


def test_a_zstd_compressed_blend_header_is_read(server, tmp_path):
    """Blender has written zstd since 3.0, so every published model uses it -
    only the gzip path had coverage."""
    zstandard = pytest.importorskip("zstandard")

    addon, _srv = server
    path = tmp_path / "zstd.blend"
    path.write_bytes(zstandard.ZstdCompressor().compress(_blend_bytes(BLEND_HEADER_500)))

    assert addon._polyhaven_blend_version(str(path)) == (5, 0)


def test_a_models_three_axis_size_is_reported_in_full():
    """dimensions is [W, H] on a texture and [X, Y, Z] on a model. Slicing [:2]
    dropped a model's actual height and printed its depth as one - ArmChair_01
    is [848, 766, 1065] mm and rendered as "0.85m x 0.77m"."""
    import asyncio

    from blender_mcp import server

    class FakeBlender:
        def send_command(self, command, params=None):
            if command == "get_polyhaven_status":
                return {"enabled": True}
            return {"assets": [
                {"id": "ArmChair_01", "name": "Arm Chair 01", "type": "models",
                 "url": "https://polyhaven.com/a/ArmChair_01", "authors": ["Kirill Sannikov"],
                 "downloads": 1, "dimensions_mm": [848.43, 765.76, 1065.09]},
                {"id": "rusty_metal", "name": "Rusty Metal", "type": "textures",
                 "url": "https://polyhaven.com/a/rusty_metal", "authors": [],
                 "downloads": 1, "dimensions_mm": [1000, 1000]},
            ], "total_count": 2, "returned_count": 2, "query": None, "note": None}

    original = server.get_blender_connection
    server.get_blender_connection = lambda: FakeBlender()
    try:
        out = asyncio.run(server.search_polyhaven_assets(None, query="chair", user_prompt=""))
    finally:
        server.get_blender_connection = original

    assert "0.84843m x 0.76576m x 1.06509m (W x D x H)" in out
    assert "1m x 1m" in out, "a texture keeps its two-axis form"


# --- what the texture is, in metres -------------------------------------------

def test_the_mapping_node_is_left_in_blenders_default_point_mode(server, monkeypatch):
    """POINT scales the coordinate; TEXTURE inverse-maps it. They are exact
    opposites, so on a TEXTURE node the obvious arithmetic - Scale = surface
    size / texture size - tiles by its reciprocal, and a 2m texture asked to
    repeat twice repeated half a time. Poly Haven authors the Mapping node
    inside its own published .blend files at POINT and its real-world-scale
    operator solves for a Scale that grows with the surface, so this is the
    convention the assets were made for."""
    addon, srv = server
    _install_requests(monkeypatch, addon, files=TEXTURE_FILES)

    result = srv.download_polyhaven_asset(TEXTURE_SLUG, "textures", "1k", "jpg")
    material = _material(addon, result)

    mapping = _node_of_type(material.node_tree, "MAPPING")
    assert mapping is not None
    assert mapping.vector_type == "POINT"
