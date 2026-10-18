"""Small, predeclared comparison; run from repo root with python -m ...run."""
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from research.fx.reversion.repeated.population_time_metrics import metrics
from .policies import POLICY, PRESETS, simulate


ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).parent
OUT = HERE / 'results'
FROZEN = ROOT / 'research/fx/reversion/repeated/FROZEN_CONFIG.json'
VARIANTS = ['baseline', 'robust_inventory', 'belief_mix']
LABELS = dict(baseline='原库存', robust_inventory='中位数库存', belief_mix='信念混编')


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(x, summary, episodes, fills, records, cfg):
    for key, values in x.items():
        assert np.isfinite(values).all(), key
    assert x['account_gold'].min() >= 0 and x['account_foreign'].min() >= 0
    np.testing.assert_allclose(x['gold'] + x['account_gold'].sum(axis=1),
        cfg['pool_gold'] + cfg['accounts'] * cfg['initial_gold'] + np.cumsum(x['injection_gold']), atol=1e-8, rtol=0)
    np.testing.assert_allclose(x['pool_foreign'] + x['account_foreign'].sum(axis=1),
        cfg['pool_foreign'] + cfg['accounts'] * cfg['initial_foreign'] + np.cumsum(x['injection_foreign']), atol=1e-8, rtol=0)
    np.testing.assert_allclose(x['account_wealth'],
        x['account_gold'] + x['account_foreign'] * x['prices'][:, None], atol=1e-8, rtol=0)
    np.testing.assert_array_equal(x['account_trades'].sum(axis=1), x['trades'])
    assert all(r['observed_through_tick'] < r['tick'] for r in records)
    assert all(abs(float(r['target'])) <= cfg['inventory_position_cap'] + 1e-9 for r in records)
    done = [e for e in episodes if e['close_tick'] is not None]
    assert abs(sum(float(e['realized_profit']) for e in done) - float(summary['behavior']['realized_pnl'])) < 1e-7
    # Conservation above includes every trader; candidate records additionally
    # reconcile each fill's observable wallet change.
    for r in records:
        assert abs(float(r['after_foreign']) - float(r['before_foreign']) - r['actual_flow']) < 1e-9
        assert abs(float(r['after_gold']) - float(r['before_gold']) - float(r['gold_change'])) < 1e-9
    assert all(int(f['account']) >= 15 for f in fills)
    return dict(completed=len(done), no_future_policy_inputs=True,
                two_assets_conserved=True, wallet_marks_checked=True)


def run():
    frozen = json.loads(FROZEN.read_text())
    cfg = frozen['research_reproduction']['selected_simulator_config']
    schedule = {int(t): v for t, v in frozen['research_reproduction']['synthetic_shock_schedule'].items()}
    seeds = [81, 82, 83]
    protected = {str(p.relative_to(ROOT)): digest(p)
        for p in (ROOT / 'research/fx').rglob('*')
        if p.is_file() and '__pycache__' not in p.parts and HERE not in p.parents}
    OUT.mkdir(exist_ok=True)
    manifest = dict(kind='exploratory_not_selected', seeds=seeds, variants=VARIANTS,
                    config=cfg, frozen_name=frozen['name'], frozen_sha256=digest(FROZEN),
                    activity_b=frozen['activity_b'], schedule=schedule,
                    policy=POLICY, belief_presets=PRESETS,
                    source_sha256={str(p.relative_to(ROOT)): digest(p)
                                   for p in [HERE/'policies.py', HERE/'run.py']},
                    protected_sha256=protected)
    dump(OUT / 'manifest.json', manifest)
    rows = []
    audits = []
    for variant in VARIANTS:
        for seed in seeds:
            normal = None
            for scenario in ['normal', 'shock']:
                result, records = simulate(variant, cfg, frozen, seed, schedule if scenario == 'shock' else {})
                x, summary, episodes, fills, inventory, decisions = result
                audit = check(x, summary, episodes, fills, records, cfg)
                if scenario == 'normal':
                    normal = {k: v[:1000].copy() for k, v in x.items()}
                else:
                    for key in x:
                        np.testing.assert_array_equal(normal[key], x[key][:1000], err_msg=key)
                if variant == 'baseline':
                    prior = ROOT / f'research/fx/reversion/repeated/results/short-exit-tuning/profit_trail04-{scenario}-seed{seed}/trace.npz'
                    with np.load(prior) as previous:
                        for key in previous.files:
                            np.testing.assert_array_equal(x[key], previous[key], err_msg=key)
                    audit['frozen_trace_exact_match'] = True
                directory = OUT / f'{variant}-{scenario}-{seed}'
                directory.mkdir(exist_ok=True)
                np.savez_compressed(directory/'trace.npz', **x)
                dump(directory/'summary.json', summary)
                dump(directory/'episodes.json', episodes)
                for name, records_out in [('inventory', inventory), ('short-fills', fills)]:
                    (directory/f'{name}.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in records_out))
                d = metrics(x, summary, episodes, frozen['tick_seconds'])
                hold = cfg['initial_gold'] + cfg['initial_foreign'] * x['prices'][-1]
                d.update(variant=variant, scenario=scenario, seed=seed,
                    trades_per_minute=d['trades']/(d['hours']*60),
                    inventory_trades_per_minute=float(x['reversion_trades'].sum()/(d['hours']*60)),
                    inventory_vs_hold=float(x['account_wealth'][-1, 10:15].mean()-hold),
                    inventory_max_abs_position=float(np.max(abs(x['account_foreign'][:, 10:15]-cfg['initial_foreign']))),
                    inventory_final_position=(x['account_foreign'][-1, 10:15]-cfg['initial_foreign']).tolist(),
                    inventory_vs_hold_individual=(x['account_wealth'][-1, 10:15]-hold).tolist(),
                    final_price=float(x['prices'][-1]),
                    data=str(directory.relative_to(ROOT)))
                rows.append(d)
                audits.append(dict(variant=variant, scenario=scenario, seed=seed, **audit))
                dump(OUT/'metrics.json', rows)
                print(f'{variant} {scenario} seed={seed}: range30={d["range30"]["median"]:.2%}, '
                      f'inventory excess={d["inventory_vs_hold"]:+.2f}, trades/min={d["trades_per_minute"]:.2f}', flush=True)
    for path, expected in protected.items():
        assert digest(ROOT/path) == expected, path
    dump(OUT/'audit.json', dict(paths=len(rows), path_checks=audits,
        protected_files_unchanged=len(protected), paired_first_shock_prefix_exact=True))
    report(rows)


def report(rows):
    plt.rcParams.update({'font.family': 'Noto Sans CJK SC', 'axes.unicode_minus': False})
    fig, axes = plt.subplots(1, 3, figsize=(14, 4))
    specs = [('range30', '常态30分钟波幅', 100),
             ('inventory_vs_hold', '5位候选相对不交易的每户净值差', 1),
             ('mean_retraced_10m', '冲击后10分钟原始回吐', 100)]
    for ax, (key, title, scale) in zip(axes, specs):
        scenario = 'shock' if key == 'mean_retraced_10m' else 'normal'
        for i, variant in enumerate(VARIANTS):
            selected = [r for r in rows if r['variant'] == variant and r['scenario'] == scenario]
            values = [(r['range30']['median'] if key == 'range30' else r[key])*scale for r in selected]
            ax.scatter(i+np.linspace(-.09, .09, len(values)), values, color='#267288')
            ax.plot([i-.2, i+.2], [np.mean(values)]*2, color='#d17b35', lw=2)
        ax.set(xticks=range(3), xticklabels=[LABELS[v] for v in VARIANTS], title=title,
               ylabel='%' if scale == 100 else '金圆券')
        ax.grid(axis='y', alpha=.2)
    fig.tight_layout()
    fig.savefig(OUT/'comparison.png', dpi=150)
    plt.close(fig)
    aggregate = []
    for scenario in ['normal', 'shock']:
        for variant in VARIANTS:
            selected = [r for r in rows if (r['variant'], r['scenario']) == (variant, scenario)]
            d = dict(variant=variant, scenario=scenario)
            for key in ['trades_per_minute', 'inventory_trades_per_minute', 'inventory_vs_hold',
                        'short_realized_per_account', 'holding_median_minutes', 'mean_retraced_10m']:
                if key in selected[0]:
                    d[key] = float(np.mean([r[key] for r in selected]))
            d['range30'] = float(np.mean([r['range30']['median'] for r in selected]))
            aggregate.append(d)
    dump(OUT/'aggregate.json', aggregate)


if __name__ == '__main__':
    run()
