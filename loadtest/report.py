"""Summarise one load-test run directory into <dir>/summary.md (read by RESULTS.md).

  .venv-load\\Scripts\\python.exe loadtest/report.py loadtest/results/cache-on
"""

import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def endpoint_table(rows: list[dict[str, str]]) -> list[str]:
    lines = ["| Endpoint group | Requests | Req/s | p50 ms | p95 ms | p99 ms | Failures |", "|---|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        if r["Type"] == "RESULT" or r["Name"] == "setup":
            continue
        lines.append(f"| {r['Name']} | {r['Request Count']} | {float(r['Requests/s']):.1f} | {r['50%']} | {r['95%']} | {r['99%']} | {r['Failure Count']} |")
    return lines


def results_table(rows: list[dict[str, str]]) -> list[str]:
    outcomes = [r for r in rows if r["Type"] == "RESULT"]
    if not outcomes:
        return []
    return ["| Outcome | Count |", "|---|---:|", *[f"| {r['Name']} | {r['Request Count']} |" for r in outcomes]]


def failures_table(path: Path) -> list[str]:
    rows = read_csv(path)
    if not rows:
        return ["No failures."]
    by_error: Counter[str] = Counter()
    for r in rows:
        by_error[r["Error"]] += int(r["Occurrences"])
    return ["| Error | Count |", "|---|---:|", *[f"| {e} | {n} |" for e, n in by_error.most_common()]]


def stages_table(history: list[dict[str, str]]) -> list[str]:
    by_users: dict[int, list[dict[str, str]]] = defaultdict(list)
    for r in history:
        if r["Name"] == "Aggregated" and r["User Count"] and int(r["User Count"]) > 0:
            by_users[int(r["User Count"])].append(r)
    lines = ["| Concurrent users | Req/s (mean) | p50 ms | p95 ms | p99 ms | Failures/s |", "|---:|---:|---:|---:|---:|---:|"]
    for users in sorted(by_users):
        rows = by_users[users][3:] or by_users[users]  # skip the ramp-up seconds

        def mean(col: str) -> float:
            values = [float(r[col]) for r in rows if r[col] not in ("", "N/A")]
            return statistics.mean(values) if values else 0.0

        lines.append(f"| {users} | {mean('Requests/s'):.0f} | {mean('50%'):.0f} | {mean('95%'):.0f} | {mean('99%'):.0f} | {mean('Failures/s'):.2f} |")
    return lines


def cache_stats(log: Path) -> tuple[Counter[str], int]:
    counts: Counter[str] = Counter()
    requests = 0
    if not log.exists():
        return counts, 0
    with open(log, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            start = line.find('{"request_id"')
            if start < 0:
                continue
            try:
                record = json.loads(line[start:])
            except ValueError:
                continue
            requests += 1
            for key, value in record.get("cache", {}).items():
                counts[key] += value
    return counts, requests


def monitor_stats(path: Path) -> str:
    rows = read_csv(path)
    if not rows:
        return "No monitor data."
    connections = max(int(r["threads_connected"]) for r in rows)
    api = max(float(r["api_rss_mb"]) for r in rows)
    worker = max(float(r["worker_rss_mb"]) for r in rows)
    return f"MySQL connections peak: **{connections}**; API processes RSS peak: **{api:.0f} MB**; worker RSS peak: **{worker:.0f} MB**."


def main() -> None:
    run = Path(sys.argv[1])
    out: list[str] = [f"## Run: {run.name}", ""]
    for phase, title in (("browse", "Browsing and shopping (stages 50/200/500/1000)"), ("flash", "Flash sale"),
                         ("login", "Login burst"), ("broadcast", "Broadcast during browsing")):
        stats = read_csv(run / f"{phase}_stats.csv")
        if not stats:
            continue
        out += [f"### {title}", ""]
        if phase == "browse":
            out += stages_table(read_csv(run / f"{phase}_stats_history.csv")) + [""]
        out += endpoint_table(stats) + [""]
        results = results_table(stats)
        if results:
            out += results + [""]
        out += ["Errors by code:", ""] + failures_table(run / f"{phase}_failures.csv") + [""]
        for extra in (run / f"{phase}.txt",):
            if extra.exists():
                out += ["```", extra.read_text(encoding="utf-8", errors="replace").strip(), "```", ""]
    counts, requests = cache_stats(run / "api.log")
    lookups = counts["hit"] + counts["miss"]
    ratio = f"{counts['hit'] / lookups:.1%}" if lookups else "n/a"
    out += ["### Cache and resources", "",
            f"Requests logged: {requests}; cache hits {counts['hit']}, misses {counts['miss']}, bypassed {counts['bypass']} "
            f"(hit ratio of cacheable lookups: **{ratio}**).", "", monitor_stats(run / "monitor.csv"), ""]
    (run / "summary.md").write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))


if __name__ == "__main__":
    main()
