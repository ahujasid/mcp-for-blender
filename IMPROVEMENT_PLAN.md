# Blender MCP — researched quality roadmap

Updated September 12, 2026. This replaces the initial low-poly-focused plan. This document is the research roadmap, not a completion report. See IMPLEMENTATION_STATUS.md for the verified development build, tool coverage, tests and remaining research. The user subsequently narrowed agent guidance to discoverable tool descriptions and excluded worked examples or a separate workflow guide. Current sculpt revision protection is target-scoped, and current pose previews are sampled images rather than continuous video.

## Outcome and scope

Build an integration that helps an agent deliver finished, editable Blender work across stylized, medium-detail, and high-detail modeling; materials; rigging; animation; and experimental sculpting. Low polygon count is a production constraint, not a measure of artistic quality. The user’s gateway, cactus, and tree remain regression examples, not the scope boundary.

The strongest improvement is a closed feedback loop: understand the brief and scene, execute a bounded edit, observe the real result, check technical correctness, critique appearance or motion, revise, and verify the exported deliverable. More tool names alone will not establish better artistic judgment.

## What the source already provides

The reviewed community repository includes Python execution, object inspection, viewport capture with an offscreen path, main-thread command scheduling, richer internal scene-snapshot helpers, and regression tests. Basic scene listing stops at ten objects, but Python execution can inspect the remainder. Existing snapshot helpers should be assessed for reuse rather than duplicated. Root and packaged add-on copies must remain synchronized. This is a community project, not an official Blender integration.

## Design principles grounded in documentation

- Blender operators depend on context, and Blender data access has threading constraints. Preserve main-thread scheduling; prefer direct data APIs when suitable; explicitly validate mode and context for operators. Network handling must not mutate Blender data. [1][2]
- MCP supports structured results and output schemas. Use compact, validated results instead of prose-only success messages, subject to the installed SDK and negotiated protocol. Tool annotations are descriptive hints, not access controls. [3]
- Armatures, skin weights, constraints, and actions form separate parts of animation. A rig existing is not evidence of good deformation or motion. [4]
- Multiresolution supports editing at subdivision levels; sculpting also has topology-changing workflows. Treat these as different operations with different preservation and memory requirements. [5][6]
- Export changes representation: UV seams and shading splits can increase exported vertex counts, and animation formats have supported-channel limits. Inspect exports, not just the working mesh. [7]
- Roblox has asset-type-specific requirements. Keep its checks in a target profile rather than imposing avatar rules on every Blender model. [8]

These sources establish API behavior and available mechanisms. The tool architecture, heuristics, and acceptance criteria below are engineering proposals, not capabilities guaranteed by those sources. Version-specific documentation must match the actual installed Blender version before coding.

## A. Agent-facing foundation — highest priority

### Capability and session report

Expose Blender/add-on versions, supported operations, current file, active scene/view layer/mode, render availability, enabled integrations, and units. Include a session identifier and a scene revision token. Clearly identify unsupported features. Discover relevant capabilities without sending the entire mesh or a huge tool catalog on every call.

### Bounded observation

Provide searchable, paginated objects and collections; stable references with explicit deleted/renamed-target handling; selection; parenting; instances; linked/shared mesh data; modifiers; materials; dimensions; and animation bindings. Offer summary and detailed modes, and request selected geometry only when needed. Report raw and evaluated triangles separately. Mesh element indices must be treated as stale after topology changes.

### Reliable edit contracts

Every edit should report success, partial failure, changed objects, warnings, checkpoint reference, and the resulting scene revision. Validate targets, coordinate space, units, mode, and parameters before mutation. Detect edits made by the user since observation; re-inspect rather than blindly replaying stale assumptions. Avoid automatically retrying uncertain mutations: use operation identifiers and a bounded result ledger to prevent duplicate edits. Crash recovery remains a separate problem.

Keep Python as an escape hatch, paired with preconditions, checkpoint options, and post-edit observations. Do not pretend unrestricted scripts can always be rolled back atomically. Reuse the existing safe-mode policy and do not weaken it for convenience.

### Long-running operations

Use a job interface for previews, baking, export, and expensive diagnostics: start, inspect progress, retrieve result, request cancellation. Support queued cancellation immediately; active cancellation is best effort and only where the underlying Blender operation permits it. Never interrupt Blender data mutation from a worker thread. Enforce time, geometry, image-size, and memory budgets. Use MCP progress features only after checking SDK/host support; ordinary job tools are the fallback.

## B. Visual feedback that supports actual judgment

Create consistent front/side/back/top and three-quarter views, with target isolation, solid/clay, material, wireframe, and normals overlays. Include framing, lighting, resolution, and color-management metadata. Restore user viewport, frame, selection, shading, and render state on every exit path.

Use inexpensive contact sheets for routine inspection and high-resolution crops only for suspicious areas. Add object-ID overlays and surface picking so an agent can relate a visible flaw to the correct object or region. Picking must report coordinate space and confidence; hidden surfaces are not inferred from one image.

Reference boards should record intended silhouette, proportions, style, scale, material treatment, and areas that must remain unchanged. Before/after comparisons must use identical cameras and lighting. An image similarity score alone is not an aesthetic acceptance test.

## C. Modeling and material quality

A read-only validation report should distinguish confirmed defects, configurable budget violations, heuristic warnings, and artistic concerns.

Deterministic checks: degenerate faces, loose elements, missing images/material references, UV presence when required, invalid transforms, evaluated geometry budgets, and export prerequisites. Report manifold/boundary information, but allow intentional open surfaces and separate components.

Heuristic checks: duplicate or near-coplanar faces, self-intersections, thin regions, likely shading artifacts, density imbalance, and UV distortion or unintended overlap. These need tolerances tied to scale and documented false positives. Intentional mirrored/stacked UVs must be supported. Never automatically weld or remesh every flagged object.

Detailed models need separate quality profiles for subdivision, hard-surface, organic, and real-time assets. Inspect silhouette and highlights under neutral lighting; check bevel scale and modifier ordering; preserve UVs, materials, shape keys, and shared-data semantics during edits. UV and material checks should include appropriate texture resolution, color-space assignments, and missing/broken texture paths. Bake checks should inspect actual output for artifacts rather than report only that a bake completed.

Suggested tools: inspect_scene, inspect_object, inspect_region, preview_asset, validate_asset, apply_edit. Begin with a small coherent set rather than a command for every Blender operator.

## D. Rigging and animation — first-class deliverables

### Rig inspection

Expose hierarchy, rest and pose transforms, deform bones, constraints and targets, drivers, skin groups, shape keys, actions, and NLA relationships. Account for version-dependent action APIs. Report unweighted vertices, non-normalized deform weights where applicable, invalid constraint targets, and engine-specific influence limits. Do not misclassify control groups as deform weights.

### Deformation checks

Use user-appropriate pose suites: shoulders, elbows, knees, wrists, hips, neck, and facial poses where supported. Capture mesh deformation at each pose, flag extreme collapse/stretch and intersections as heuristics, and compare silhouette. Save and restore original pose and animation bindings. No assumption that all characters are humanoid.

### Motion checks

Inspect evaluated motion across a frame range, including constraints and NLA, not merely authored keyframes. Produce a playable preview plus contact sheets with frame labels, key poses, and trajectories. Test loop closure in position and orientation and continuity near the seam. Treat intentional cuts and stepped animation differently from smooth motion.

Add candidate warnings for sudden velocity changes, foot sliding during declared contact intervals, ground penetration relative to a specified surface, root-motion drift, and limb popping. These require rig mapping and user-defined contact/loop intent; they are not universal numerical proofs of good animation. Sampling can miss between-frame defects, so increase sampling around suspected problems and report coverage.

Acceptance includes timing, readable poses, anticipation/follow-through where intended, and deformation during motion. Human visual judgment remains necessary. A valid rig and smooth curves alone are not enough.

Suggested tools: inspect_rig, preview_animation, validate_animation, edit_animation. Editing must identify the action, frame range, channel scope, interpolation, and preservation constraints.

## E. Sculpting — phased and explicitly experimental

Start with inspection of sculpt mode, multires levels, masks, face sets, symmetry, and geometry/memory budgets. Support recoverable setup and bounded operations before pursuing arbitrary brush automation. Multires subdivisions and topology-changing remesh/dyntopo operations need different contracts; the latter invalidate element references and can damage attributes or deformation setups.

A brush-stroke prototype should use explicit surface-space or world-space samples, radius units, strength, falloff, symmetry, target region, and a preview. Validate the specific Blender version and brush asset/context requirements. Prefer surface-anchored input to unrepeatable screen-coordinate dragging.

Benchmark simple forms and relief first, then organic refinement. Do not promise autonomous high-end anatomy, likeness, perfect retopology, or facial sculpting. Stop at experimental support if repeatability, preservation, or runtime tests fail. Keep a recoverable high-resolution master and test any low-resolution/baked derivative separately.

## F. Checkpoints and final delivery

Create checkpoints before destructive topology, rig, or animation changes. Prefer explicit saved copies with metadata and bounded retention. Restore tests must cover unsaved scenes, missing external assets, and failed saves. File checkpoints do not undo remote jobs or arbitrary external-file changes.

Deliver an editable project, requested exports, preview images/video, and a concise validation report with tested target/version, unresolved warnings, and reproduction settings. Export a copy using a format-specific profile; reopen it in a clean Blender process and compare scale, orientation, object coverage, textures, rig mapping, and animation samples. A Blender round trip does not prove target-engine behavior: run a Roblox Studio import test when that is the target and access is available. Otherwise explicitly report it untested.

Avoid overwriting the user's working project or auto-applying every modifier. Export conversion belongs on a copy.

## G. Revision workflow and stopping rules

Store the brief, references, asset profile, preserved regions, and quality criteria in project-local task state. After each meaningful edit: observe, run relevant checks, compare against the brief, select the largest remaining issue, revise only the needed region, and verify again.

Use a configurable revision and runtime budget. Reject or restore revisions that break preservation constraints or introduce new hard failures. Stop when criteria pass, when further edits stop improving the result, or when uncertainty requires user judgment. The report should never imply that a missing check passed.

Keep quality guidance separate from the MCP transport: tools provide evidence and controls; the agent workflow requires their use. This allows the same integration to support different models without hard-coding one model's prompts into every tool.

## H. Evaluation plan and release gates

1. Baseline the existing test suite and original integration before changing behavior.
2. Unit tests: schemas, pagination, stale IDs/revisions, bounded outputs, retry deduplication, response errors, and packaged add-on parity.
3. Real Blender integration tests: evaluated modifiers, shared meshes, deliberate partial failures, checkpoint restore, rig weights/constraints, animation sampling, and export round trips.
4. Interactive tests: previews with a hidden/background window, multiple scenes/viewports, state restoration, and active user edits. Headless tests cannot certify viewport behavior.
5. Benchmark the same briefs with the same agent model, settings, references, and budgets before and after changes. Repeat runs; measure technical pass rate, unintended changes, user corrections, time, calls/output size, and blinded visual preference. Avoid publishing an improvement percentage before measurement.
6. Extend fixtures beyond the three screenshots: medium-detail hard-surface prop, UV/PBR asset, skinned character stress poses, looping walk, non-loop action with contact, mechanical animation, and high-resolution sculpt preservation.

Hard release gates: no silent partial-success reports; tested checkpoint recovery; no unintended edits outside the target; no known failing existing regressions; all declared required export checks executed or explicitly marked unavailable. Performance thresholds are set after measuring representative hardware and scenes.

## Implementation order

Milestone 1: capability handshake, structured contracts, paginated inspection, revisions, and checkpoint recovery. Establish tests first.

Milestone 2: reproducible previews, read-only geometry/material reports, and evidence-based revision workflow. Compare baseline delivery quality.

Milestone 3: rig and animation inspection, pose suites, temporal previews and diagnostics, and format-specific export validation. Animation is core scope, not an optional afterthought.

Milestone 4: detailed-model preservation, baking diagnostics, and experimental sculpt tools, each gated by real Blender tests.

Milestone 5: benchmark-driven optimization, documentation, packaging, and release preparation. Defer unrelated generation services, large UI redesigns, and unmeasured tool expansion.

No fixed completion estimate until Blender version, executable, representative scenes, and target budgets are available. A prototype is not equivalent to verified modeling/animation quality.

## Research references

[1] Blender API gotchas: https://docs.blender.org/api/dev/info_gotcha.html
[2] Blender threading constraints: https://docs.blender.org/api/main/info_gotchas_threading.html
[3] MCP tool results, schemas, and annotations: https://modelcontextprotocol.io/specification/2025-06-18/server/tools
[4] Blender armatures, skinning, and posing: https://docs.blender.org/manual/en/latest/animation/armatures/index.html
[5] Blender multiresolution modifier (versioned reference): https://docs.blender.org/manual/en/4.4/modeling/modifiers/generate/multiresolution.html
[6] Blender sculpting features: https://www.blender.org/features/sculpting/
[7] Blender glTF exporter (versioned reference): https://docs.blender.org/manual/en/4.0/addons/import_export/scene_gltf2.html
[8] Roblox asset-type requirements: https://create.roblox.com/docs/art/modeling

Official sources were consulted for this revision. Specific API calls, exporter versions, diagnostic thresholds, and host feature support still require implementation-time verification.

## Additional acceptance checks — references and attached animation props

### Reference-image fidelity

When modeling from images, retain the reference and record which views it actually shows. Provide tools to align a Blender camera to the reference, render the model from that camera, and produce side-by-side, silhouette, and optional transparent-overlay comparisons. Record camera projection, framing, scale, and alignment confidence; perspective and orthographic views are not interchangeable. Keep lighting/material comparison separate from shape comparison.

Compare silhouette, proportions, feature placement, visible component count, and color/material regions. Optional landmark reprojection and silhouette-overlap measurements may support the review once alignment is meaningful. Do not label a raw pixel-similarity score as an overall accuracy percentage. Report individual measurements and qualitative findings, including registration uncertainty and occlusions. Reference overlays and camera fitting are proposed tooling; semantic resemblance and artistic decisions remain agent responsibilities.

A single front image does not establish the hidden back, depth, or internal construction. Preserve the visible evidence and explicitly record inferred geometry. Use coherent plausible depth for ordinary drafts; request additional views only when the missing information materially affects required accuracy. Deliver a reference-matched front preview and independent side/back views so invented geometry can be inspected. Never claim full 3D accuracy from one visible projection.

Test cases: front-only stylized prop; perspective photograph; multiple consistent views; conflicting views; partially occluded asset. Check that camera misalignment is not mistaken for a modeling defect and that a good front silhouette does not hide implausible depth. Acceptance requires both reference fidelity on observed surfaces and coherent 3D construction elsewhere.

### Attachment and position continuity in animation

For held props and hand-offs, including a fictional game reload animation, define intended relationships over frame intervals: which hand/bone/socket holds each prop, when release or transfer is intentional, and which coordinate space each offset uses. Inspect evaluated world transforms and relative transforms to the declared attachment, not only local keyframes. Include scene units, parent inverses, constraints/influence, root motion, and rig/object scale when diagnosing jumps.

Track positional and angular discontinuities, unexpected changes in hand-to-prop offset, and unintended movement away from the character. Set tolerances from asset scale and the animation brief rather than assuming one universal distance. Sample around parenting/constraint transitions and suspect intervals; intentional throws, fast motion, cuts, and releases must not be automatically corrected. Flag and preview exact suspect frames, with trajectories and before/after poses.

Acceptance fixture: a held prop remains attached through a normal motion; a deliberate constraint hand-off preserves its intended world pose; an injected parent-inverse or coordinate-space error is detected; an intentional release is accepted. Repeat these checks after export/import, since baking or hierarchy conversion may change attachment behavior. Compare exported samples with the evaluated source using documented position/orientation tolerances. Preserve original animation before repairs.
