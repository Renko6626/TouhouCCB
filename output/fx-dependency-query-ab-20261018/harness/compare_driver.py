import os,time,json,random
from pathlib import Path
import fx_http
root=Path(os.environ['LOCAL_RUN_DIRECTORY']);original=fx_http.Metrics.__setattr__
def setter(self,name,value):
    original(self,name,value)
    if name=='phase':
        (root/'phase.txt').write_text(value)
        with (root/'phases.jsonl').open('a') as f:f.write(json.dumps({'phase':value,'t':time.time()})+'\n')
fx_http.Metrics.__setattr__=setter
random.seed(20261017)
fx_http.cli()
