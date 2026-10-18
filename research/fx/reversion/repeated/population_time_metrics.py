"""Population-aware physical-time metrics; frozen 40-account analysis stays unchanged."""
import numpy as np

from research.fx.common.diagnostics import acf,describe
from .timing_metrics import event_response,price_ranges


def metrics(x,summary,episodes,tick_seconds):
    r=x['returns'][1:].copy();responses=[]
    events=summary['events'];last_tick=len(x['prices'])-1
    for i,event in enumerate(events):
        t=event['tick'];r[t-1]=np.log(float(event['pre_price'])/x['prices'][t-1])
        end=events[i+1]['tick']-1 if i+1<len(events) else last_tick
        response=event_response(x['prices'],event,end,tick_seconds)
        width=round(100/tick_seconds)
        if t+width<=end:
            window=slice(t+1,t+width+1)
            response.update(arrivals_100s=int(x['arrivals'][window].sum()),trades_100s=int(x['trades'][window].sum()),
                mean_intensity_100s=float(x['intensity'][window].mean()))
        else:response.update(arrivals_100s=None,trades_100s=None,mean_intensity_100s=None)
        responses.append(response)
    done=[e for e in episodes if e['close_tick'] is not None]
    holding=[(e['close_tick']-e['entry_tick'])*tick_seconds/60 for e in done]
    laglo=round(300/tick_seconds)+1;laghi=round(1800/tick_seconds)
    curves={name:acf(v,max(100,laghi)) for name,v in [('signed',r),('abs',abs(r)),('sq',r*r)]}
    n=x['account_wealth'].shape[1];short_start=3*n//4;short_count=n-short_start
    short=x['account_wealth'][:,short_start:]
    result=dict(tick_seconds=tick_seconds,hours=last_tick*tick_seconds/3600,
        range30=price_ranges(x['prices'],tick_seconds),shape=describe(r),
        mean_intensity=float(x['intensity'][1:].mean()),peak_intensity=float(x['intensity'][1:].max()),
        max_arrivals=int(x['arrivals'][1:].max()),trades=int(x['trades'][1:].sum()),
        clipped_fraction=float(x['clipped_orders'][1:].sum()/x['order_attempts'][1:].sum()),
        executed_fraction=float(x['executed_abs_quantity'][1:].sum()/x['raw_abs_demand'][1:].sum()),
        holding_median_minutes=float(np.median(holding)),holding_mean_minutes=float(np.mean(holding)),
        short_realized_per_account=float(summary['behavior']['realized_pnl'])/short_count,
        short_mean_wealth=float(short[-1].mean()),short_mean_vs_hold=float(short[-1].mean()-(5000+1000*x['prices'][-1])),
        short_win_fraction=float(np.mean([float(e['realized_profit'])>0 for e in done])),
        signed_acf_lag1=float(curves['signed'][1]),abs_acf_5_30m=float(curves['abs'][laglo:laghi+1].mean()),
        sq_acf_5_30m=float(curves['sq'][laglo:laghi+1].mean()),acf_band_ticks=[laglo,laghi],
        unfinished=summary['behavior']['unfinished'],events=responses)
    observed=[e for e in responses if e['retraced_10m'] is not None]
    if observed:
        result.update(observed_events=len(observed),mean_retraced_10m=float(np.mean([e['retraced_10m'] for e in observed])),
            mean_trades_100s=float(np.mean([e['trades_100s'] for e in observed])),
            mean_intensity_100s=float(np.mean([e['mean_intensity_100s'] for e in observed])),
            half_reached=sum(e['first_half_minutes'] is not None for e in observed),
            half_sustained=sum(e['sustained_half_start_minutes'] is not None for e in observed))
    return result
