"""Metrics in physical time; no assumptions that a tick lasts ten seconds."""
import numpy as np

from research.fx.common.diagnostics import acf,describe


def price_ranges(prices,tick_seconds,minutes=30):
    width=round(minutes*60/tick_seconds)
    values=[]
    for start in range(0,len(prices)-width,width):
        window=prices[start:start+width+1]
        values.append(float((window.max()-window.min())/window[0]))
    return dict(windows=len(values),ticks_per_window=width,median=float(np.median(values)),
                p10=float(np.quantile(values,.1)),p90=float(np.quantile(values,.9)))


def event_response(prices,event,end_tick,tick_seconds):
    t=event['tick'];pre=float(event['pre_price']);post=float(event['post_price'])
    remaining=(prices[t:end_tick+1]-pre)/(post-pre)
    hits=np.flatnonzero(remaining<=.5)
    count=round(120/tick_seconds)
    sustained=[k for k in range(1,len(remaining)-count+1) if (remaining[k:k+count]<=.5).all()]
    horizon=round(600/tick_seconds);average=round(60/tick_seconds)
    price_10m=float(prices[t+horizon-average+1:t+horizon+1].mean()) if t+horizon<=end_tick else None
    return dict(tick=t,jump=event['return_requested'],observed_followup_ticks=end_tick-t,
        first_half_minutes=float(hits[0]*tick_seconds/60) if len(hits) else None,
        sustained_half_start_minutes=float(sustained[0]*tick_seconds/60) if sustained else None,
        price_10m=price_10m,retraced_10m=(post-price_10m)/(post-pre) if price_10m is not None else None)


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
    short=x['account_wealth'][:,30:]
    result=dict(tick_seconds=tick_seconds,hours=last_tick*tick_seconds/3600,
        range30=price_ranges(x['prices'],tick_seconds),shape=describe(r),
        mean_intensity=float(x['intensity'][1:].mean()),peak_intensity=float(x['intensity'][1:].max()),
        max_arrivals=int(x['arrivals'][1:].max()),trades=int(x['trades'][1:].sum()),
        clipped_fraction=float(x['clipped_orders'][1:].sum()/x['order_attempts'][1:].sum()),
        executed_fraction=float(x['executed_abs_quantity'][1:].sum()/x['raw_abs_demand'][1:].sum()),
        holding_median_minutes=float(np.median(holding)),holding_mean_minutes=float(np.mean(holding)),
        short_realized_per_account=float(summary['behavior']['realized_pnl'])/10,
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
