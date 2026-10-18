"""Recent seven complete UTC days of Binance BTCUSDT, compared at 1m resolution.
Run: python output/btc-timescale-20261016/run.py
"""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import csv
import json
import hashlib
import runpy
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
import numpy as np

OUT=Path(__file__).resolve().parent
HELPERS=runpy.run_path(str(ROOT/'output/fx-timescale-20261016/run.py'))
BASE='https://data-api.binance.vision/api/v3/klines'


def sample_metrics(prices):
    result={'minutes':len(prices)-1,'blocks':{},'swings':{}}
    for width in [15,30,60,1440]:
        n=(len(prices)-1)//width
        if not n:continue
        parts=[prices[i*width:(i+1)*width+1] for i in range(n)]
        ranges=[(p.max()-p.min())/p[0] for p in parts]
        net=[abs(p[-1]/p[0]-1) for p in parts]
        result['blocks'][str(width)]={'count':n,'range':HELPERS['stats'](ranges),'absolute_net_return':HELPERS['stats'](net)}
    for threshold in [.005,.01,.02]:
        _,legs,cycles=HELPERS['swings'](prices,threshold)
        # Shared helper assumes 5 seconds/sample; this dataset has 60 seconds/sample.
        result['swings'][str(threshold)]={'leg_minutes':HELPERS['stats']([v['minutes']*12 for v in legs]),
            'overlapping_cycle_minutes':HELPERS['stats']([v*12 for v in cycles])}
    return result


def main():
    end=datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
    start=end-timedelta(days=7)
    lo,hi=int(start.timestamp()*1000),int(end.timestamp()*1000)
    sources=[]
    def fetch(at):
        stop=min(at+1000*60000,hi)
        url=BASE+'?'+urllib.parse.urlencode(dict(symbol='BTCUSDT',interval='1m',startTime=at,endTime=stop-1,limit=1000))
        with urllib.request.urlopen(url,timeout=30) as response:rows=json.load(response)
        assert isinstance(rows,list) and rows
        return url,rows
    cache=OUT/'raw-klines.json'
    if cache.exists():
        saved=json.loads(cache.read_text());raw=saved['rows'];sources=saved['sources']
        assert saved['start_utc']==start.isoformat() and saved['end_exclusive_utc']==end.isoformat()
    else:
        raw=[]
        with ThreadPoolExecutor(max_workers=3) as pool:
            for url,rows in pool.map(fetch,range(lo,hi,1000*60000)):
                sources.append(url);raw.extend(rows)
                print('Downloaded',len(raw),'candles',flush=True)
        cache.write_text(json.dumps(dict(start_utc=start.isoformat(),end_exclusive_utc=end.isoformat(),
            fetched_at_utc=datetime.now(timezone.utc).isoformat(),sources=sources,rows=raw))+'\n')
    raw.sort(key=lambda row:row[0])
    timestamps=np.array([r[0] for r in raw],dtype=np.int64)
    np.testing.assert_array_equal(timestamps,np.arange(lo,hi,60000))
    ohlc=np.array([[float(v) for v in r[1:5]] for r in raw])
    assert np.isfinite(ohlc).all() and (ohlc>0).all()
    assert np.all(ohlc[:,1]>=ohlc[:,[0,3]].max(axis=1)) and np.all(ohlc[:,2]<=ohlc[:,[0,3]].min(axis=1))
    prices=np.r_[ohlc[0,0],ohlc[:,3]]
    btc=sample_metrics(prices)
    bots={}
    pooled_blocks={str(w):{'range':[],'absolute_net_return':[]} for w in [15,30,60]}
    pooled_swings={str(d):{'leg_minutes':[],'overlapping_cycle_minutes':[]} for d in [.005,.01,.02]}
    for seed in range(20261016,20261022):
        with np.load(ROOT/f'output/fx-reference-sides-20261016/seed{seed}/trace.npz') as z:
            p=z['prices'][360::12]
            bots[str(seed)]=sample_metrics(p)
            for w in [15,30,60]:
                for i in range((len(p)-1)//w):
                    part=p[i*w:(i+1)*w+1]
                    pooled_blocks[str(w)]['range'].append((part.max()-part.min())/part[0])
                    pooled_blocks[str(w)]['absolute_net_return'].append(abs(part[-1]/part[0]-1))
            for d in [.005,.01,.02]:
                _,legs,cycles=HELPERS['swings'](p,d)
                pooled_swings[str(d)]['leg_minutes'].extend(v['minutes']*12 for v in legs)
                pooled_swings[str(d)]['overlapping_cycle_minutes'].extend(v*12 for v in cycles)
    bot_pooled={'blocks':{w:{k:HELPERS['stats'](v) for k,v in values.items()} for w,values in pooled_blocks.items()},
                'swings':{d:{k:HELPERS['stats'](v) for k,v in values.items()} for d,values in pooled_swings.items()}}
    result=dict(start_utc=start.isoformat(),end_exclusive_utc=end.isoformat(),symbol='BTCUSDT',
        source='Binance spot public REST API',rows=len(raw),btc=btc,bot_seeds=bots,bot_pooled=bot_pooled,
        comparison_rule='Both use 1-minute sampled prices. Ranges use close samples plus block start, not intraminute high/low.',
        limitations=['Seven recent days are not long-run BTC norms.',
            '1m sampling misses intraminute reversals; bot results differ from earlier 5s analysis.',
            'Confirmed extrema exclude first boundary leg and unfinished tail; cycles overlap.',
            'Bot sample is six seeds after first 30m; each path is only about 13.4 hours.'])
    (OUT/'metrics.json').write_text(json.dumps(result,indent=2)+'\n')
    with (OUT/'btc-1m.csv').open('w') as f:
        writer=csv.writer(f);writer.writerow(['open_time_utc','open','high','low','close'])
        for row,values in zip(raw,ohlc):writer.writerow([datetime.fromtimestamp(row[0]/1000,timezone.utc).isoformat(),*values])
    (OUT/'audit.json').write_text(json.dumps(dict(expected_candles=10080,actual_candles=len(raw),
        timestamps_complete_unique_and_ordered=True,ohlc_finite_positive_and_consistent=True,
        raw_sha256=hashlib.sha256(cache.read_bytes()).hexdigest(),request_urls=sources),indent=2)+'\n')
    print(json.dumps(result,indent=2),flush=True)


if __name__=='__main__':main()
