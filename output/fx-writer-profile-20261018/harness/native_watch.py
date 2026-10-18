import os,time,json,subprocess,signal
from pathlib import Path
root=Path(os.environ['LOCAL_RUN_DIRECTORY']);out=Path(os.environ['LOCAL_OUTPUT_DIRECTORY']);deadline=time.monotonic()+600
while time.monotonic()<deadline:
 phase=root/'phase.txt'
 if phase.exists() and phase.read_text().strip()=='stress':break
 if (root/'driver-ended').exists():(out/'native-window.json').write_text(json.dumps({'error':'no stress reached'}));raise SystemExit(0)
 time.sleep(.05)
else:raise RuntimeError('stress not observed')
start=time.time();command=['/tmp/sunyunbo/fx-pool-loadtest-20261017/profiler/bin/py-spy','record','--rate','19','--gil','--native','--format','speedscope','--full-filenames','--pid',(root/'server.pid').read_text().strip(),'--output',str(out/'native-stress.speedscope.json')]
with (out/'native.log').open('w') as log:
 proc=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT)
 while proc.poll() is None and not (root/'protection.json').exists() and not (root/'stress-ended.json').exists():time.sleep(.05)
 stop=time.time()
 if proc.poll() is None:proc.send_signal(signal.SIGINT)
 try:status=proc.wait(timeout=20)
 except subprocess.TimeoutExpired:proc.terminate();status=proc.wait(timeout=5)
(out/'native-window.json').write_text(json.dumps(dict(start=start,stop_requested=stop,end=time.time(),status=status,rate=19,gil_only=True,native=True,blocking=True,command=command),indent=2)+'\n')
