"""Reject unsupported generation controls before making paid requests."""
import ast
from pathlib import Path

import pytest


def options(model, effort=None):
    path = Path(__file__).resolve().parents[1] / 'benchmarks/multiview/agent_benchmark.py'
    if not path.exists():
        path = path.parent.parent / 'agent_benchmark.py'
    tree = ast.parse(path.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == 'generation_options')
    ns = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), ns)
    return ns['generation_options'](model, effort)


def test_supported_temperature_keeps_existing_deterministic_request():
    assert options({'supported_parameters': ['tools', 'temperature']}) == {'temperature': 0}


def test_reasoning_model_does_not_receive_unsupported_temperature():
    assert options({'supported_parameters': ['tools', 'reasoning']}, 'low') == {
        'reasoning': {'effort': 'low'}}


def test_default_reasoning_settings_are_not_overridden():
    assert options({'supported_parameters': ['tools', 'reasoning']}) == {}


def test_unsupported_reasoning_is_rejected_before_spending():
    with pytest.raises(ValueError, match='reasoning controls'):
        options({'supported_parameters': ['tools', 'temperature']}, 'low')
