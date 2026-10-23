# Load test results

**Read this first: the numbers are relative, not capacity figures.** Everything ran on one Windows laptop:
- the API (4 processes), the worker, MySQL 9.6 and Valkey (in Docker Desktop);
- Locust itself (1 master + 4 workers);
- the editor and background WSL/Docker activity, about 1.3 cores even when idle.

All of this shared 8 logical cores. Docker Desktop's port forwarding adds about 1 ms per Valkey call and measurable CPU. A production Linux server, with nothing else running and Locust elsewhere, will do considerably better. What these runs show reliably:
- the cache-on vs cache-off comparison;
- how the system behaves when overloaded (it sheds load cleanly, no 500s);
- that the correctness checks hold under concurrency.

Repeat runs varied by up to about ±40% in throughput, because of the shared machine.

Setup: database `lamsa_load` with 3,000 products and 10,000 users; 4 single-worker Uvicorn processes (ports 8001–8004, users spread round-robin); `PASSWORD_HASH_CONCURRENCY=2` per process; `DB_POOL_SIZE=10`, `DB_MAX_OVERFLOW=10`; push backend `log`. Runs on 2026-10-23 (`scripts/run_loadtest.ps1`, 90 s per stage). Raw data: `loadtest/results/cache-on|off/` (not committed).

## Summary

| | Cache on | Cache off |
|---|---:|---:|
| Browsing phase: requests served (6 min) | 30,783 | 21,405 |
| Browsing phase: mean throughput | **86 req/s** | 60 req/s |
| Browsing phase: failed requests | **18 %** (all `503 SERVICE_BUSY`) | 50 % (`503` + 902 client timeouts) |
| Browsing phase: p50 / p95 / p99 | 2.2 / 12 / 17 s | 7.1 / 18 / 57 s |
| HTTP 500 | **0** | **0** |
| Cache hit ratio (cacheable lookups) | **73.7 %** (22,411 hits, 7,989 misses, 5,620 bypassed under overload) | n/a |
| MySQL connections peak | 82 (= 4 × 20 pool + worker, the configured cap) | 82 |
| API memory peak (4 processes) | 773 MB | 770 MB |
| Flash sale | **correct**: 50 sold out of 50, stock 0, never negative | **correct**: 50 of 50 |
| Broadcast to 10,000 users during browsing | **44 s**, 10,000 / 10,000 delivered (log backend) | not sent: under the load the admin's login got `SERVICE_BUSY` twice |

### Per stage (browsing and shopping mix)

| Users | Cache on: req/s | p50 | p95 | p99 | failures/s | Cache off: req/s | p50 | p95 | p99 | failures/s |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 50 | 63 | 0.10 s | 0.54 s | 0.85 s | 0 | 31 | 0.69 s | 2.2 s | 3.2 s | 0 |
| 200 | 85 | 1.5 s | 4.1 s | 5.8 s | 0 | 38 | 4.1 s | 8.0 s | 11 s | 4.4 |
| 500 | 98 | 3.1 s | 10 s | 13 s | 9.3 | 66 | 6.8 s | 10 s | 13 s | 36 |
| 1000 | 97 | 8.4 s | 15 s | 22 s | 56 | ~40 | 7–9 s | 12–14 s | 13–17 s | ~26 |

At 50 users the cache cuts p50 from 0.69 s to 0.10 s and doubles throughput. On this machine the knee is at about 100 req/s. Beyond it, the database pools stay full and requests are shed with `503 SERVICE_BUSY` after `DB_POOL_TIMEOUT` (5 s), instead of piling up indefinitely.

### Per endpoint group (whole browsing phase)

| Group | Cache on: failures | p50 | p95 | p99 | Cache off: failures | p50 | p95 | p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Home (categories, banners, new, sale) | 15 % | 1.9 s | 11 s | 14 s | 52 % | 7.1 s | 20 s | 28 s |
| Lists and Arabic search | 19 % | 2.6 s | 12 s | 19 s | 49 % | 8.1 s | 33 s | 54 s |
| Product detail and related | 24 % | 3.2 s | 11 s | 15 s | 56 % | 6.8 s | 22 s | 43 s |
| Cart and checkout | 25 % | 2.8 s | 11 s | 14 s | 59 % | 6.0 s | 12 s | 14 s |
| Login and favorites | 10 % | 8.9 s | 22 s | 28 s | 32 % | 12 s | 55 s | 62 s |

These are request-weighted averages of the per-endpoint percentiles. Before the knee (50 users, cache on), cached catalog requests answered in 10–15 ms on the API side.

## Correctness under concurrency

- **Flash sale.** 300 users bought the same product (stock 50) at the same moment, in both runs. The database check afterwards (`seed_load_data --verify-flash 50`) found **stock 0, never negative, and exactly 50 units sold**. Everybody else got a clean `409 OUT_OF_STOCK` or `409 CART_HAS_ISSUES`. Some losers got `503 SERVICE_BUSY` (the load was shed).
- **No 500s** in any phase of the final runs. Errors during the stages were only `503 SERVICE_BUSY` with `Retry-After`, plus client-side timeouts with the cache off.
- **Login burst** (400 users logging in as fast as they could, 30 s):
  - 1,485 attempts in 30 s (about 50/s), of which 584 succeeded (about 20/s, at 2 hashes at a time per process, sharing the CPU with Locust);
  - the other 901 got `503 SERVICE_BUSY` after the 5 s queue timeout;
  - no 500s;
  - memory stayed flat at about 190 MB per API process, because 19 MiB Argon2 and the limiter bound memory per process.
- **Broadcast.** A campaign to all 10,000 users (10,000 devices) was dispatched in 44 s while 200 users kept browsing with **0 failures** (p50 1.0 s, p95 2.8 s). In production FCM is called in batches of 500, so the duration depends on FCM's latency rather than the log backend.

## Problems the load test found (fixed in this change)

1. **Login queued for hashing while holding a database connection.** A login burst held every pooled connection, and unrelated requests timed out. Now the read transaction ends before hashing, and authenticated requests release their connection right after the user is loaded.
2. **A deadlock retry crashed with a 500.** The retry read the user's name from an ORM object that the rollback had expired. Fixed, with a regression test.
3. **A deadlock could persist under flash-sale contention.** Cart and checkout writes now retry up to 3 times with a random backoff, then return `503 SERVICE_BUSY`, never a 500.
4. **Cache connection storms.** The default redis-py pool raised "Too many connections", and 200 new connections at once through Docker's port proxy took up to 1.3 s. Each process now has a bounded, blocking pool of 32 connections, warmed at startup.
5. **The 150 ms cache timeout measured event-loop lag.** It switched a healthy cache off exactly under load, which made everything hit the database. Measured: with 150 ms, most requests bypassed the cache at only 50 users; with 1 s, none did. Now `CACHE_TIMEOUT_MS=1000`, and the circuit opens only after 3 consecutive failures.
6. **Building an image URL resolved a filesystem path** (`Path.resolve()`, about 1 ms on Windows). That cost about 12 % of all CPU in catalog responses. URLs are now validated without touching the disk.
7. **Windows-specific:** `uvicorn --workers 4` handed nearly all connections to one process. The run therefore uses 4 processes on 4 ports. On Linux, use Gunicorn with Uvicorn workers or `uvicorn --workers`, or let Nginx balance over upstreams.

## Recommendations for production

- **Server.** 4 cores, Linux. Either 4 Uvicorn workers (Gunicorn with `-k uvicorn.workers.UvicornWorker -w 4`) or 4 processes behind the Nginx upstream. Run the worker process separately (`RUN_WORKER_IN_APP=false`).
- **`PASSWORD_HASH_CONCURRENCY` = cores ÷ workers** (1 with 4 workers on 4 cores), `PASSWORD_HASH_QUEUE_TIMEOUT=5`. A login spike after a campaign then degrades into short waits and a few `SERVICE_BUSY` responses, never into memory exhaustion.
- **Pool.** `DB_POOL_SIZE=10`, `DB_MAX_OVERFLOW=10`, `DB_POOL_TIMEOUT=5` per worker, with MySQL `max_connections=150` (4 × 20 + worker + headroom) and `innodb_buffer_pool_size` at about 50–60 % of MySQL's RAM (`docker/mysql/my.cnf`).
- **Cache.** Valkey on the same host (not through a port proxy), 256 MB, `allkeys-lru`, `CACHE_TIMEOUT_MS=1000`.
  - The cache roughly doubled throughput and cut latency severalfold here.
  - Product-detail freshness comes from per-product versions, not the TTL, so that TTL could safely go from 30 s to 120 s, and the list TTLs to 60 s, for a higher hit ratio. 73.7 % was limited by random product views across 3,000 products.
- **Nginx in front** (`docker/nginx/lamsa.conf`). The 2 s micro-cache absorbs the identical anonymous requests of a post-campaign spike before they reach Python, and the per-IP limits protect login. This is the most effective protection against spikes, and it wasn't part of this measurement.
- **Re-run this test on the production server** (Locust from another machine, `--host` pointing at it) before launch, to get real capacity figures.
