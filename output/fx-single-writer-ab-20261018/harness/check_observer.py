"""Preparation-only observer wiring check against a committed actor class AST.

No app imports, service/DB startup or load. External actor dependencies are replaced
with in-memory boundaries; this does not validate trading or funding correctness.
"""
import argparse
import ast
import asyncio
import contextvars
from dataclasses import dataclass
import importlib.util
import subprocess
import sys
import types
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--commit', required=True)
args = parser.parse_args()
repo = '/tmp/sunyunbo/thccb-single-writer-20261018'
source = subprocess.check_output(['git', '-C', repo, 'show',
                                  args.commit + ':backend/app/services/fx/writer.py'], text=True)
nodes = [node for node in ast.parse(source).body if isinstance(node, ast.ClassDef)
         and node.name in ('FxWriterState', 'ReloadPair', 'FxWriter')]
assert len(nodes) == 3


class HTTPException(Exception):
    pass


scope = {'asyncio': asyncio, 'dataclass': dataclass, 'HTTPException': HTTPException,
         'OWNERSHIP': types.SimpleNamespace(require_writes=lambda: None),
         'GATES': types.SimpleNamespace(held_keys_by_current_task=lambda: False)}
exec('from __future__ import annotations\n' + '\n'.join(ast.unparse(node) for node in nodes), scope)
for name in ('app', 'app.services', 'app.services.fx'):
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
module = types.ModuleType('app.services.fx.writer')
module.FxWriter = scope['FxWriter']
sys.modules[module.__name__] = module
spec = importlib.util.spec_from_file_location('fx_writer_observer', Path(__file__).with_name('fx_writer_observer.py'))
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)
route = contextvars.ContextVar('route', default='POST synthetic-preparation-check')
assert observer.install(route, lambda: 'synthetic_check')['status'] == 'attached'


@dataclass(frozen=True)
class Command:
    pair_id: int
    index: int


async def check():
    writer = module.FxWriter()
    writer._enabled = True

    async def load(pair):
        return types.SimpleNamespace(id=pair)

    writer._load_direct = load
    writer._invalidate = lambda pair: None
    entered, release = asyncio.Event(), asyncio.Event()
    labels = []

    async def op(writer, command):
        labels.append(route.get())
        if command.index == 1:
            entered.set()
            await release.wait()
        return command.index

    writer.register_op(Command, op)
    await writer._bootstrap(1)
    first = asyncio.create_task(writer.submit(Command(1, 1)))
    await entered.wait()
    second = asyncio.create_task(writer.submit(Command(1, 2)))
    await asyncio.sleep(0)
    release.set()
    assert await first == 1 and await second == 2
    await writer.stop()
    assert labels == ['background:fx_writer'] * 2, labels
    rows = observer.results()
    assert sum(row['count'] for row in rows if row['kind'] == 'queue_wait') == 2, rows
    assert sum(row['count'] for row in rows if row['kind'] == 'execution') == 2, rows
    assert all(row['submit_route'] == 'POST synthetic-preparation-check' for row in rows), rows
    assert all(row['mean_ms'] >= 0 for row in rows), rows


asyncio.run(check())
print('PASS: committed actor AST, two in-memory commands, timing counts and actor context; no app/DB import or service/load start')
