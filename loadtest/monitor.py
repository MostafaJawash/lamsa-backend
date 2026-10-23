"""Samples once per second while a load test runs: MySQL connections and API/worker memory.

  .venv-load\\Scripts\\python.exe loadtest/monitor.py --out <csv> --api-pids <pid,pid> --worker-pid <pid>

Reads ONLY the connection keys from .env (DB_HOST, DB_PORT, DB_USER, DB_PASSWORD) and connects to
DB_NAME from the environment, which must end in "_load". Stops when the file <csv>.stop appears.
"""

import argparse
import csv
import os
import sys
import time
from pathlib import Path

import psutil
import pymysql


def env_value(key: str, default: str) -> str:
    if key in os.environ:
        return os.environ[key]
    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return default


def tree_rss_mb(pid: int) -> float:
    try:
        root = psutil.Process(pid)
        return sum(p.memory_info().rss for p in [root, *root.children(recursive=True)]) / 1024 / 1024
    except psutil.Error:
        return 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--api-pids", required=True, help="comma-separated PIDs of the API processes")
    parser.add_argument("--worker-pid", type=int, required=True)
    args = parser.parse_args()
    db_name = os.environ.get("DB_NAME", "")
    if not db_name.endswith("_load"):
        sys.exit(f"Refusing: DB_NAME={db_name!r} must end with _load")
    connection = pymysql.connect(
        host=env_value("DB_HOST", "localhost"), port=int(env_value("DB_PORT", "3306")), user=env_value("DB_USER", "lamsa"),
        password=env_value("DB_PASSWORD", ""), database=db_name, autocommit=True,
    )
    stop = Path(args.out + ".stop")
    api_pids = [int(pid) for pid in args.api_pids.split(",") if pid]
    with open(args.out, "w", newline="", encoding="utf-8") as handle, connection.cursor() as cursor:
        writer = csv.writer(handle)
        writer.writerow(["time", "threads_connected", "api_rss_mb", "worker_rss_mb"])
        while not stop.exists():
            cursor.execute("SHOW GLOBAL STATUS LIKE 'Threads_connected'")
            row = cursor.fetchone()
            writer.writerow([round(time.time(), 1), int(row[1]) if row else 0,
                             round(sum(tree_rss_mb(pid) for pid in api_pids), 1), round(tree_rss_mb(args.worker_pid), 1)])
            handle.flush()
            time.sleep(1)
    stop.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
