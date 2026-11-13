# MCP for Blender

A community-built, open-source project. Not affiliated with or endorsed by the Blender Foundation.

MCP for Blender connects Claude to the Blender running on your computer. Claude can read your scene, write and run Blender Python to create and edit objects, materials, lighting and animation, and look at the result from any angle to check its own work. It can also pull free models, textures and HDRIs from Poly Haven, Sketchfab and Poly Pizza, and generate new 3D models or Gaussian-splat worlds with services you choose to switch on.

The plugin runs the open-source [`mcp-for-blender`](https://pypi.org/project/mcp-for-blender/) server on your machine. Source: [github.com/ahujasid/mcp-for-blender](https://github.com/ahujasid/mcp-for-blender).

## Where it works

- **Claude Code**: terminal, IDE extensions and the desktop app's Code tab
- **Cowork**, when the session runs on your computer

The server talks to Blender on your machine, so it doesn't run in Claude chat on the web or mobile.

## Setup

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/), which runs the server.
2. Install the Blender addon. From a terminal:

   ```bash
   uvx mcp-for-blender==2.2.8 install-addon
   ```

   Or download [`addon.py`](https://github.com/ahujasid/mcp-for-blender/blob/main/addon.py) and install it in Blender under **Edit → Preferences → Add-ons → Install…**.
3. In Blender, enable **Interface: MCP for Blender**, open the **MCP for Blender** tab in the 3D viewport's sidebar (press <kbd>N</kbd>) and click **Connect to Claude**.
4. Install the plugin. In Claude Code:

   ```
   /plugin install mcp-for-blender --marketplace ahujasid/mcp-for-blender
   ```

5. Ask Claude to build something.

If you added the server by hand before (`claude mcp add blender …`), remove that entry so Claude doesn't see the tools twice.

## Tools

| Tool | What it does |
|---|---|
| `get_addon_status` | Blender version, addon version, and which libraries and generators are switched on |
| `get_scene_info` | Objects in the scene with their positions and sizes |
| `execute_blender_code` | Runs Python inside Blender with the full `bpy` API |
| `look` | Viewport screenshots from any angle, and renders |
| `search_assets`, `import_asset` | Find and import models, textures and HDRIs from Poly Haven, Sketchfab and Poly Pizza |
| `generate_3d` | Generates one textured model from text or an image (Hyper3D Rodin, Hunyuan3D, Meshy, Tripo) |
| `generate_gaussian_world` | Generates a Gaussian-splat world with World Labs |
| `record_trajectory_feedback` | Records your feedback on a session, only if you opted in to sharing |
| `disable_telemetry` | Stops sharing prompts, code, screenshots and scene data, if you opted in |

## What it runs, sends and fetches

- **Runs**: `uvx mcp-for-blender==2.2.8`, which downloads that exact version of the server from PyPI and starts it on your machine.
- **Blender**: the server connects to the addon over a local socket (`localhost:9876` by default). `execute_blender_code` runs arbitrary Python in Blender, so save your work. The socket has no authentication, so keep it on `localhost`.
- **Asset libraries**: when you switch them on in the addon, Blender searches and downloads from Poly Haven (`api.polyhaven.com`), Sketchfab (`api.sketchfab.com`, with your API key) and Poly Pizza (`api.poly.pizza`, with your API key). Search results show thumbnails from those services.
- **3D generation**: when you switch a generator on, Blender sends your prompt or image to that provider with your own API key: Hyper3D Rodin (`hyperhuman.deemos.com`, or `queue.fal.run` in fal.ai mode), Hunyuan3D (Tencent Cloud), Meshy (`api.meshy.ai`), Tripo (`openapi.tripo3d.ai`, or `openapi.tripo3d.com` for China accounts) or World Labs (`api.worldlabs.ai`). With an MCP for Blender Premium subscription, the request goes through the MCP for Blender backend (hosted on Supabase) instead, which calls the provider for you.
- **Addon updates**: when Blender starts, the addon checks `raw.githubusercontent.com/ahujasid/blender-mcp` for a newer `addon.py` and shows an **Update Addon** button if there is one. It installs nothing unless you click it. Set `BLENDERMCP_NO_UPDATE_CHECK=1` to skip the check.
- **Telemetry**: by default, the server sends a minimal anonymous usage record to the MCP for Blender backend (Supabase): a random install ID, a session ID, the tool name, whether it succeeded, how long it took, the MCP for Blender and Blender versions, your operating system and a timestamp. Your prompts, code, screenshots and scene data are **not** sent unless you opt in, either in the addon preferences or through a one-time prompt some clients show. Asking Claude to stop data collection (`disable_telemetry`) turns off that opt-in sharing. To also turn off the anonymous usage record, set `DISABLE_TELEMETRY=true` in the environment Claude runs in.
- **Local files**: the server keeps its install ID and a list of pending generations in your user data folder. It never modifies your Blender addon files on its own.

API keys are stored in Blender's add-on preferences, not in this plugin.

## Privacy Policy

The [privacy policy](https://www.mcp-for-blender.com/privacy) covers what is collected, how it's used and stored, who it's shared with, how long it's kept, and how to reach us. Full terms for opt-in data are in [TERMS_AND_CONDITIONS.md](https://github.com/ahujasid/mcp-for-blender/blob/main/TERMS_AND_CONDITIONS.md). Contact: ahujasid@gmail.com.

## Support

[mcp-for-blender.com/support](https://www.mcp-for-blender.com/support), [GitHub issues](https://github.com/ahujasid/mcp-for-blender/issues) or [Discord](https://discord.gg/SNqPn4TcKQ).

## License

MIT. See [LICENSE](LICENSE).
