#!/usr/bin/env python3
import argparse
import sys
import threading
import time

from environment_registry import get_environment
from job_runner import (
    _read_state_file,
    _job_state_path,
    _now_utc_iso,
    _write_state_file,
    collect_environment_now,
    run_due_collection_jobs,
)


try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass


def mark_job_finished(db_path, environment_id, trigger, exit_code):
    existing = _read_state_file(_job_state_path(db_path, environment_id))
    _write_state_file(
        _job_state_path(db_path, environment_id),
        {
            "status": "finished" if exit_code == 0 else "failed",
            "started_at": existing.get("started_at", _now_utc_iso()),
            "finished_at": _now_utc_iso(),
            "last_exit": str(exit_code),
            "pid": "",
            "trigger": trigger,
        },
    )


def _local_log_timestamp():
    return time.strftime("%Y-%m-%d %I:%M:%S %p %Z", time.localtime())


def _environment_label(db_path, environment_id):
    environment = get_environment(db_path, environment_id, include_secret=True) or {}
    name = str(environment.get("name") or "").strip()
    env_id = str(environment_id or "").strip()
    if name and env_id:
        return "{0} ({1})".format(name, env_id)
    return name or env_id or "Environment"


def _log_line(db_path, environment_id, message):
    return "[{0}] [{1}] {2}".format(
        _local_log_timestamp(),
        _environment_label(db_path, environment_id),
        message,
    )


def _progress_logger(db_path, environment_id):
    def _log(message):
        print(_log_line(db_path, environment_id, message), flush=True)

    return _log


def _start_heartbeat(db_path, environment_id, interval_seconds=15):
    stop_event = threading.Event()
    started = time.time()

    def _run():
        while not stop_event.wait(interval_seconds):
            elapsed = int(time.time() - started)
            print(
                _log_line(
                    db_path,
                    environment_id,
                    "Collector still running; waiting for the current command to finish ({0}s elapsed).".format(elapsed),
                ),
                flush=True,
            )

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return stop_event, thread


def main():
    parser = argparse.ArgumentParser(description="Run IAM environment collection jobs.")
    parser.add_argument("--db-path", required=True, help="SQLite registry path")
    parser.add_argument("--env-id", help="Environment id for a single collector run")
    parser.add_argument("--trigger", default="manual", help="Job trigger label")
    parser.add_argument("--scheduler", action="store_true", help="Launch any due environment collector jobs")
    args = parser.parse_args()

    if args.scheduler:
        launched = run_due_collection_jobs(args.db_path)
        print("Scheduler launched {0} collector job(s).".format(len(launched)), flush=True)
        for item in launched:
            label = "started" if item.get("started") else "already running"
            if item.get("error"):
                label = "error: {0}".format(item.get("error"))
            print("  - {0} ({1}): {2}".format(item.get("environmentName") or item.get("environmentId"), item.get("environmentId"), label), flush=True)
        return 0

    if not args.env_id:
        raise ValueError("--env-id is required unless --scheduler is used.")

    print(_log_line(args.db_path, args.env_id, "Starting environment collector."), flush=True)
    exit_code = 0
    heartbeat_stop, heartbeat_thread = _start_heartbeat(args.db_path, args.env_id)
    try:
        dashboard = collect_environment_now(
            args.db_path,
            args.env_id,
            trigger=args.trigger,
            progress=_progress_logger(args.db_path, args.env_id),
        )
        print(
            _log_line(
                args.db_path,
                args.env_id,
                "Collector finished with status {0}.".format(dashboard.get("status") or "unknown"),
            ),
            flush=True,
        )
    except Exception as exc:
        exit_code = 1
        print(_log_line(args.db_path, args.env_id, "Collector failed: {0}".format(exc)), flush=True)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1)
        mark_job_finished(args.db_path, args.env_id, args.trigger, exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
