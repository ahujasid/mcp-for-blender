---
title: Materials and texturing
summary: Node-based materials that survive version and language changes, applying Poly Haven textures, real-world tiling, and procedural looks.
---

# Materials and texturing

## Creating materials

```python
mat = bpy.data.materials.new("Brushed Steel")
mat.use_nodes = True   # no-op in 5.0+, where it's always on
bsdf = next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
bsdf.inputs["Base Color"].default_value = (0.6, 0.6, 0.62, 1)
bsdf.inputs["Metallic"].default_value = 1.0
bsdf.inputs["Roughness"].default_value = 0.35
obj.data.materials.clear(); obj.data.materials.append(mat)
```

Sockets renamed in 4.0; resolve them defensively (`inputs.get("Emission Color") or inputs.get("Emission")`).

## Believable values

| Material | Base colour (linear) | Metallic | Roughness |
|---|---|---|---|
| Painted wall | 0.5–0.7 grey | 0 | 0.8–0.9 |
| Wood (varnished) | textured | 0 | 0.3–0.5 |
| Plastic | any | 0 | 0.3–0.6 |
| Polished metal | 0.9 | 1 | 0.05–0.2 |
| Rubber | 0.02–0.05 | 0 | 0.8 |
| Glass | white | 0 | 0; Transmission 1, IOR 1.45 |

Nothing in nature is pure black (0) or pure white (1) as a base colour. Keep it between ~0.02 and 0.9.

## Textures

- Poly Haven textures come as full PBR sets. `import_asset(source="polyhaven", id=...,
  apply_to=["Floor"])` builds the material and applies it in one call.
- Tiling: the import result gives the texture's real-world size. For 0–1 UVs across a surface,
  Mapping node Scale = surface size in metres / texture size in metres. Prefer large-coverage
  textures (`min_size_m` in search) for walls and floors; small ones repeat visibly.
- Objects need UVs. Primitives have them; scripted meshes need `smart_project` or box-projection
  (`Texture Coordinate > Object` into Mapping, and the Image Texture node's projection `BOX`).
- Image textures for roughness, metallic and normal maps must be Non-Color
  (`img.colorspace_settings.name = "Non-Color"`).

## Procedural

Noise, Voronoi and Wave textures through a Color Ramp make variation without images: grunge for
roughness, stains, wood rings. Mixing a little noise into roughness makes any flat material look
less CG. Look each node up before indexing its sockets (see `get_guide("bpy")`).

## Check it

Material colours only show in Material Preview or Rendered shading:
`look(mode="camera", shading="material")` or `shading="rendered"`. Solid shading shows only the
viewport display colour.
