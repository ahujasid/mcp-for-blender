# Multi-view validation and benchmark

These scripts use a disposable scene. `native_validation.py` and
`transport_validation.py` require no model or API key. The agent comparison
uses OpenRouter and records actual provider usage and charges.

Install development dependencies in a virtual environment:

```sh
python -m pip install -e . -r benchmarks/multiview/requirements.txt
python -m pytest -q tests
```

Run the native checks in GUI Blender (on Linux, a virtual display works):

```sh
xvfb-run -a blender --factory-startup --python benchmarks/multiview/native_validation.py -- \
  --addon "$PWD/addon.py" --out "$PWD/benchmarks/multiview/results/native"
python -c 'import json; assert json.load(open("benchmarks/multiview/results/native/native.json"))["passed"]'
```

The native report is authoritative: Blender can exit successfully even when a
timer callback records a failed assertion. It checks distinct views, selection
and active-object preservation, visibility restoration, an injected capture
failure, and edit-plus-inspect behavior. The included before-fix report records
the selection regression this test found.

For live transport or the agent comparison, keep this disposable Blender
process running in a separate terminal:

```sh
xvfb-run -a blender --factory-startup --python benchmarks/multiview/boot_blender.py -- \
  --addon "$PWD/addon.py" --port 19876
```

Then run:

```sh
python benchmarks/multiview/transport_validation.py \
  --out benchmarks/multiview/results/transport
```

That check uses the real STDIO MCP server and Blender socket. It verifies native
image content, custom angles, the legacy text-only result, exactly-once edits
after inspection failure, and rejection of malformed inspection options before
mutation. It creates only a test cube in the disposable process.

For the paid comparison, supply `OPENROUTER_API_KEY` through your environment
and use a provider key with its own spending limit. The runner's pre-call
reservations are estimates; they cannot enforce an absolute billing cap.

```sh
python benchmarks/multiview/agent_benchmark.py --checkout . \
  --model openai/gpt-5.6-terra --budget 4.0 --repeats 3 \
  --max-turns 12 --max-tokens 4096 --reasoning-effort low \
  --out benchmarks/multiview/results/agent
python benchmarks/multiview/report.py \
  benchmarks/multiview/results/agent/summary.json benchmarks/multiview/results/report
```

Both arms choose their own tool calls. Their final geometry is graded outside
the measured interval. All attempts remain in the results, including failures.
Read the report alongside the final images: the grader checks dimensions,
positions and brace removal, not artistic quality. Small-scene results on Mesa
software rendering do not establish performance on hardware GPUs or large scenes.
