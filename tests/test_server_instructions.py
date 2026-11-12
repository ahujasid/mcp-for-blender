"""Server instructions carry the rules that keep generated Blender code working.

The guidance used to live in an asset_creation_strategy prompt, but MCP prompts
are user-invoked and the model can't fetch one, so clients effectively got none.
That is how generated scripts ended up looking shader nodes up by localized name
(#26) and hardcoding render engine identifiers from a different Blender version
(#110).

These tests deliberately assert on API identifiers rather than prose, so the wording
stays free to change.
"""

from blender_mcp.server import SERVER_INSTRUCTIONS, mcp


def test_instructions_are_advertised_to_clients():
    assert mcp.instructions
    assert mcp.instructions == SERVER_INSTRUCTIONS


def test_instructions_name_the_apis_that_keep_scripts_portable():
    # Node type lookup instead of localized names (#26); reading enum values
    # instead of hardcoding identifiers (#110).
    assert "BSDF_PRINCIPLED" in SERVER_INSTRUCTIONS
    assert "bl_rna" in SERVER_INSTRUCTIONS
    assert "get_addon_status" in SERVER_INSTRUCTIONS


def test_instructions_stay_small_enough_to_inject_every_turn():
    # Instructions go into every conversation; #347 tracks context cost.
    assert len(SERVER_INSTRUCTIONS) < 2500


def test_instructions_carry_the_asset_workflow():
    for name in ("get_addon_status", "world_bounding_box", "generate_3d", "search_assets", "import_asset"):
        assert name in SERVER_INSTRUCTIONS


def test_instructions_do_not_point_at_an_unreachable_prompt():
    assert "asset_creation_strategy" not in SERVER_INSTRUCTIONS


def test_instructions_send_the_model_to_look():
    assert "look" in SERVER_INSTRUCTIONS
