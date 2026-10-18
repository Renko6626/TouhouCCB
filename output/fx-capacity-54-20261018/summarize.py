"""Derive comparison rows from completed runs; no database access required."""
import json
import gzip
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1] / "loadtest"))
from fx_http import percentiles


def resources(path):
    content = path.read_text() if path.exists() else gzip.open(str(path)+".gz", "rt").read()
    rows = [json.loads(line) for line in content.splitlines()]
    rows = [r for r in rows if r["phase"] == "capacity"]
    elapsed = sum(r["interval_s"] for r in rows)
    return dict(samples=len(rows), seconds=elapsed,
        cpu_percent=sum(r["app_cpu_percent"]*r["interval_s"] for r in rows)/elapsed,
        sql_per_s=sum(r["sql"] for r in rows)/elapsed,
        pool_peak=max(r["pool_peak"] for r in rows),
        queue_peak=max(max(r["queue_depths"].values(), default=0) for r in rows),
        handler=percentiles([ms for r in rows for ms in r["handlers_ms"]]),
        loop_lag=percentiles([ms for r in rows for ms in r["loop_lags_ms"]]))


def main():
    rows = []
    for name in ("current-60", "cadence8-60", "asgi-cadence8-60", "split-reads-60", "split-reads-300"):
        path = ROOT/name
        if not (path/"integrity.json").exists():
            continue
        summary = json.loads((path/"summary.json").read_text())
        stage = next(s for s in summary["stages"] if s["name"] == "capacity")
        trade = next(r for r in summary["requests"] if r["phase"] == "capacity" and r["name"] == "trade")
        attempts = stage["arrivals"]["offered"]
        good = stage["arrivals"].get("successful_cycles", 0)
        row = dict(run=name, seconds=stage["arrival_window_s"], offered=attempts,
            submitted=sum(trade["results"].values()), success_including_drain=good,
            success_by_deadline=stage["trade_responses_by_deadline"].get("ok", 0),
            success_per_s_in_window=stage["trade_responses_by_deadline"].get("ok", 0)/stage["arrival_window_s"],
            offered_not_completed_percent=100*(attempts-good)/attempts,
            skipped=stage["arrivals"].get("busy_user_skipped", 0),
            quote_or_trade_failed=stage["arrivals"].get("failed_cycles", 0),
            trade_latency=trade["latency_by_result"], cycle_latency=stage["completed_cycle_latency"],
            connected=stage["connected_at_end"], sse=stage["sse_delta"],
            owner=resources(path/"resources.jsonl"),
            integrity=json.loads((path/"integrity.json").read_text()),
            visibility=json.loads((path/"visibility.json").read_text()),
            client_schedule_lag=stage["client_schedule_lag"])
        if (path/"read-worker").exists():
            row["reader"] = resources(path/"read-worker/resources.jsonl")
        client_path = path/"client-resources.jsonl"
        content = client_path.read_text() if client_path.exists() else gzip.open(str(client_path)+".gz", "rt").read()
        client_rows = [json.loads(line) for line in content.splitlines()]
        client_rows = [r for r in client_rows if r["phase"] == "capacity"]
        elapsed = sum(r["interval_s"] for r in client_rows)
        row["client_cpu_percent"] = sum(r["cpu_percent"]*r["interval_s"] for r in client_rows)/elapsed
        rows.append(row)
    (ROOT/"comparison.json").write_text(json.dumps(rows, indent=2)+"\n")
    print("| 方案 | 计划/提交/成功（含排空） | 窗口内成功/s | POST P99 ms | 未完成计划 % | 1秒采样队列最大值 | owner CPU % |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        print(f"| {r['run']} | {r['offered']}/{r['submitted']}/{r['success_including_drain']} | "
              f"{r['success_per_s_in_window']:.2f} | {r['trade_latency']['ok']['p99_ms']:.1f} | "
              f"{r['offered_not_completed_percent']:.2f} | {r['owner']['queue_peak']} | {r['owner']['cpu_percent']:.1f} |")


if __name__ == "__main__":
    main()
