"""Episode simulator fork: add a slow endogenous inventory role and signed shocks.

Derived from episodes/run.py; the disabled role is checked against its trace.
"""
from decimal import Decimal
import math
import numpy as np

from backend.app.services.fx.quantize import amount_down
from research.fx.activity.model import ActivityState
from research.fx.common.amm import Account,Pool
from research.fx.common.clock import market_streams,draw_events
from research.fx.common.execution import execute
from research.fx.factor_activity.model import components,target_position
from research.fx.episodes.model import BehaviorTrader,liquidation_mark
from research.fx.episodes.run import longest_empty
from .inventory import inventory_step


def simulate(cfg,profiles,inventory_count,b,seed,shock_tick=None,shock_return=0):
    episode_start=15
    random_count=episode_start-inventory_count
    variant='trailing' 
    n = cfg['accounts']
    assert inventory_count in (0,5) and n==20
    assert -.5<shock_return<.5
    state = ActivityState(b,cfg['tau'],cfg['mean_arrivals'],cfg['psi'],cfg['initial_z'])
    arrival_rngs,mark_rngs,execution_rng = market_streams(seed,n)
    pool = Pool(Decimal(str(cfg['pool_gold'])),Decimal(str(cfg['pool_foreign'])),Decimal(str(cfg['fee_rate'])))
    baseline = Decimal(str(cfg['initial_foreign']))
    accounts = [Account(str(i),Decimal(str(cfg['initial_gold'])),baseline) for i in range(n)]
    traders = [None if i<episode_start else BehaviorTrader(variant) for i in range(n)]
    fields = ['prices','gold','pool_foreign','returns','z_pre','intensity','arrivals','trades',
              'raw_demand','raw_abs_demand','actual_flow','executed_abs_quantity','gold_volume',
              'clipped_orders','skipped_orders','cash_saturated','foreign_saturated','wallet_gold_min','wallet_foreign_min']
    group_fields = ['arrivals','trades','raw_abs_demand','executed_abs_quantity','actual_flow','clipped_orders']
    fields += [f'{g}_{k}' for g in ['random','factor','reversion'] for k in group_fields]
    fields += ['injection_gold','random_net_position','factor_net_position','random_gold','factor_gold',
               'holding_accounts','long_basis','short_basis','long_quantity','short_quantity','entries','completions',
               'take_profit_exits','stop_loss_exits','trailing_exits','realized_pnl','idle_arrivals','order_attempts','injection_foreign','reversion_net_position',
               'reversion_gold','slow_reference','slow_gap','inventory_target']
    x = {k:np.zeros(cfg['ticks']+1) for k in fields}
    for field,value in [('prices',pool.price),('gold',pool.gold),('pool_foreign',pool.foreign),
                         ('wallet_gold_min',cfg['initial_gold']),('wallet_foreign_min',baseline),
                         ('random_gold',random_count*cfg['initial_gold']),('reversion_gold',inventory_count*cfg['initial_gold']),('factor_gold',(n-episode_start)*cfg['initial_gold'])]:
        x[field][0] = float(value)
    logs = np.zeros(cfg['ticks']+1);logs[0] = math.log(float(pool.price))
    gold_total = pool.gold+sum(a.gold for a in accounts)
    foreign_total = pool.foreign+sum(a.foreign for a in accounts)
    issued_gold=Decimal('0');issued_foreign=Decimal('0');issue=None;episodes={};fills=[];inventory_fills=[];realized=Decimal('0')
    for tick in range(1,cfg['ticks']+1):
        lam = state.intensity
        x['intensity'][tick],x['z_pre'][tick] = lam,state.z
        window = max(cfg['trend_window'],cfg['mean_window'])
        past = logs[max(0,tick-window):tick]
        trend,mean,_ = components(past,logs[tick-1],0,1,1,cfg)
        forecast = np.zeros(n)
        for i in range(episode_start,n):
            p = profiles[i]
            terms = np.array([p['trend']*trend,p['reversion']*mean,p['sigma']*float(mark_rngs[i].normal())])
            forecast[i] = terms.sum()
        history=logs[max(0,tick-cfg['inventory_window']):tick]
        reference=float(np.mean(history))
        gap=reference-logs[tick-1]
        x['slow_reference'][tick]=math.exp(reference)
        x['slow_gap'][tick]=gap
        target_cfg={**cfg,'position_cap':cfg['inventory_position_cap'],'sensitivity':cfg['inventory_sensitivity']}
        x['inventory_target'][tick]=float(target_position(gap,target_cfg)) if inventory_count else 0
        observed_price=pool.price
        acted_inventory=set()
        events = draw_events(arrival_rngs,lam/n,execution_rng)
        x['arrivals'][tick] = len(events)
        for event_index,i in enumerate(events):
            group='random' if i<random_count else 'reversion' if i<episode_start else 'factor'
            x[f'{group}_arrivals'][tick] += 1
            account = accounts[i]
            before_foreign,before_gold = account.foreign,account.gold
            if i<random_count:
                wanted = cfg['order_sigma_foreign']*float(mark_rngs[i].normal())
                quote = execute(pool,account,wanted,cfg['max_pool_fraction'])
                clipped = quote is None or abs(float(account.foreign-before_foreign))+2e-6<abs(wanted)
                filled = quote is not None
            elif i<episode_start:
                if i in acted_inventory:
                    x['idle_arrivals'][tick]+=1
                    continue
                event=inventory_step(pool,account,gap,baseline,observed_price,cfg)
                wanted=event['wanted'];filled=event['filled'];clipped=event['clipped']
                if abs(wanted)<=.000001:
                    x['idle_arrivals'][tick]+=1
                    continue
                if filled:
                    acted_inventory.add(i)
                    inventory_fills.append(dict(account=i,tick=tick,event_index=event_index,
                                                reference=x['slow_reference'][tick],**event))
            else:
                trader = traders[i]
                event = trader.arrive(pool,account,float(forecast[i]),profiles[i]['sigma'],tick,cfg)
                if event['action'] in ('wait','same_tick'):
                    x['idle_arrivals'][tick] += 1
                    continue
                wanted = event['wanted'];filled = event['action'] != 'unfilled'
                clipped = not filled or event['clipped']
                if filled:
                    fills.append(dict(account=i,event_index=event_index,**event))
                    if event['action']=='open':
                        episodes[trader.episode.id] = trader.episode
                        x['entries'][tick] += 1
                    else:
                        realized += Decimal(event['realized_profit'])
                        if event['complete']:
                            x['completions'][tick] += 1
                            x[event['reason']+'_exits'][tick] += 1
            x['order_attempts'][tick] += 1
            x['raw_demand'][tick] += wanted;x['raw_abs_demand'][tick] += abs(wanted)
            x[f'{group}_raw_abs_demand'][tick] += abs(wanted)
            x['cash_saturated'][tick] += int(wanted>0 and before_gold<=0)
            x['foreign_saturated'][tick] += int(wanted<0 and before_foreign<=0)
            x['clipped_orders'][tick] += int(clipped);x[f'{group}_clipped_orders'][tick] += int(clipped)
            if not filled:
                x['skipped_orders'][tick] += 1
                continue
            actual = float(account.foreign-before_foreign)
            for field,value in [('trades',1),('actual_flow',actual),('executed_abs_quantity',abs(actual))]:
                x[field][tick] += value;x[f'{group}_{field}'][tick] += value
            x['gold_volume'][tick] += float(abs(account.gold-before_gold))
        if tick==shock_tick:
            issue=dict(tick=tick,return_requested=shock_return,pre_price=str(pool.price),
                       pre_gold=str(pool.gold),pre_foreign=str(pool.foreign),pre_k=str(pool.gold*pool.foreign))
            if shock_return>0:
                amount=amount_down(pool.gold*Decimal(str(shock_return)))
                pool.inject(gold=amount);issued_gold+=amount;x['injection_gold'][tick]=float(amount)
            elif shock_return<0:
                amount=amount_down(pool.foreign*(1/(1+Decimal(str(shock_return)))-1))
                pool.inject(foreign=amount);issued_foreign+=amount;x['injection_foreign'][tick]=float(amount)
            issue.update(post_price=str(pool.price),post_gold=str(pool.gold),post_foreign=str(pool.foreign),post_k=str(pool.gold*pool.foreign))
        active = [t.episode for t in traders[episode_start:] if t.episode is not None]
        x['holding_accounts'][tick] = len(active)
        for direction,label in [(1,'long'),(-1,'short')]:
            x[label+'_basis'][tick] = float(sum((e.basis for e in active if e.direction==direction),Decimal('0')))
            x[label+'_quantity'][tick] = float(sum((e.remaining for e in active if e.direction==direction),Decimal('0')))
        x['realized_pnl'][tick] = float(realized)
        for group,subset in [('random',accounts[:random_count]),('reversion',accounts[random_count:episode_start]),('factor',accounts[episode_start:])]:
            x[group+'_net_position'][tick] = float(sum((a.foreign-baseline for a in subset),Decimal('0')))
            x[group+'_gold'][tick] = float(sum((a.gold for a in subset),Decimal('0')))
        assert pool.gold+sum(a.gold for a in accounts)==gold_total+issued_gold
        assert pool.foreign+sum(a.foreign for a in accounts)==foreign_total+issued_foreign
        x['prices'][tick] = float(pool.price);x['gold'][tick] = float(pool.gold);x['pool_foreign'][tick] = float(pool.foreign)
        x['wallet_gold_min'][tick] = float(min(a.gold for a in accounts))
        x['wallet_foreign_min'][tick] = float(min(a.foreign for a in accounts))
        logs[tick] = math.log(x['prices'][tick])
        x['returns'][tick] = math.log(x['prices'][tick]/x['prices'][tick-1])
        state.advance(x['returns'][tick])
    assert all(a.gold>=0 and a.foreign>=0 for a in accounts)
    for i in range(episode_start,n):
        e = traders[i].episode
        expected = baseline if e is None else baseline+e.direction*e.remaining
        assert accounts[i].foreign==expected
    all_episodes = list(episodes.values());done = [e for e in all_episodes if e.close_tick is not None]
    pending = [e for e in all_episodes if e.close_tick is None]
    unfinished = []
    for e in pending:
        mark = liquidation_mark(pool,e)
        unfinished.append(dict(**e.record(),age=cfg['ticks']-e.entry_tick,unrealized_pnl=str(mark['profit']),
                               liquidation_return=mark['return'],full_rebuy_affordable=e.direction==1 or accounts[int(e.account)].gold>=mark['gold']))
    holding = [e.close_tick-e.entry_tick for e in done]
    active_trades = np.flatnonzero(x['trades'])
    behavior_trades = np.flatnonzero(x['factor_trades'])
    summary = dict(seed=seed,b=b,random_count=random_count,inventory_count=inventory_count,variant=variant,ticks=cfg['ticks'],issue=issue,
                   peak_intensity=float(x['intensity'].max()),final_z=state.z,conserved_with_issue=True,
                   pool_gold=str(pool.gold),pool_foreign=str(pool.foreign),issued_gold=str(issued_gold),issued_foreign=str(issued_foreign),
                   accounts=[dict(name=a.name,policy='random' if i<random_count else 'reversion' if i<episode_start else 'episode',gold=str(a.gold),foreign=str(a.foreign)) for i,a in enumerate(accounts)],
                   behavior=dict(opened=len(all_episodes),completed=len(done),unfinished=len(pending),
                       unfinished_fraction=len(pending)/len(all_episodes) if all_episodes else None,
                       long_opened=sum(e.direction==1 for e in all_episodes),short_opened=sum(e.direction==-1 for e in all_episodes),
                       exits={reason:sum(e.closing_reason==reason for e in done) for reason in ['take_profit','stop_loss','trailing']},
                       mean_completed_holding=float(np.mean(holding)) if holding else None,
                       median_completed_holding=float(np.median(holding)) if holding else None,
                       max_completed_holding=max(holding) if holding else None,
                       realized_pnl=str(realized),unrealized_pnl=str(sum((Decimal(e['unrealized_pnl']) for e in unfinished),Decimal('0'))),
                       post_burn_entries=sum(e.entry_tick>cfg['burn_in'] for e in all_episodes),
                       carry_in=sum(e.entry_tick<=cfg['burn_in'] and (e.close_tick is None or e.close_tick>cfg['burn_in']) for e in all_episodes),
                       post_burn_completions=sum(e.close_tick>cfg['burn_in'] for e in done),
                       longest_no_trade_ticks=longest_empty(x['trades'][1:]),
                       post_burn_longest_no_trade_ticks=longest_empty(x['trades'][cfg['burn_in']+1:]),
                       last_trade_tick=int(active_trades[-1]) if len(active_trades) else None,
                       last_behavior_trade_tick=int(behavior_trades[-1]) if len(behavior_trades) else None),
                   unfinished_episodes=unfinished)
    return x,summary,[e.record() for e in all_episodes],fills,inventory_fills

