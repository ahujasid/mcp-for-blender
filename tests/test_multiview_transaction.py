"""Mocked Python execution/transport contracts; not a live Blender test."""
import ast
import base64
import io
import json
import types
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

from test_multiview_core import MV


def load_method(capture):
    root=Path(__file__).resolve().parents[1]
    p=root/'implementation/addon_methods.py'
    if p.exists():
        source='class Fixture:\n'+p.read_text(encoding="utf-8")
    else:
        source=(root/'addon.py').read_text(encoding="utf-8")
    nodes=[n for n in ast.walk(ast.parse(source)) if isinstance(n,ast.FunctionDef) and n.name=='execute_code']
    node=nodes[0]
    fake_bpy=types.SimpleNamespace(counter=0)
    ns={'_mv_options':MV._mv_options,'_mv_capture':capture,'bpy':fake_bpy,
        'io':io,'redirect_stdout':redirect_stdout}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(p),'exec'),ns)
    return ns['execute_code'],fake_bpy


def test_default_is_unchanged_and_no_capture():
    capture=Mock()
    execute,bpy=load_method(capture)
    result=execute(None,'bpy.counter += 1\nprint("done")')
    assert result=={'executed':True,'result':'done\n'}
    assert bpy.counter==1
    capture.assert_not_called()


def test_edit_and_inspect_is_one_execution():
    capture=Mock(return_value={'image_base64':'image'})
    execute,bpy=load_method(capture)
    result=execute(None,'bpy.counter += 1',inspect={})
    assert bpy.counter==1 and result['executed']
    assert result['inspection']=={'image_base64':'image'}
    capture.assert_called_once_with({})


def test_bad_options_do_not_execute():
    capture=Mock()
    execute,bpy=load_method(capture)
    with pytest.raises(ValueError):
        execute(None,'bpy.counter += 1',inspect={'views':[]})
    assert bpy.counter==0
    capture.assert_not_called()


def test_inspection_failure_does_not_erase_success_or_replay_code():
    capture=Mock(side_effect=RuntimeError('GPU context lost'))
    execute,bpy=load_method(capture)
    result=execute(None,'bpy.counter += 1\nprint("applied")',inspect={})
    assert bpy.counter==1 and result['executed']
    assert result['inspection_error']=='GPU context lost'
    assert result['result']=='applied\n'
    capture.assert_called_once()


def test_execution_failure_does_not_capture():
    capture=Mock()
    execute,bpy=load_method(capture)
    with pytest.raises(Exception,match='Code execution error'):
        execute(None,'bpy.counter += 1\nraise ValueError("bad")',inspect={})
    assert bpy.counter==1
    capture.assert_not_called()


def load_server_helper(monkeypatch):
    root=Path(__file__).resolve().parents[1]
    p=root/'implementation/montage_server.py'
    if not p.exists(): p=root/'src/blender_mcp/server.py'
    tree=ast.parse(p.read_text(encoding="utf-8"))
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_inspection_content')
    module=types.ModuleType('mcp.types')
    module.TextContent=lambda **kw:types.SimpleNamespace(**kw)
    module.ImageContent=lambda **kw:types.SimpleNamespace(**kw)
    monkeypatch.setitem(__import__('sys').modules,'mcp.types',module)
    ns={'json':json,'base64':base64,'Image':lambda **kw:types.SimpleNamespace(**kw)}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(p),'exec'),ns)
    return ns['_inspection_content']


def test_server_keeps_image_out_of_text(monkeypatch):
    convert=load_server_helper(monkeypatch)
    png=MV._mv_png(np.zeros((20,20,4),dtype=np.uint8))
    data={'image_base64':base64.b64encode(png).decode(),'width':20,'height':20,'views':[]}
    text,image=convert(data)
    assert base64.b64decode(image.data)==png and image.type=='image'
    assert image.mimeType=='image/png' and 'image_base64' not in text.text
    assert json.loads(text.text)['size']==[20,20]


@pytest.mark.parametrize('value',[None,{'error':'unavailable'},{'image_base64':'not base64'},
                                 {'image_base64':base64.b64encode(b'not png').decode()}])
def test_invalid_image_payload(monkeypatch,value):
    with pytest.raises((ValueError,base64.binascii.Error)):
        load_server_helper(monkeypatch)(value)
