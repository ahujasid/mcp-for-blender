# Quality extension implementation status

Development build 1.9.1+quality.1, add-on 1.6.1, quality extension protocol 1. The socket protocol is 6 so older add-ons are detected during the connection handshake. Root and bundled add-ons are identical.

This build supplies inspection, recovery, visual feedback, animation diagnostics, reference comparison and experimental sculpt editing. It does not establish an artistic-quality improvement percentage or complete every research proposal in IMPROVEMENT_PLAN.md.

## Discoverable tools

| Area | Tools | Implemented behavior |
| --- | --- | --- |
| Discovery | get_quality_capabilities, get_quality_status | Server catalog and separate live Blender capability/context report; effect annotations and detailed parameter/evidence descriptions. |
| Scene and mesh | inspect_scene, validate_mesh | Searchable pagination, evaluated mesh budgets, degeneracy and boundary/loose/overused-edge counts. |
| Recovery | create_checkpoint, restore_checkpoint | Temporary saved copies, bounded session retention, rescue copy before restore. |
| Visual review | preview_target, preview_animation, preview_camera | Six orthographic directions, optional fixed framing, labeled pose sequences locked to the first sample, and Workbench renders from an existing scene camera with projection metadata. View/timeline/render settings restored on exit. |
| Rig and motion | inspect_rig, inspect_motion, validate_animation, validate_deformation | Bones, constraints, deform weights, evaluated world and bone-relative transforms, loop and attachment position/orientation checks, declared contact-marker sliding/flat-ground tests, evaluated edge stretch/collapse across poses. |
| References | compare_reference | Side-by-side comparison or caller-aligned overlay; persistent camera previews can feed this tool. No invented accuracy percentage or verification of hidden surfaces. |
| Sculpting | inspect_sculpt, sculpt_region | Bounded radial displacement with quadratic falloff, mask preservation, checkpoint and rollback on edit errors. Optional target revision rejects stale edits; operation identifiers deduplicate successful session requests. |
| Export | export_asset, validate_export | GLB export with explicit scene/action/static profiles and bounded ranges. Fresh Blender import with matching frame rate; optional world and object/bone-relative source comparison at sampled frames. |

All eighteen quality tools are registered with the MCP server. Tool descriptions explain prerequisites, effects, evidence limits and appropriate follow-up checks.

## Validation

- All 148 pytest checks pass: the original 113 regressions, 25 scene and mesh checks, and 10 quality-tool contracts.
- Blender 5.2.1 LTS background fixtures pass: pagination, newly linked and excluded objects, evaluated subdivision, open boundaries, intentional motion jumps, checkpoint save/restore, and background viewport rejection.
- Advanced real Blender fixtures pass: deliberate stretch, bad weights, positional and angular loop/attachment defects, contact-marker warnings, protected sculpt masks, stale-edit rejection, duplicate-request suppression, image comparisons and export.
- Interactive fixtures pass: six preview directions and viewport restoration after an injected capture error. A render-failure contract verifies camera/render-setting restoration.
- A real MCP stdio client exercises all eighteen tools against a disposable interactive Blender server, including image delivery and continued inspection after checkpoint recovery.
- The wheel was built without downloading dependencies, installed into an isolated test directory, checked for matching bundled files, and passed the same eighteen-tool live bridge test from that installed package.
- The export fixture includes a skinned rig and bone-parented held prop at 60 fps. Reimport preserves sampled world and attachment-relative position/orientation within configured tolerances. A deliberate 0.5-unit source offset is detected.
- Testing exposed a separate-action import mismatch. The default SCENE profile now bakes one coordinated clip; ACTIONS remains an explicit choice for separate clips.
- Export-worker testing exposed inherited standard-input interference. The disposable process now receives closed input and reports bounded timeouts.

Tests write generated artifacts under test-artifacts, including preview-result.txt and bridge/results.json. These files are excluded from Git. Automated integration tests use disposable Blender windows. Separate hands-on testing installed the matching add-on in a dedicated Blender session and exercised model construction, mesh checks, checkpoints, previews, and animation sampling with an animated cube and a detailed phone reference model. These local assets are excluded from Git.

## Limits and further research

- These are constructed technical fixtures and limited hands-on examples, not a benchmark proving improved artistic results. Broader representative assets still need evaluation.
- Sculpting is experimental radial displacement. Native brush assets, dyntopo, multires detail editing, remeshing, retopology and anatomy/likeness assessment are not implemented. Shared meshes, shape keys, armatures and multires edits are rejected.
- Reference support uses an existing camera and caller-confirmed alignment. Automatic camera fitting, landmark detection and semantic scoring are not implemented. A front-only image cannot verify depth or the back.
- Mesh diagnostics do not cover self-intersections, near-coplanar faces, UV overlap/distortion, complete material/texture audits or bake artifacts.
- Animation checks are sampled and intention-dependent. Contact uses origins/markers and a flat plane; deformation uses corresponding edges. No arbitrary ground-surface collision, anatomy judgment, complete skin-surface round-trip comparison or continuous playback video is claimed.
- Revision protection covers radial sculpt edits, not the entire scene or arbitrary Python. The operation ledger and checkpoint registry are session-local and do not survive an add-on restart. There is no asynchronous cancellable job service.
- Export is limited to 2000 frames. Multiple actions require explicit clip interpretation. Blender import is not Roblox Studio or other target-engine certification.
- Image tools and export checks assume shared local paths between Blender and the MCP server. Previews require graphics support; viewport tools reject background mode. Blender 5.2.1 and the installed MCP SDK were exercised; older Blender/exporter versions are not certified.
- Temporary checkpoints exclude external files/remote work and are recovery aids, not permanent backups.

## Installable build and local checks

The wheel is dist/blender_mcp-1.9.1+quality.1-py3-none-any.whl. It bundles the matching add-on and export-validation worker. The root addon.py is the standalone Blender add-on.

For hands-on testing, use a separate Blender session and launch the server from this fork or wheel. The ordinary published uvx blender-mcp command uses the upstream package rather than this development build. Installing this repository does not publish a new upstream PyPI package.

Developer checks from this repository:

```text
.venv/Scripts/python.exe -m pytest -q
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python tests/blender_quality_smoke.py
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python tests/blender_advanced_smoke.py
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python tests/blender_scene_inspection.py
.venv/Scripts/python.exe tests/run_bridge_integration.py
```

The bridge runner creates, controls and closes its own factory Blender process. Set BLENDER_TEST_EXECUTABLE when Blender is installed elsewhere. It needs reference images produced by the advanced smoke test. BLENDER_MCP_TEST_PACKAGE optionally selects a separately installed wheel directory for the server-side test.
