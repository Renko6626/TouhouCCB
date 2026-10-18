from pathlib import Path
root=Path(__file__).resolve().parent
p=root/'count_server.py';s=p.read_text().replace('gates=collections.defaultdict(list)','gates=collections.defaultdict(list); dependency_counts=collections.Counter()')
s=s.replace('def query(*args):counts[(phase,route.get())]+=1', '''def query(conn,cursor,statement,parameters,context,executemany):
    counts[(phase,route.get())]+=1
    text=' '.join(statement.lower().split())
    kind=None
    if 'union all' in text and 'fx_wallet.pair_id' in text and 'fx_short_position.pair_id' in text and 'outcome.market_id' in text:kind='dependency_union'
    elif text.startswith('select outcome.market_id ') and ' join position ' in text:kind='dependency_market'
    elif text.startswith('select fx_wallet.pair_id ') and ' where fx_wallet.user_id ' in text:kind='dependency_wallet'
    elif text.startswith('select fx_short_position.pair_id ') and ' where fx_short_position.user_id ' in text:kind='dependency_short'
    if kind:dependency_counts[(phase,route.get(),kind)]+=1''')
s=s.replace("(root/'gates.json').write_text", "(root/'dependency-sql.json').write_text(json.dumps([{'phase':p,'route':r,'kind':k,'executions':n} for (p,r,k),n in dependency_counts.items()],indent=2))\n            (root/'gates.json').write_text")
p.write_text(s)
p=root/'run.py';s=p.read_text().replace("print(variant+': 100 users", "print(variant+': deterministic reads',flush=True)\n        if run([sys.executable,str(root/'read_driver.py')],env,directory/'read-driver.log',source):raise RuntimeError('deterministic reads failed')\n        print(variant+': 100 users")
s=s.replace("'phases.jsonl')", "'phases.jsonl','dependency-sql.json','deterministic.json')")
p.write_text(s)
