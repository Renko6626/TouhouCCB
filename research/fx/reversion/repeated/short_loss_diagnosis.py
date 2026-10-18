"""Attribute saved short-episode outcomes without changing or rerunning strategies."""
import csv
import hashlib
import json
from collections import defaultdict
from decimal import Decimal as D
from pathlib import Path

import numpy as np

from backend.app.services.fx.amm import quote_buy_exact_out, quote_sell
from research.fx.factor_activity.model import components
from .population import OUTPUT


def bucket(rows):
    return dict(count=len(rows),pnl=sum(float(e['realized_profit']) for e in rows),
        positive_fraction=float(np.mean([float(e['realized_profit'])>0 for e in rows])) if rows else None,
        mean_trade_return=float(np.mean([float(e['realized_profit'])/float(e['entry_gold']) for e in rows])) if rows else None)


def main():
    manifest_path=OUTPUT/'manifest.json';manifest=json.loads(manifest_path.read_text())
    inputs=[manifest_path,Path(__file__),Path('research/fx/common/clock.py'),Path('research/fx/factor_activity/model.py'),
            Path('research/fx/episodes/model.py'),Path('backend/app/services/fx/amm.py')]
    inputs += [OUTPUT/f'n{n}'/f'{case}{suffix}' for n in [40,80] for case in ['control','shock']
               for suffix in ['.npz','-episodes.json','-fills.jsonl']]
    inputs += [OUTPUT/f'n{n}'/'summary.json' for n in [40,80]]
    hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    results={};annotations=[]
    seed=manifest['base_spec']['market_seed']
    for n in [40,80]:
        cfg=manifest['cases'][str(n)]['config'];profiles=manifest['cases'][str(n)]['profiles']
        noises={i:np.random.default_rng(seed*1000003+i*101+2).normal(size=cfg['ticks']) for i in range(3*n//4,n)}
        summaries=json.loads((OUTPUT/f'n{n}'/'summary.json').read_text())
        for case in ['control','shock']:
            with np.load(OUTPUT/f'n{n}'/f'{case}.npz') as saved:x={k:saved[k] for k in saved.files}
            episodes=json.loads((OUTPUT/f'n{n}'/f'{case}-episodes.json').read_text())
            fills=[json.loads(line) for line in (OUTPUT/f'n{n}'/f'{case}-fills.jsonl').read_text().splitlines()]
            done=[e for e in episodes if e['close_tick'] is not None]
            logs=np.log(x['prices']);classes=defaultdict(list);reverse_pnl=[];mismatch=0
            for e in episodes:
                i=int(e['account']);t=e['entry_tick'];p=profiles[i]
                past=logs[max(0,t-max(cfg['trend_window'],cfg['mean_window'])):t]
                trend,mean,_=components(past,logs[t-1],0,1,1,cfg)
                terms=np.array([p['trend']*trend,p['reversion']*mean,p['sigma']*noises[i][t-1]])
                assert np.sign(terms.sum())==e['direction'] and abs(terms.sum())+1e-15>=p['sigma']
                dominant=['trend','reversion','noise'][np.argmax(abs(terms))]
                if e['close_tick'] is not None:
                    classes[dominant].append(e)
                    mismatch+=int(np.sign(terms[np.argmax(abs(terms))])!=e['direction'])
                    annotations.append(dict(accounts=n,case=case,episode=e['id'],account=i,
                        entry_tick=t,close_tick=e['close_tick'],direction=e['direction'],
                        trend_term=float(terms[0]),reversion_term=float(terms[1]),noise_term=float(terms[2]),
                        dominant_term=dominant,exit_reason=e['closing_reason'],
                        pnl=e['realized_profit'],trade_return=float(e['realized_profit'])/float(e['entry_gold'])))
            for fill in fills:
                assert D(fill['fee'])==0
                if fill['action']!='open':continue
                after=fill['pool_after'];q=D(fill['quantity']);gold=abs(D(fill['gold_change']))
                if fill['direction']==1:
                    pnl=quote_sell(q,D(after['gold']),D(after['foreign']),D('0')).output_amount-gold
                else:
                    pnl=gold-quote_buy_exact_out(q,D(after['gold']),D(after['foreign']),D('0')).input_amount
                reverse_pnl.append(float(pnl))
            exits={reason:bucket([e for e in done if e['closing_reason']==reason]) for reason in ['take_profit','stop_loss','trailing']}
            total=bucket(done)
            assert abs(total['pnl']-float(summaries[case]['behavior']['realized_pnl']))<1e-7
            assert abs(sum(bucket(es)['pnl'] for es in classes.values())-total['pnl'])<1e-7
            event_ticks=[int(t) for t in manifest['schedule']] if case=='shock' else []
            carried=[e for e in done if any(e['entry_tick']<=t<e['close_tick'] for t in event_ticks)]
            not_carried=[e for e in done if not any(e['entry_tick']<=t<e['close_tick'] for t in event_ticks)]
            results[f'n{n}_{case}']=dict(total=total,dominant={name:bucket(classes[name]) for name in ['trend','reversion','noise']},
                exits=exits,dominant_component_opposes_final_direction=mismatch,
                carried_over_issue=bucket(carried),not_carried_over_issue=bucket(not_carried),
                trailing_negative_count=sum(float(e['realized_profit'])<0 for e in done if e['closing_reason']=='trailing'),
                immediate_reverse_pnl_sum=sum(reverse_pnl),immediate_reverse_pnl_min=min(reverse_pnl),
                immediate_reverse_pnl_max=max(reverse_pnl),
                mean_short_check_interval_minutes=cfg['ticks']/6/(x['factor_arrivals'].sum()/(n/4)))
    with (OUTPUT/'short_loss_trades.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=list(annotations[0]));writer.writeheader();writer.writerows(annotations)
    (OUTPUT/'short_loss.json').write_text(json.dumps(dict(cases=results,sources_sha256=hashes,
        method='Reconstruct every entry signal using recorded previous closes, frozen personal coefficients and independent per-account normal stream (one draw per short account per tick). Dominant means largest absolute component, not a strategy ablation. Completed episodes only for PnL tables; all opened episodes for immediate reversal quote. Carried-over-event and entry-type splits are observational, not causal effects.'),indent=2,allow_nan=False)+'\n')
    lines=['# 短线亏损拆分：40人与80人','','仅分析已保存账本，未重跑、调参或修改策略。所有收益表是短线组全部已完成轮次的合计金圆券盈亏，未完成轮次不混入；完整钱包净值另见wallet_report.md。','','## 入场信号贡献最大的项','','|入场绝对贡献最大项|40人无冲击|40人连续冲击|80人无冲击|80人连续冲击|','|---|---:|---:|---:|---:|']
    keys=['n40_control','n40_shock','n80_control','n80_shock']
    for name,label in [('trend','趋势'),('reversion','均值回归'),('noise','随机项')]:
        lines.append('|'+label+'|'+'|'.join(f"{results[k]['dominant'][name]['pnl']:+.2f}（{results[k]['dominant'][name]['count']}轮）" for k in keys)+'|')
    lines+=['','每次开仓都使用当时已知历史价格、个人参数和原账户随机流重建，校验方向和入场阈值一致。最大项归类不等于只用该项，三个分量仍然共同决定方向；少数轮次最大项与合计信号方向相反，计数保留在JSON。归类不是关掉某项后的反事实消融。','','## 连续冲击下的退出','','|退出原因|40人：轮数／平均每轮盈亏|80人：轮数／平均每轮盈亏|','|---|---:|---:|']
    for name,label in [('take_profit','止盈'),('stop_loss','止损'),('trailing','移动回撤退出')]:
        vals=[]
        for n in [40,80]:
            e=results[f'n{n}_shock']['exits'][name];vals.append(f"{e['count']}／{e['pnl']/e['count']:+.2f}")
        lines.append('|'+label+'|'+'|'.join(vals)+'|')
    for n in [40,80]:
        d=results[f'n{n}_shock']
        lines.append(f"\n{n}人短线总体盈利轮次占比{d['total']['positive_fraction']:.1%}，已实现合计{d['total']['pnl']:+.2f}。移动回撤退出中{d['trailing_negative_count']}/{d['exits']['trailing']['count']}轮最终亏损。止损轮次实际平均收益率{d['exits']['stop_loss']['mean_trade_return']:.2%}，止盈轮次{d['exits']['take_profit']['mean_trade_return']:+.2%}。")
    lines+=['','移动回撤规则为盈利至少.25%后，从已观察到的峰值回落.25个百分点退出，不是保证锁定正利润。检查仅发生在随机到达时；价格可能越过回撤线或±.5%止盈止损线。越过止损也可能来自即时跳价，不能全部归因于检查间隔。常态下每位短线账户的实际平均查看间隔约4.05/4.39分钟；冲击路径全程约3.12/3.82分钟。','','## 冲击及成交成本核对','','40人：跨越事件的54轮合计−793.42，其余1005轮−1188.47。80人：跨事件121轮合计−181.42，其余1839轮−3177.91。没有跨事件的交易仍可能受之前事件影响，因此这不是冲击的因果贡献；真正无冲击对照本身也亏损。','','账本手续费逐笔为0。按每次真实开仓后的池储备，立即对同一数量做逆向生产报价，每轮仅损失约0.000001～0.000002金圆券的保守舍入。恒定乘积零费用下，在没有中间交易或发行时原地逆向基本回本，不能把亏损简单说成固定AMM往返滑点成本；持有期间其他成交和价格变化仍然影响真实退出价。','','## 解释边界','','本路径中的趋势项最大开仓亏损显著、均值回归项最大开仓盈利，与更强的短滞后反转及库存组的反向调仓一致。支持“趋势入场与当前强回归环境不匹配”的解释，但没有通过删除趋势项、调整退出规则或排除他人交易的对照证明单一因果机制。止盈止损只负责规定退出，没有为入场创造盈利优势。','','初始1000外币的贬值解释了完整钱包净值损失中的一大块：40人短线钱包约−6.97%，同路径持有初始钱包约−4.99%；80人约−7.09%，持有基准约−5.34%。相对差额仍含未实现盈亏，不能直接等同于上面的已完成轮次利润。','','[逐轮信号归类与盈亏](short_loss_trades.csv)、[完整分解与来源哈希](short_loss.json)。复现：`python -m research.fx.reversion.repeated.short_loss_diagnosis`。所有入场方向/阈值、盈亏总和与账本对齐、手续费和即时逆向报价已核对；这是单种子诊断，未新增永久测试。','']
    (OUTPUT/'short_loss.md').write_text('\n'.join(lines))
    for p in inputs:assert hashlib.sha256(p.read_bytes()).hexdigest()==hashes[str(p)]
    print(f'Verified {len(annotations)} completed episodes, every entry signal, PnL reconciliations and zero-fee immediate reversals; original files unchanged.')


if __name__=='__main__':main()
