# Local-First + Sync Plan (No UI Changes)

## Goal
- Keep current UI unchanged.
- Keep shared DB as source of truth for cross-role visibility.
- Add local read replica + background sync to reduce network sensitivity and improve resilience.

## Stage 0 (Current, Completed)
- Baseline measurements collected:
  - startup (`startup_benchmark.py`)
  - click-latency (`orders_click_latency_benchmark.py`)
  - multi-client consistency (`multi_client_stress.py`)

## Stage 1 (Implemented in this iteration)
- Added local read replica service (`app/local_replica_sync.py`):
  - central -> local sync via SQLite backup API.
  - periodic sync (default every 5s) + fast sync trigger.
- Enabled local-first reads in the unified DB manager:
  - `data/dao/db_manager.py`
- Kept critical consistency points on central DB:
  - write path remains central.
  - change-log cursor reads remain central for synchronization correctness.
- Added short post-write central-read grace window, then fallback back to local reads.

## Stage 2 (Implemented in this iteration)
- Added durable local outbox for writes (per app node):
  - `app/durable_sql_outbox.py`
  - local SQLite queues in `%LOCALAPPDATA%/RemCard/cache` (`remcard_outbox.db`, `journal_outbox.db`).
- Added idempotent replay to central DB:
  - `sync_applied_ops` table in unified schema.
  - replay uses `INSERT OR IGNORE` by `op_id`, so retries do not duplicate writes.
- Added background replay workers in both DB managers:
  - exponential backoff,
  - max-attempt guard + failed status,
  - applied-entry pruning.
- Added safe fallback policy (no UI changes):
  - retryable central-write errors are queued only for safe SQL groups.
  - conflict strategy for queued statements:
    - `last-write-wins` for scalar-style `UPDATE/DELETE/REPLACE`,
    - append-only for event/history insert tables.

## Stage 3 (Hardening)
- Node identity + operation IDs for deduplication.
- Sync health panel in logs/telemetry (no UI redesign needed).
- Crash recovery validation suite for:
  - power loss,
  - SMB drop,
  - reconnect replay.
- Replay throughput optimization (implemented):
  - batched outbox status updates per replay cycle (`applied/retry/failed`) to reduce SQLite connection churn and lock pressure.
- Auto failover on DB corruption (implemented):
  - on startup quick-check failure, runtime DB is auto-restored from first healthy source:
    - preferred local replica (if valid),
    - then newest valid backup from `backups/`.
  - corrupted primary DB is quarantined to dedicated folder `Baza_rao3_jurnal/corrupted_db` with `*.reason.txt` metadata and healthy copy becomes primary automatically.
- Backup validation automation script (implemented):
  - `scripts/validate_backups.py` validates backup files (`quick_check` + `integrity_check`),
  - writes JSON reports to `Baza_rao3_jurnal/backup_health/reports`,
  - can optionally move invalid backup files to `Baza_rao3_jurnal/backup_health/invalid_backups`.
