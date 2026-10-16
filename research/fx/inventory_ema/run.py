"""Paired warmup experiment. No production edits and no changes to frozen sources.

python -m research.fx.inventory_ema.run
"""
import difflib
import hashlib
import json
import math
from pathlib import Path
import numpy as np
from research.fx.intraday_6_8h import model
from research.fx.intraday_6_8h.run import summarize, swing_metrics

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / 'output/fx-inventory-ema-20261016'
SEEDS = [20261029, 20261030, 20261031, 20261102]
MODES = [('baseline', None), ('ema5', 5), ('ema10', 10), ('ema30', 30)]


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def experimental_model():
    source = Path(model.__file__).read_text()
    original = '''        history=logs[max(0,tick-cfg['inventory_window']):tick]
        reference=float(np.mean(history))'''
    replacement = '''        if settings.get('inventory_ema_half_life_minutes') is None:
            history=logs[max(0,tick-cfg['inventory_window']):tick]
            reference=float(np.mean(history))
        else:
            half_life_ticks=settings['inventory_ema_half_life_minutes']*60/5
            alpha=-math.expm1(-math.log(2)/half_life_ticks)
            if tick==1:reference=logs[0]
            reference+=alpha*(logs[tick-1]-reference)'''
    assert source.count(original) == 1
    derived = source.replace(original, replacement)
    (OUT / 'experimental_model.py').write_text(derived)
    (OUT / 'model.patch').write_text(''.join(difflib.unified_diff(source.splitlines(True), derived.splitlines(True), fromfile='frozen/model.py', tofile='experimental_model.py')))
    namespace = dict(model.__dict__)
    exec(compile(derived, str(OUT / 'experimental_model.py'), 'exec'), namespace)
    return namespace['simulate']


def checks(x, seed, mode, half_life):
    assert all(np.isfinite(v).all() for v in x.values())
    assert x['account_gold'].min() >= 0 and x['account_foreign'].min() >= 0
    assert not x['injection_gold'].any() and not x['injection_foreign'].any()
    np.testing.assert_allclose(x['gold'] + x['account_gold'].sum(axis=1), 600000, rtol=0, atol=1e-8)
    np.testing.assert_allclose(x['pool_foreign'] + x['account_foreign'].sum(axis=1), 120000, rtol=0, atol=1e-8)
    np.testing.assert_array_equal(x['account_trades'].sum(axis=1), x['trades'])
    np.testing.assert_allclose(x['account_wealth'], x['account_gold']+x['account_foreign']*x['prices'][:,None], rtol=0, atol=1e-8)
    if half_life is None:
        previous = ROOT / f'output/fx-warmup-24h-final-20261016/seed{seed}/trace.npz'
        if previous.exists():
            with np.load(previous) as old:
                for key in x: np.testing.assert_array_equal(x[key], old[key])
    else:
        # Independently check the geometric weighted average at sampled ticks.
        decay = 2 ** (-5 / (60 * half_life))
        logs = np.log(x['prices'])
        for tick in [1, 100, 360, 720, 5000, len(logs)-1]:
            weights = decay ** np.arange(tick-2, -1, -1)
            expected = decay**(tick-1)*logs[0] + (1-decay)*np.dot(weights, logs[1:tick])
            np.testing.assert_allclose(x['slow_reference'][tick], math.exp(expected), rtol=2e-12)
    for h in [5,10,30]:
        np.testing.assert_allclose((2**(-5/(60*h)))**(h*12), .5, rtol=1e-12)


def metrics(x, episodes):
    m = summarize(x, episodes, 24)
    s = slice(361, None)
    minutes = (len(x['prices'])-361)/12
    wave = swing_metrics(x['prices'][360:], .015)
    p = x['prices'][360:]
    ret10 = np.diff(np.log(p[::120]))
    gross = float(x['executed_abs_quantity'][s].sum())
    role_flow = {role: float(x[f'{key}_executed_abs_quantity'][s].sum()) for role,key in [('random','random'), ('inventory','reversion'), ('short','factor')]}
    return dict(fills_per_minute=m['trades_per_minute_after30'], cycle_minutes=wave['cycles_minutes']['median'],
        cycle_n=wave['cycles_minutes']['n'], up_minutes=wave['up_minutes']['median'], up_gain=wave['up_gain']['median'],
        cycle1pct_minutes=m['swings']['0.01']['cycles_minutes']['median'], range30=m['range30']['median'],
        return10m_std=float(np.std(ret10, ddof=1)), gap_rms=float(np.sqrt(np.mean(x['slow_gap'][s]**2))),
        mean_abs_target=float(np.mean(abs(x['inventory_target'][s]))), inventory_fills_per_minute=float(x['reversion_trades'][s].sum()/minutes),
        inventory_volume_share=role_flow['inventory']/gross, volume_per_minute=gross/minutes,
        clipped_fraction=float(x['clipped_orders'][s].sum()/x['order_attempts'][s].sum()),
        final_return=m['net_change'], role_volume_per_minute={k:v/minutes for k,v in role_flow.items()},
        inventory_final_net=float(x['reversion_net_position'][-1]))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    snapshot = ROOT / 'research/fx/warmup/CONFIG.json'
    config = json.loads(snapshot.read_text())
    tracked = [snapshot, Path(model.__file__), ROOT/'research/fx/reversion/inventory.py', ROOT/'research/fx/episodes/model.py', ROOT/'research/fx/activity/model.py', Path(__file__)]
    hashes = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in tracked}
    simulate = experimental_model()
    write(OUT/'manifest.json', dict(preset=config['name'], seeds=SEEDS, hours=24, modes=MODES,
        excluded_startup_minutes=30, wave_reversal=.015, source_sha256=hashes,
        definition='EMA of log prices, initialized at opening price; updates every tick using only prior close. Baseline is original 600min rolling log mean. No players, shocks, fees, borrowing or replenishment.'))
    results={}
    for mode, half_life in MODES:
        results[mode]={}
        for seed in SEEDS:
            print('Running', mode, seed, flush=True)
            cfg={**config['parameters'], 'ticks':24*720, 'seeds':[seed]}
            x,summary,episodes,*_ = simulate(cfg, config['profiles'], 3, config['activity_b'], seed, {}, config['policy'],
                {'trend_multiplier':config['trend_multiplier'], 'episode_start':13, 'inventory_ema_half_life_minutes':half_life})
            assert not summary['events']
            checks(x, seed, mode, half_life)
            results[mode][str(seed)] = metrics(x, episodes)
            folder=OUT/mode/f'seed{seed}'; folder.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(folder/'path.npz', **{k:x[k] for k in ['prices','slow_reference','slow_gap','inventory_target','trades','reversion_trades','executed_abs_quantity','reversion_executed_abs_quantity']})
            write(folder/'metrics.json', results[mode][str(seed)])
            write(OUT/'metrics.json', results)
            print('Finished', mode, seed, results[mode][str(seed)], flush=True)
    aggregate={}
    for mode,_ in MODES:
        rows=list(results[mode].values())
        aggregate[mode]={k:float(np.mean([r[k] for r in rows if r[k] is not None])) for k in rows[0] if isinstance(rows[0][k], (float,int))}
        aggregate[mode]['per_seed_cycle_minutes']=[r['cycle_minutes'] for r in rows]
        aggregate[mode]['per_seed_up_gain']=[r['up_gain'] for r in rows]
    write(OUT/'averages.json',aggregate)
    assert all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in hashes.items())
    write(OUT/'audit.json',dict(paths=16, baseline_exactly_matches_saved_traces=all((ROOT / f'output/fx-warmup-24h-final-20261016/seed{s}/trace.npz').exists() for s in SEEDS), conserved_assets=True, nonnegative_wallets=True,
        finite_series=True, no_shocks=True, independent_ema_reference_checks=True, source_hashes_unchanged=True,
        aggregation='Equal-weight mean of per-seed statistics; cycle and gain are means of seed medians.'))
    print(json.dumps(aggregate,ensure_ascii=False,indent=2),flush=True)


if __name__ == '__main__': main()
