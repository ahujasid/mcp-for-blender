"""Server instructions carry the rules that keep generated Blender code working.

asset_creation_strategy is only returned when a client calls prompts/get. Clients
that never do previously received no guidance at all, which is how generated scripts
ended up looking shader nodes up by localized name (#26) and hardcoding render engine
identifiers from a different Blender version (#110).

These tests deliberately assert on API identifiers rather than prose, so the wording
stays free to change.
"""

from blender_mcp.server import SERVER_INSTRUCTIONS, asset_creation_strategy, mcp


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


def test_prompt_still_works():
    strategy = asset_creation_strategy()
    assert "get_polyhaven_status" in strategy
    assert "BSDF" in strategy
