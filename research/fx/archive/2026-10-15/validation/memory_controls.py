import json
import numpy as np
from scipy.signal import lfilter
from scipy.stats import linregress
from research.fx.diagnostics import acf,rs_curve,block_shuffle,describe
N=65536
# Exact stationary fractional Gaussian noise through circulant embedding.
# Covariance gamma(k)=.5*((k+1)**(2H)-2*k**(2H)+abs(k-1)**(2H)).
def fgn(rng,H=.8):
 k=np.arange(N,dtype=float)
 cov=.5*((k+1)**(2*H)-2*k**(2*H)+np.abs(k-1)**(2*H))
 lam=np.fft.rfft(np.r_[cov,0,cov[:0:-1]]).real
 assert lam.min()>0
 return np.fft.irfft(np.fft.rfft(rng.normal(size=2*N))*np.sqrt(lam),n=2*N)[:N]
results={}
for label in ['iid_exp','ar_logvol_phi08','fgn_H08','fgn_positive_vol','iid_scale_trend']:
 rows=[]
 for seed in range(8):
  rng=np.random.default_rng(seed+910)
  if label=='iid_exp':x=rng.exponential(size=N)
  elif label=='ar_logvol_phi08':x=np.exp(.3*lfilter([.6],[1,-.8],rng.normal(size=N+4096))[4096:])
  elif label=='fgn_H08':x=fgn(rng)
  elif label=='fgn_positive_vol':x=np.exp(.3*fgn(rng))
  else:x=rng.exponential(size=N)*np.linspace(1,3,N)
  row={}
  for q in [0,20,100]:
   c=rs_curve(x,q)
   row[str(q)]={'slope':c['scaling_slope'],'beta128':c['beta_n'][0],'beta16384':c['beta_n'][-1],
    'large_slope':linregress(np.log(c['sizes'][3:]),np.log(c['statistic'][3:])).slope}
  row['shuffle_q20']=rs_curve(block_shuffle(x,seed=seed),20)['scaling_slope']
  row['acf100_1000']=float(acf(x,1000)[100:].mean())
  rows.append(row)
 def summarise(v):return {'mean':float(np.mean(v)),'min':float(np.min(v)),'max':float(np.max(v))}
 results[label]={str(q):{key:summarise([r[str(q)][key] for r in rows]) for key in rows[0][str(q)]} for q in [0,20,100]}
 results[label]['shuffle_q20']=summarise([r['shuffle_q20'] for r in rows])
 results[label]['acf100_1000']=summarise([r['acf100_1000'] for r in rows])
# Independent direct formula for ACF and Lo's single-window Q.
rng=np.random.default_rng(901)
x=rng.normal(size=4096)
z=x-x.mean()
manual=np.array([np.dot(z[k:],z[:len(z)-k])/np.dot(z,z) for k in range(101)])
print('acf_max_error',np.max(np.abs(manual-acf(x,100))))
refs=[]
for n in [128,256,512,1024]:
 qs=[]
 for block in x.reshape(-1,n):
  z=block-block.mean();v=np.dot(z,z)/n
  v+=2*sum((1-j/21)*np.dot(z[j:],z[:-j])/n for j in range(1,21))
  s=np.cumsum(z);qs.append((max(0,s.max())-min(0,s.min()))/np.sqrt(v))
 refs.append(np.median(qs))
print('rs_max_error',np.max(np.abs(np.array(refs)-rs_curve(x,20)['statistic'])))
print(json.dumps(results,indent=2))
# Simple theoretical iid aggregation kurtosis check for centered compound Poisson.
y=rng.poisson(.5,1_000_000)-rng.poisson(.5,1_000_000)
print('compound_poisson_aggregation',{w:describe(y[:len(y)//w*w].reshape(-1,w).sum(1))['excess_kurtosis'] for w in [1,5,30,60,240]})
