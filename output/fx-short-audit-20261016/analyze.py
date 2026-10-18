"""Audit actual short-agent entries and exits on unchanged sigma108 traces.

Exact replay captures decision records already returned by the simulator.
Noise-zero entry checks are local diagnostics, not alternative-market runs.
Run: python output/fx-short-audit-20261016/analyze.py
"""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import csv
import hashlib
import json
from collections import Counter
import numpy as np
from scipy.stats import norm
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from research.fx.reversion.repeated.entry_model import simulate

OUT=Path(__file__).resolve().parent
SOURCE=ROOT/'output/fx-shallow-calibration-20261016'
SEEDS=[20261029,20261030,20261031]
ACTORS=list(range(15,20))


def describe(values):
    a=np.array(values,dtype=float)
    if not len(a):return dict(n=0,median=None,p90=None,mean=None,max=None)
    return dict(n=len(a),median=float(np.median(a)),p90=float(np.quantile(a,.9)),mean=float(a.mean()),max=float(a.max()))


def summarize(entries,episodes,fills):
    opens={e['episode_id']:e for e in entries}
    selected=[e for e in episodes if e['id'] in opens]
    done=[e for e in selected if e['close_tick'] is not None]
    pending=[e for e in selected if e['close_tick'] is None]
    durations=[(e['close_tick']-e['entry_tick'])/12 for e in done]
    entry_classes=Counter(e['noise_zero_class'] for e in entries)
    dominant=Counter(e['dominant'] for e in entries)
    reason=Counter(e['closing_reason'] for e in done)
    entry_fills={f['episode_id']:f for f in fills if f['action']=='open'}
    closed_ids={e['id'] for e in done}
    exits=[f for f in fills if f['action']=='close' and f['episode_id'] in closed_ids]
    assert len(exits)==len(done), 'This audit expects a single full-quantity exit per completed episode.'
    actual_returns=[float(e['realized_profit'])/abs(float(entry_fills[e['id']]['gold_change'])) for e in done]
    pnl=sum(float(e['realized_profit']) for e in done)
    n=len(entries)
    return dict(entries=n,completed=len(done),unfinished=len(pending),
        entry_classes=dict(entry_classes),dominant_terms=dict(dominant),exit_reasons=dict(reason),
        noise_needed_fraction=entry_classes['noise_needed_for_threshold']/n if n else None,
        direction_flipped_from_nonzero_deterministic_fraction=float(np.mean([e['opposes_deterministic_direction'] for e in entries])) if n else None,
        deterministic_support_fraction=entry_classes['same_direction_without_noise']/n if n else None,
        noise_largest_fraction=dominant['noise']/n if n else None,
        holding_minutes=describe(durations),holding_over30_fraction=float(np.mean(np.array(durations)>30)) if durations else None,
        holding_over60_fraction=float(np.mean(np.array(durations)>60)) if durations else None,
        unfinished_age_minutes=describe([(10000-e['entry_tick'])/12 for e in pending]),
        realized_pnl=pnl,win_fraction=float(np.mean([float(e['realized_profit'])>0 for e in done])) if done else None,
        realized_return=describe(actual_returns),
        by_exit_reason={r:dict(count=sum(e['closing_reason']==r for e in done),
            mean_execution_return=float(np.mean([f['mark_return'] for f in exits if f['reason']==r])) if any(f['reason']==r for f in exits) else None)
            for r in ['take_profit','stop_loss','trailing']},
        trailing_loss_count=sum(f['reason']=='trailing' and float(f['realized_profit'])<0 for f in exits))


def main():
    candidate=SOURCE/'candidate-a1.8_t2.json';base=json.loads(candidate.read_text())
    assert base['parameters']['order_sigma_foreign']==108 and base['tick_seconds']==5
    sources=[candidate,ROOT/'research/fx/reversion/repeated/entry_model.py',ROOT/'research/fx/episodes/model.py']
    protected={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    all_entries=[];all_episodes=[];all_fills=[];seed_metrics={};entry_rows=[];audits={}
    for seed in SEEDS:
        cfg={**base['parameters'],'seeds':[seed]};folder=SOURCE/'a1.8_t2'/f'seed{seed}'
        saved=json.loads((folder/'parameters.json').read_text())
        assert saved==dict(config=cfg,profiles=base['profiles'],activity_b=base['activity_b'],trend_multiplier=base['trend_multiplier'])
        protected[str((folder/'trace.npz').relative_to(ROOT))]=hashlib.sha256((folder/'trace.npz').read_bytes()).hexdigest()
        print('Exact replay seed',seed,flush=True)
        x,s,episodes,fills,inventory,decisions=simulate(cfg,base['profiles'],5,base['activity_b'],seed,{},'weak_trend',
            {'trend_multiplier':base['trend_multiplier']})
        with np.load(folder/'trace.npz') as z:
            assert set(z.files)==set(x)
            for key in z.files:np.testing.assert_array_equal(x[key],z[key],err_msg=key)
        assert episodes==json.loads((folder/'trace-episodes.json').read_text())
        prior_fills=[json.loads(line) for line in (folder/'trace-fills.jsonl').read_text().splitlines()]
        assert fills==prior_fills
        assert len([d for d in decisions if d['action']=='open'])==len(episodes)
        open_map={(e['account'],e['entry_tick']):e for e in episodes}
        analyzed=[]
        for d in decisions:
            if d['action']!='open':continue
            trend,reversion,noise=d['terms'];det=trend+reversion;signal=det+noise;threshold=d['sigma']
            np.testing.assert_allclose(signal,d['signal'],rtol=1e-13,atol=1e-16)
            episode=open_map[(str(d['account']),d['tick'])]
            direction=episode['direction']
            assert abs(signal)>=threshold and np.sign(signal)==direction
            assert d['episode_id']==episode['id']
            if abs(det)>=threshold:
                cls='same_direction_without_noise' if np.sign(det)==direction else 'noise_reversed_qualified_direction'
            else:cls='noise_needed_for_threshold'
            row=dict(seed=seed,account=d['account'],tick=d['tick'],episode_id=f"{seed}/{d['episode_id']}",
                direction=direction,trend=trend,reversion=reversion,noise=noise,sigma=threshold,
                deterministic_signal=det,total_signal=signal,dominant=d['dominant'],noise_zero_class=cls,
                opposes_deterministic_direction=bool(det!=0 and np.sign(det)!=direction),
                observation='Decision uses closes through tick-1; execution occurs at recorded event index within tick.')
            analyzed.append(row)
            if d['tick']>=360:entry_rows.append(row)
        # Economic behavior checks: each actor has at most one episode, closes
        # restore the account's initial foreign inventory, no signal-based adds.
        for actor in ACTORS:
            es=sorted([e for e in episodes if int(e['account'])==actor],key=lambda e:e['entry_tick'])
            for before,after in zip(es[:-1],es[1:]):
                assert before['close_tick'] is not None and before['close_tick']<after['entry_tick']
            for f in [f for f in fills if f['account']==actor and f['action']=='close']:
                assert f['complete'] and float(f['after_foreign'])==1000
                rate=f['mark_return'];peak=f['peak_return']
                if f['reason']=='take_profit':assert rate>=cfg['take_profit']
                elif f['reason']=='stop_loss':assert rate<=-cfg['stop_loss']
                elif f['reason']=='trailing':assert peak>=cfg['trail_arm'] and peak-rate>=cfg['trail_drawdown']
                else:raise AssertionError(f['reason'])
        primary=[v for v in analyzed if v['tick']>=360]
        renamed_episodes=[{**e,'id':f"{seed}/{e['id']}"} for e in episodes]
        renamed_fills=[{**f,'episode_id':f"{seed}/{f['episode_id']}"} for f in fills]
        seed_metrics[str(seed)]=summarize(primary,renamed_episodes,renamed_fills)
        all_entries.extend(primary);all_episodes.extend(renamed_episodes);all_fills.extend(renamed_fills)
        (OUT/f'decisions-seed{seed}.json').write_text(json.dumps(decisions,indent=2)+'\n')
        audits[str(seed)]=dict(all_trace_fields_bitwise_equal=True,episodes_and_fills_exactly_equal=True,
            entries_joined_to_episodes=True,no_overlapping_episodes=True,full_exits_restore_inventory=True,
            observed_exit_thresholds_verified=True)
    overall=summarize(all_entries,all_episodes,all_fills)
    by_actor={str(a):summarize([r for r in all_entries if r['account']==a],
        [e for e in all_episodes if int(e['account'])==a],[f for f in all_fills if f['account']==a]) for a in ACTORS}
    result=dict(overall=overall,by_actor=by_actor,by_seed=seed_metrics,
        theoretical_noise_only_entry_probability=float(2*norm.sf(1)),
        scope='Actual entries at tick>=360, five short accounts, three sigma108 seeds. Completed durations exclude unfinished episodes.',
        noise_zero_test='At each actual entry, set only that personal noise term to zero, holding observed historical terms and threshold fixed. Not a full counterfactual path.')
    (OUT/'metrics.json').write_text(json.dumps(result,indent=2)+'\n')
    with (OUT/'entries.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=entry_rows[0]);writer.writeheader();writer.writerows(entry_rows)
    rows=[dict(account=int(a),entries=m['entries'],completed=m['completed'],unfinished=m['unfinished'],
        dominant_trend=m['dominant_terms'].get('trend',0)/m['entries'],dominant_reversion=m['dominant_terms'].get('reversion',0)/m['entries'],
        dominant_noise=m['noise_largest_fraction'],deterministic_support=m['deterministic_support_fraction'],
        noise_needed=m['noise_needed_fraction'],opposes_deterministic=m['direction_flipped_from_nonzero_deterministic_fraction'],
        holding_median=m['holding_minutes']['median'],holding_p90=m['holding_minutes']['p90'],
        win_fraction=m['win_fraction'],realized_pnl_per_seed=m['realized_pnl']/3) for a,m in by_actor.items()]
    with (OUT/'accounts.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=rows[0]);writer.writeheader();writer.writerows(rows)
    (OUT/'manifest.json').write_text(json.dumps(dict(seeds=SEEDS,random_order_sigma=108,
        candidate=str(candidate.relative_to(ROOT)),primary_entry_start_tick=360,source_sha256=protected,
        caveats=['Only three existing research paths, not new validation.','Noise-zero diagnosis is entry-local, without rerunning the economic path.',
        'Largest absolute term alone is not a causal contribution.','No forced holding timeout; unfinished ages reported separately.',
        'Zero fees, no players or external shocks.']),indent=2)+'\n')
    plot(by_actor)
    for path,digest in protected.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    audits['all_sources_and_original_traces_unchanged']=True
    (OUT/'audit.json').write_text(json.dumps(audits,indent=2)+'\n')
    print(json.dumps(dict(overall=overall,accounts=rows),indent=2),flush=True)


def plot(by_actor):
    plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False,'font.size':10})
    fig,axes=plt.subplots(2,2,figsize=(14,9),layout='constrained')
    colors=['#176997','#b27a16','#a14870'];ys=np.arange(5)
    for ax,keys,labels,field in [
        (axes[0,0],['trend','reversion','noise'],['趋势项最大','均值回归项最大','个人噪声项最大'],'dominant_terms'),
        (axes[0,1],['same_direction_without_noise','noise_needed_for_threshold','noise_reversed_qualified_direction'],
            ['去噪仍同向入场','需噪声才跨门槛','噪声翻转已达门槛方向'],'entry_classes')]:
        left=np.zeros(5)
        for key,label,color in zip(keys,labels,colors):
            values=np.array([by_actor[str(a)][field].get(key,0)/by_actor[str(a)]['entries']*100 for a in ACTORS])
            ax.barh(ys,values,left=left,label=label,color=color);left+=values
        ax.set(yticks=ys,yticklabels=[f"{a}号（{by_actor[str(a)]['entries']}次入场）" for a in ACTORS],xlabel='实际入场占比（%）',xlim=(0,100))
        ax.invert_yaxis();ax.legend(fontsize=8,loc='lower left',bbox_to_anchor=(0,1.01),ncol=1)
    axes[0,0].set_title('入场时最大信号项',pad=65);axes[0,1].set_title('同一入场时刻，将个人噪声置零',pad=65)
    median=[by_actor[str(a)]['holding_minutes']['median'] for a in ACTORS]
    p90=[by_actor[str(a)]['holding_minutes']['p90'] for a in ACTORS]
    axes[1,0].plot(ACTORS,median,marker='o',color='#176997',label='已完成轮次中位数')
    axes[1,0].plot(ACTORS,p90,marker='s',color='#b27a16',label='已完成轮次P90')
    axes[1,0].set(title='实际持仓时长：未完成轮次另行报告',xlabel='短线账户编号',ylabel='分钟',xticks=ACTORS,ylim=(0,None));axes[1,0].legend(fontsize=8)
    pnl=[by_actor[str(a)]['realized_pnl']/3 for a in ACTORS]
    axes[1,1].bar(ACTORS,pnl,color=['#176997' if v>=0 else '#a14870' for v in pnl])
    axes[1,1].axhline(0,color='#444',lw=.8)
    axes[1,1].set(title='每种子平均已完成轮次盈亏',xlabel='短线账户编号',ylabel='金圆券',xticks=ACTORS)
    for ax in axes.flat:ax.grid(axis='x' if ax in axes[0] else 'y',alpha=.15);ax.set_axisbelow(True)
    fig.suptitle('浅池慢候选短线审计：纯随机订单σ＝108保持不变，三个原种子精确重放',fontsize=14)
    fig.savefig(OUT/'short-agents.png',dpi=150);plt.close(fig)


if __name__=='__main__':main()
