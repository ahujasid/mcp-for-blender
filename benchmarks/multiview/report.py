#!/usr/bin/env python3
"""Graphs from ACTUAL benchmark JSON only. Never fills missing runs with estimates."""
import argparse
import csv
import json
from pathlib import Path
import statistics

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

METRICS={
    'input_tokens':('Input tokens (provider-reported; text + images)','tokens'),
    'output_tokens':('Completion tokens (includes any counted reasoning)','tokens'),
    'reported_cost_usd':('Provider-reported charge','USD'),
    'wall_s':('Agent wall time (grading excluded)','seconds'),
    'inference_calls':('Model requests','requests'),
    'tool_calls':('Agent tool calls','calls'),
}


def generate(source,out):
    data=json.loads(source.read_text(encoding="utf-8"))
    if data.get('kind')!='live_vision_agent_benchmark' or not data.get('runs'):
        raise ValueError('No live benchmark evidence; refusing to generate synthetic performance graphs')
    runs=data['runs']; out.mkdir(parents=True,exist_ok=True)
    fields=['repeat','mode','model','status','passed',*METRICS,'cached_tokens','reasoning_tokens','usage_complete','cost_complete']
    with (out/'metrics.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore',lineterminator='\n'); writer.writeheader(); writer.writerows(runs)
    for metric,(title,unit) in METRICS.items():
        fig,ax=plt.subplots(figsize=(7.6,4.8))
        paired={}
        for run in runs:
            val=run.get(metric)
            if val is None: continue
            if metric=='reported_cost_usd' and not run.get('cost_complete'):
                continue  # Unknown total is missing, not zero.
            x=0 if run['mode']=='baseline' else 1
            passed=run['status']=='completed' and run['passed']
            ax.scatter(x,val,marker='o' if passed else 'x',s=60)
            ax.annotate(f"r{run['repeat']+1}"+('' if passed else ' incomplete/fail'),(x,val),xytext=(7,3),textcoords='offset points',fontsize=8)
            paired.setdefault(run['repeat'],{})[x]=val
        for pair in paired.values():
            if len(pair)==2: ax.plot([0,1],[pair[0],pair[1]],linestyle=':',alpha=.45)
        ax.set_xticks([0,1],['Single-view tools','Multi-view feedback'])
        ax.set_xlim(-.4,1.55); ax.set_ylim(bottom=0)
        ax.set_ylabel(unit); ax.set_title(title); ax.grid(axis='y',alpha=.2)
        fig.text(.1,.015,'Each dot is one run; dotted lines connect paired repeats. Failed/incomplete attempts remain visible.',fontsize=8)
        fig.tight_layout(rect=(0,.04,1,1)); fig.savefig(out/f'{metric}.png',dpi=160); plt.close(fig)
    lines=['# Live Blender MCP vision-agent benchmark','',
           'These are actual provider usage and wall-clock measurements from the included runner. '
           'All attempts are listed. Unknown cost totals are omitted from the charge plot, not plotted as zero. Input tokens include text and images; reasoning tokens are not added '
           'again to completion totals. This small task does not establish performance on complex scenes.','',
           '| Repeat | Arm | Completed | Geometry checks passed | Input | Output | USD | Wall seconds | Model calls | Tool calls |',
           '|---:|---|---|---|---:|---:|---:|---:|---:|---:|']
    for r in runs:
        lines.append(f"| {r['repeat']+1} | {r['mode']} | {r['status']} | {r['passed']} | {r.get('input_tokens')} | {r.get('output_tokens')} | {format(r['reported_cost_usd'], '.5f') if r.get('cost_complete') else 'unknown'} | {r['wall_s']:.2f} | {r['inference_calls']} | {r['tool_calls']} |")
    pairs=[]
    for repeat in sorted({r['repeat'] for r in runs}):
        arms={r['mode']:r for r in runs if r['repeat']==repeat}
        if set(arms)=={'baseline','multiview'} and all(r['status']=='completed' and r['passed'] and r.get('usage_complete') and r.get('cost_complete') for r in arms.values()): pairs.append(arms)
    lines+=['',f'{len(pairs)} complete, geometry-passing paired repeats are available. '
            'The automated grader checks final object dimensions/positions and brace removal, not artistic quality. '
            'Final inspection images are saved separately for visual review.']
    if pairs:
        lines+=['','## Matched-pair descriptive differences','']
        for metric,(title,_) in METRICS.items():
            diffs=[100*(1-p['multiview'][metric]/p['baseline'][metric]) for p in pairs if p['baseline'][metric]]
            if diffs: lines.append(f'{title}: median paired reduction {statistics.median(diffs):.1f}%, range {min(diffs):.1f}% to {max(diffs):.1f}%. Negative means multiview used more.')
    lines+=['','## Graphs','']
    for metric,(title,_) in METRICS.items(): lines += [f'![{title}]({metric}.png)','']
    (out/'README.md').write_text('\n'.join(lines))
    return runs

if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('source',type=Path); ap.add_argument('out',type=Path)
    a=ap.parse_args(); generate(a.source,a.out)
