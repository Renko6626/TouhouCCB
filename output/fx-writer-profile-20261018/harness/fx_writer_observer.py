"""Harness-only FX actor boundary timing; no transaction or operation wrapping.

Interface rechecked from final committed 6c767572 (originally 98e846e). Recheck final writer before enabling run.
"""
import collections
import functools
import time

_samples = collections.defaultdict(list)
_counts = collections.Counter()
_status = {'status': 'not_installed'}


def _distribution(values):
    ordered = sorted(values)
    n = len(ordered)
    return {'count': n, 'mean_ms': sum(ordered) / n,
            'p50_ms': ordered[int((n - 1) * .5)],
            'p95_ms': ordered[int((n - 1) * .95)],
            'p99_ms': ordered[int((n - 1) * .99)], 'max_ms': ordered[-1]}


def install(route, current_phase):
    global _status
    try:
        from app.services.fx.writer import FxWriter
    except ModuleNotFoundError as exc:
        if exc.name != 'app.services.fx.writer':
            raise
        _status = {'status': 'absent_in_source', 'queue_wait': None,
                   'execution': None, 'sql_attribution': 'no FX actor in this source'}
        return _status

    original_bootstrap = FxWriter._bootstrap
    original_consume = FxWriter._consume

    def attach(queue, pair_id):
        # _bootstrap creates an empty queue and schedules _consume without another
        # await. This wrapper runs before that newly scheduled consumer can run.
        original_put = queue.put_nowait
        original_get = queue.get
        pending = {}
        active = None

        def finish(boundary):
            nonlocal active
            if active is None:
                return
            started, item, execution_phase, submit_route = active
            command, future = item
            key = (execution_phase, pair_id, type(command).__name__, submit_route,
                   'execution', boundary)
            _samples[key].append((time.monotonic() - started) * 1000)
            _counts[(execution_phase, pair_id, 'finished_future_cancelled' if future.cancelled()
                     else 'finished_future_done' if future.done() else 'finished_future_pending')] += 1
            active = None

        def put(item):
            command, future = item
            # Timestamp precedes the nonblocking accepted enqueue by one dict write.
            pending[future] = (time.monotonic(), route.get())
            try:
                return original_put(item)
            except BaseException:
                pending.pop(future, None)
                raise

        async def get():
            nonlocal active
            # Next get begins after the previous command's future handling and any
            # recovery. Idle waiting in original_get is excluded from execution.
            finish('next_dequeue_call')
            item = await original_get()
            command, future = item
            enqueued = pending.pop(future, None)
            now = time.monotonic()
            execution_phase = current_phase()
            if enqueued is None:
                _counts[(execution_phase, pair_id, 'unobserved_enqueue')] += 1
                return item
            submitted, submit_route = enqueued
            skipped = future.done()
            key = (execution_phase, pair_id, type(command).__name__, submit_route,
                   'queue_wait', 'skipped_already_done' if skipped else 'started')
            _samples[key].append((now - submitted) * 1000)
            if not skipped:
                active = (now, item, execution_phase, submit_route)
            return item

        queue.put_nowait = put
        queue.get = get
        # Stop/delete drains the queue via get_nowait; no timing is fabricated for drained
        # commands that never pass the consumer boundary.
        queue._ab_observer_finish = finish

    @functools.wraps(original_bootstrap)
    async def bootstrap(self, pair_id, *args, **kwargs):
        token = route.set('background:fx_writer_bootstrap')
        try:
            response = await original_bootstrap(self, pair_id, *args, **kwargs)
            attach(self._queues[pair_id], pair_id)
            return response
        finally:
            route.reset(token)

    @functools.wraps(original_consume)
    async def consume(self, pair_id, *args, **kwargs):
        token = route.set('background:fx_writer')
        try:
            return await original_consume(self, pair_id, *args, **kwargs)
        finally:
            queue = self._queues.get(pair_id)
            finish = getattr(queue, '_ab_observer_finish', None)
            if finish is not None:
                finish('consumer_exit')
            route.reset(token)

    FxWriter._bootstrap = bootstrap
    FxWriter._consume = consume
    _status = {
        'status': 'attached', 'reviewed_interface_commit': '6c7675722cda265bd205dced6ce0f939ae7ac769',
        'hooks': ['FxWriter._bootstrap', 'FxWriter._consume',
                  'per-actor queue.put_nowait', 'per-actor queue.get'],
        'sql_attribution': 'consumer background:fx_writer; bootstrap background:fx_writer_bootstrap',
        'queue_wait': 'accepted put_nowait to consumer get return; includes scheduler delay',
        'execution': 'consumer get return to next get call or consumer exit; includes handler, commit/publication/notifications/future handling and recovery where present; excludes idle queue get',
        'phase': 'execution/dequeue phase; submission route retained separately',
        'overhead': 'per accepted enqueue: timestamp + dictionary entry; per dequeue/execution: timestamps, dictionary lookup, counters and sample append; two actor-level contextvar scopes; no extra task, await, SQL, I/O, polling or profiler',
        'comparability': 'same module loaded before/after; baseline actor absence is unavailable, never zero; SQL cursor observer applies equally to both',
        'limits': 'execution is consumer service elapsed, not pure DB time; future outcomes are observed done/cancelled flags, HTTP results remain success authority; shutdown-drained commands lack dequeue timing',
    }
    return _status


def results():
    timings = [{'phase': p, 'pair_id': pair, 'command': command,
                'submit_route': submit_route, 'kind': kind, 'boundary': boundary,
                **_distribution(values)}
               for (p, pair, command, submit_route, kind, boundary), values in _samples.items()]
    return timings


def counters():
    return [{'phase': p, 'pair_id': pair, 'kind': kind, 'count': count}
            for (p, pair, kind), count in _counts.items()]
