#!/usr/bin/env python3
"""Real OpenRouter vision-agent/MCP A/B benchmark. No mocked token/latency results.

Requires: mcp>=1.9,<2, httpx, a running disposable Blender addon on --port.
API key comes ONLY from OPENROUTER_API_KEY; never logged, serialized or committed.
The baseline exposes the unchanged screenshot/execution contract; enhanced adds
multi-view observations and optional edit+inspect. Neither mode is forced to
inspect a specific number of views. Both may batch coherent edits/tool calls.
"""
from __future__ import annotations
import argparse
import asyncio
import base64
import copy
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TASKS = [
    'Build a simple toy gantry. Use metres. Make two vertical blue posts, LeftPost and RightPost, '
    'centred at x=-1 and x=1, y=0, z=1, each 0.2 by 0.2 by 2.0. Add a blue top beam '
    'named Beam centred at (0,0,2.1), dimensions (2.2,0.2,0.2). Add a thin diagonal rear '
    'brace named RearBrace behind the frame at y=0.2. Keep the front opening clear. '
    'Inspect the result and fix visible placement mistakes before completing this step.',
    'Raise the gantry by 0.5m without moving its feet: both posts should now be 2.5m tall '
    'with centres at z=1.25, and the beam centre should be at z=2.6. Update the brace '
    'to meet the upper structure. Inspect and correct attachment/position mistakes.',
    'Add two small red square plates, FrontPlate and RearPlate, each (0.4,0.06,0.4). '
    'Their centres must be (0,-0.13,2.6) and (0,+0.13,2.6), touching opposite faces '
    'of the beam. Keep both posts unchanged. Inspect both plate attachments.',
    'Remove the rear diagonal brace only. Preserve both plates, posts, and beam. '
    'Inspect the finished object from sufficient angles to check that the brace is gone '
    'and both plates remain attached. Report completion only when it matches.',
]
SYSTEM = ('You are operating a disposable Blender scene using MCP tools. Fulfil each user '
          'request accurately. Use visual observations when needed to check spatial results. '
          'Choose your own efficient workflow; you may batch edits and tool calls. '
          'Do not install packages, access networks, or read unrelated files. Do not use '
          'external models/assets or generate final beauty renders. Keep code and replies concise.')
RESET = r'''
import bpy
from mathutils import Vector
if bpy.context.object and bpy.context.object.mode != 'OBJECT':
    bpy.ops.object.mode_set(mode='OBJECT')
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
for material in list(bpy.data.materials):
    if material.users == 0:
        bpy.data.materials.remove(material)
bpy.context.scene.frame_set(1)
for window in bpy.context.window_manager.windows:
    for area in window.screen.areas:
        if area.type != 'VIEW_3D':
            continue
        space = area.spaces.active
        space.shading.type = 'SOLID'
        space.shading.color_type = 'MATERIAL'
        space.overlay.show_overlays = False
        space.show_gizmo = False
        space.clip_start = .001
        space.clip_end = 1000
        region = space.region_3d
        center = Vector((0, 0, 1.3))
        offset = Vector((5, -7, 5)) - center
        region.view_rotation = offset.to_track_quat('Z', 'Y')
        region.view_location = center
        region.view_distance = offset.length
        region.view_perspective = 'PERSP'
bpy.context.view_layer.update()
'''

GRADER = r'''
import bpy, json
expected = {'LeftPost':((-1,0,1.25),(.2,.2,2.5)),
            'RightPost':((1,0,1.25),(.2,.2,2.5)),
            'Beam':((0,0,2.6),(2.2,.2,.2)),
            'FrontPlate':((0,-.13,2.6),(.4,.06,.4)),
            'RearPlate':((0,.13,2.6),(.4,.06,.4))}
bpy.context.view_layer.update()
checks={}
for name,(loc,dim) in expected.items():
    o=bpy.data.objects.get(name)
    checks[name]=bool(o and all(abs(float(o.matrix_world.translation[k])-loc[k])<.025 and
                              abs(float(o.dimensions[k])-dim[k])<.025 for k in range(3)))
checks['brace_removed']=bpy.data.objects.get('RearBrace') is None
checks['no_extra_geometry']=len([o for o in bpy.context.scene.objects if o.type=='MESH'])==5
print(json.dumps(checks))
'''

class BudgetExceeded(RuntimeError): pass

class Budget:
    def __init__(self,limit):
        self.limit=limit; self.spent=0.0; self.uncertain=False
    def reserve(self,amount):
        if self.uncertain or amount>self.limit-self.spent:
            raise BudgetExceeded(f'Next call reservation ${amount:.4f} exceeds remaining ${self.limit-self.spent:.4f}')
    def charge(self,amount):
        if amount is None:
            self.uncertain=True
            raise BudgetExceeded('Provider omitted actual cost; stopping rather than pretending it was free')
        self.spent+=float(amount)
        if self.spent>self.limit:
            raise BudgetExceeded('Measured cost exceeded budget; no further calls will be made')


def clean_for_estimate(messages):
    cleaned=copy.deepcopy(messages); images=0
    for message in cleaned:
        if isinstance(message.get('content'),list):
            for part in message['content']:
                if part.get('type')=='image_url':
                    images+=1
                    part['image_url']={'url':'[native image]'}
    return cleaned,images


def tool_schema(tool,mode):
    schema=copy.deepcopy(tool.inputSchema)
    description=tool.description or ''
    if mode=='baseline' and tool.name=='execute_blender_code':
        schema.get('properties',{}).pop('inspect',None)
        if 'required' in schema: schema['required']=[n for n in schema['required'] if n!='inspect']
        description=('Execute arbitrary Python code in Blender. Make sure to do it step-by-step '
                     'by breaking it into smaller chunks. code: Python code; user_prompt: user goal.')
    return {'type':'function','function':{'name':tool.name,'description':description,'parameters':schema}}


def generation_options(model, reasoning_effort=None):
    supported = set(model.get('supported_parameters', []))
    options = {'temperature': 0} if 'temperature' in supported else {}
    if reasoning_effort is not None:
        if 'reasoning' not in supported:
            raise ValueError('Requested model does not advertise reasoning controls')
        options['reasoning'] = {'effort': reasoning_effort}
    return options


async def run_one(session,http,model,pricing,mode,repeat,out,budget,max_turns,max_tokens,generation):
    known=(await session.list_tools()).tools
    permitted={'execute_blender_code','get_scene_info','get_object_info','get_viewport_screenshot'}
    if mode=='multiview': permitted.add('get_viewport_montage')
    tools=[tool_schema(t,mode) for t in known if t.name in permitted]
    assert any(t['function']['name']=='execute_blender_code' for t in tools)
    if mode=='multiview': assert any(t['function']['name']=='get_viewport_montage' for t in tools)
    run_dir=out/f'{repeat:02d}-{mode}'; run_dir.mkdir(parents=True,exist_ok=True)
    reset=await session.call_tool('execute_blender_code',{'code':RESET,
        'user_prompt':'Reset the disposable benchmark scene.'})
    if reset.isError or any(p.type=='text' and p.text.lower().startswith('error') for p in reset.content):
        raise RuntimeError('Scene reset failed; refusing a non-comparable run')
    messages=[{'role':'system','content':SYSTEM}]
    records=[]; status='running'; error=None; checks={}; start=time.perf_counter()
    try:
        for phase,task in enumerate(TASKS):
            messages.append({'role':'user','content':task})
            for turn in range(max_turns):
                clean,nimages=clean_for_estimate(messages)
                # Deliberately conservative reservation. Actual costs below always
                # come from provider usage, not this estimate. Scoped key is the hard cap.
                nbytes=len(json.dumps({'messages':clean,'tools':tools},ensure_ascii=False).encode())
                reserve=((nbytes+nimages*65536)*pricing['prompt']+max_tokens*pricing['completion']+
                         nimages*pricing.get('image',0))*1.1
                budget.reserve(reserve)
                payload={'model':model,'messages':messages,'tools':tools,'max_tokens':max_tokens,
                         **generation,'provider':{'require_parameters':True}}
                t0=time.perf_counter()
                try:
                    response=await http.post('/chat/completions',json=payload)
                    response.raise_for_status()
                    data=response.json()
                    if 'error' in data:
                        raise RuntimeError(str(data['error']))
                except Exception as exc:
                    budget.uncertain=True
                    records.append({'kind':'inference_error','phase':phase,'turn':turn,
                                    'elapsed_s':time.perf_counter()-t0,
                                    'error':type(exc).__name__,'usage':None,'actual_cost_usd':None})
                    raise
                elapsed=time.perf_counter()-t0
                usage=data.get('usage',{})
                cost=usage.get('cost')
                if cost is None and data.get('id'):
                    detail=await http.get('/generation',params={'id':data['id']})
                    if detail.status_code==200: cost=detail.json().get('data',{}).get('total_cost')
                # Save evidence before budget accounting, including a missing-cost response.
                message=data['choices'][0]['message']
                record={'kind':'inference','phase':phase,'turn':turn,'elapsed_s':elapsed,
                        'usage':usage,'actual_cost_usd':cost,'generation_id':data.get('id'),
                        'provider':data.get('provider'),'message':{k:v for k,v in message.items() if k in ('role','content','tool_calls')}}
                records.append(record)
                (run_dir/'events.json').write_text(json.dumps(records,indent=2))
                budget.charge(cost)
                assistant={'role':'assistant','content':message.get('content')}
                if message.get('tool_calls'): assistant['tool_calls']=message['tool_calls']
                # Preserve provider reasoning details where exposed (without publishing them).
                if message.get('reasoning_details'): assistant['reasoning_details']=message['reasoning_details']
                messages.append(assistant)
                calls=message.get('tool_calls') or []
                if not calls: break
                image_parts=[]
                for call in calls:
                    name=call['function']['name']
                    t1=time.perf_counter()
                    record={'kind':'tool','phase':phase,'turn':turn,'name':name,
                            'arguments':call['function']['arguments'], 'images':[],
                            'elapsed_s':0.0,'is_error':False,'text':''}
                    records.append(record)  # Count attempts, including malformed/failed calls.
                    try:
                        params=json.loads(call['function']['arguments'])
                        record['arguments']=params
                        if name not in permitted or (mode=='baseline' and 'inspect' in params):
                            record['is_error']=True
                            text='Error: this tool or argument is not available in this benchmark arm.'
                            result=None
                        else:
                            result=await session.call_tool(name,params)
                            text='\n'.join(p.text for p in result.content if p.type=='text')
                            record['is_error']=bool(result.isError) or text.lower().startswith('error')
                            for item in result.content:
                                if item.type!='image': continue
                                filename=f'image-{len(records):04d}-{len(record["images"]):02d}.png'
                                raw=base64.b64decode(item.data)
                                (run_dir/filename).write_bytes(raw)
                                record['images'].append({'path':filename,'sha256':hashlib.sha256(raw).hexdigest(),
                                                         'bytes':len(raw),'mime_type':item.mimeType})
                                image_parts.append({'type':'text','text':f'Image from {name}, tool call {call["id"]}:'})
                                image_parts.append({'type':'image_url','image_url':{'url':f'data:{item.mimeType};base64,{item.data}'}})
                    except json.JSONDecodeError:
                        text='Error: tool arguments are not valid JSON.'
                        record['is_error']=True
                    except Exception as exc:
                        record['is_error']=True
                        record['text']=f'{type(exc).__name__}: {exc}'
                        raise
                    finally:
                        record['elapsed_s']=time.perf_counter()-t1
                    record['text']=text
                    messages.append({'role':'tool','tool_call_id':call['id'],'content':text or '[image observation follows]'})
                if image_parts: messages.append({'role':'user','content':image_parts})
                (run_dir/'events.json').write_text(json.dumps(records,indent=2))
            else:
                raise RuntimeError(f'Phase {phase+1} hit the per-phase turn limit')
        status='completed'
    except Exception as exc:
        error=f'{type(exc).__name__}: {exc}'; status='stopped'
        # A failed HTTP request may still have billed. Never issue another paid call
        # in that case without reconciling it.
        if isinstance(exc,httpx.HTTPError): budget.uncertain=True
    wall=time.perf_counter()-start
    try:
        graded=await session.call_tool('execute_blender_code',{'code':GRADER,'user_prompt':'Score the completed benchmark fixture.'})
        text='\n'.join(x.text for x in graded.content if x.type=='text')
        pos=text.find('{'); checks=json.JSONDecoder().raw_decode(text[pos:])[0]
    except Exception as exc: checks={'grading_error':str(exc)}
    # Same final proof image in BOTH arms, outside the measured agent interval.
    # It is not sent to the agent and cannot improve the baseline's decisions.
    try:
        final=await session.call_tool('get_viewport_montage',{'user_prompt':'Save final benchmark evidence.'})
        for item in final.content:
            if item.type=='image':
                (run_dir/'final-inspection.png').write_bytes(base64.b64decode(item.data))
                break
    except Exception as exc:
        checks['final_evidence_error']=str(exc)
    inference=[r for r in records if r['kind']=='inference']
    tool_events=[r for r in records if r['kind']=='tool']
    failed_inference=[r for r in records if r['kind']=='inference_error']
    usage_complete=not failed_inference and all('prompt_tokens' in r['usage'] and 'completion_tokens' in r['usage'] for r in inference)
    summary={'mode':mode,'repeat':repeat,'model':model,'status':status,'error':error,
             'quality_checks':checks,'passed':bool(checks) and all(v is True for v in checks.values()),
             'wall_s':wall,'inference_calls':len(inference)+len(failed_inference),'tool_calls':len(tool_events),
             'input_tokens':sum(r['usage'].get('prompt_tokens',0) for r in inference) if usage_complete else None,
             'output_tokens':sum(r['usage'].get('completion_tokens',0) for r in inference) if usage_complete else None,
             'usage_complete':usage_complete,
             'cached_tokens':sum(r['usage'].get('prompt_tokens_details',{}).get('cached_tokens',0) or 0 for r in inference),
             'reasoning_tokens':sum(r['usage'].get('completion_tokens_details',{}).get('reasoning_tokens',0) or 0 for r in inference),
             'reported_cost_usd':sum(r['actual_cost_usd'] or 0 for r in inference),
             'response_caching': 'not explicitly enabled by this runner',
             'cost_complete':not budget.uncertain and not failed_inference and all(r['actual_cost_usd'] is not None for r in inference),
             'inference_s':sum(r['elapsed_s'] for r in inference+failed_inference),
             'tool_s':sum(r['elapsed_s'] for r in tool_events)}
    (run_dir/'summary.json').write_text(json.dumps(summary,indent=2))
    (run_dir/'events.json').write_text(json.dumps(records,indent=2))
    return summary


async def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--checkout',type=Path,required=True)
    ap.add_argument('--model',required=True)
    ap.add_argument('--port',type=int,default=19876)
    ap.add_argument('--out',type=Path,default=Path('results/agent'))
    ap.add_argument('--budget',type=float,default=4.5)
    ap.add_argument('--repeats',type=int,default=3)
    ap.add_argument('--max-turns',type=int,default=8)
    ap.add_argument('--max-tokens',type=int,default=1600)
    ap.add_argument('--reasoning-effort', choices=('low', 'medium', 'high'))
    args=ap.parse_args()
    key=os.environ.get('OPENROUTER_API_KEY')
    if not key: raise SystemExit('OPENROUTER_API_KEY is required; no paid calls made')
    if not 0<args.budget<=5: raise SystemExit('Budget must be positive and no greater than the authorized $5')
    if not 1<=args.max_turns<=12 or not 128<=args.max_tokens<=4096: raise SystemExit('Invalid turn/output-token limits')
    if not 1<=args.repeats<=10: raise SystemExit('repeats must be in 1..10')
    args.out.mkdir(parents=True,exist_ok=True)
    budget=Budget(args.budget)
    # Do not pass the OpenRouter secret into the MCP subprocess/Blender.
    env={k:v for k,v in os.environ.items() if k not in {'OPENROUTER_API_KEY','OPENAI_API_KEY','GH_TOKEN','GITHUB_TOKEN'}}
    env.update({'PYTHONPATH':str(args.checkout.resolve()/'src'),'DISABLE_TELEMETRY':'true',
                'BLENDER_HOST':'127.0.0.1','BLENDER_PORT':str(args.port)})
    params=StdioServerParameters(command=sys.executable,
        args=['-c','from blender_mcp.server import main; main()'],env=env)
    async with httpx.AsyncClient(base_url='https://openrouter.ai/api/v1',
             headers={'Authorization':f'Bearer {key}'},timeout=180) as http:
        response=await http.get('/models'); response.raise_for_status()
        catalog=response.json()['data']
        model=next((x for x in catalog if x['id']==args.model),None)
        if not model: raise SystemExit('Requested model not present in current OpenRouter catalog')
        if 'image' not in model.get('architecture',{}).get('input_modalities',[]):
            raise SystemExit('Model does not advertise image inputs')
        if 'tools' not in model.get('supported_parameters',[]):
            raise SystemExit('Model does not advertise tool calling')
        generation = generation_options(model, args.reasoning_effort)
        pricing={k:float(v) for k,v in model.get('pricing',{}).items() if k in ('prompt','completion','image')}
        if any(pricing.get(k,-1)<0 for k in ('prompt','completion')): raise SystemExit('Unknown prices')
        (args.out/'configuration.json').write_text(json.dumps({'model':model,'budget_usd':args.budget,
            'tasks':TASKS,'repeats':args.repeats,'max_turns':args.max_turns,
            'max_tokens':args.max_tokens,'generation_parameters':generation,
            'note':'Provider-reported totals; cached input is not treated as free.'},indent=2))
        summaries=[]
        async with stdio_client(params) as (reader,writer):
            async with ClientSession(reader,writer) as session:
                await session.initialize()
                for repeat in range(args.repeats):
                    order=('baseline','multiview') if repeat%2==0 else ('multiview','baseline')
                    for mode in order:
                        summary=await run_one(session,http,args.model,pricing,mode,repeat,args.out,budget,args.max_turns,args.max_tokens,generation)
                        summaries.append(summary)
                        (args.out/'summary.json').write_text(json.dumps({'kind':'live_vision_agent_benchmark',
                             'runs':summaries,'spent_usd':budget.spent,'budget_usd':args.budget},indent=2))
                        print(json.dumps(summary),flush=True)
                        if budget.uncertain or summary['status']!='completed':
                            raise SystemExit('Benchmark stopped; inspect recorded evidence before continuing')

if __name__=='__main__': asyncio.run(main())
