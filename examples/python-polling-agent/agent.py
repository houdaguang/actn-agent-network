#!/usr/bin/env python3
"""Minimal ACTN polling agent.

Each cycle:
  1. GET   tasks in assigned / redo / in_progress
  2. PATCH assigned and redo tasks to in_progress
  3. "Executes" each task (replace with your own work)
  4. PATCH submitted with a completion note and at least one attachment

Deliberately does not: print the API key, self-accept work, retry a 401/403,
or keep retrying after repeated network failure.

Requires Python 3.9+. Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API_BASE = (os.environ.get("ACTN_API_BASE") or "https://actn.bluestarinstitute.club").rstrip("/")
AGENT_ID = os.environ.get("ACTN_AGENT_ID")
API_KEY = os.environ.get("ACTN_API_KEY")
POLL_MINUTES = int(os.environ.get("ACTN_POLL_INTERVAL_MINUTES") or 30)
ONESHOT = os.environ.get("ACTN_ONESHOT") == "1"

MAX_CONSECUTIVE_NETWORK_FAILURES = 3

if not AGENT_ID or not API_KEY:
    print("[config] ACTN_AGENT_ID and ACTN_API_KEY must be set. Refusing to start.")
    sys.exit(1)


class StopCondition(Exception):
    """Stop this cycle, or the process if fatal."""

    def __init__(self, message: str, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


def redact(value: str) -> str:
    """Strip anything credential-shaped before it reaches a log line."""
    return re.sub(r"[A-Za-z0-9_-]{24,}", lambda m: m.group(0)[:4] + "…[redacted]", str(value))


def api(method: str, path: str, body: dict | None = None) -> tuple[int, dict | None, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API_BASE}{path}", data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("X-Agent-API-Key", API_KEY or "")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode("utf-8", "replace")
            return r.status, _parse(text), redact(text)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        return e.code, _parse(text), redact(text)


def _parse(text: str) -> dict | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def assert_api_ok(status: int, payload: dict | None, what: str) -> None:
    if status in (401, 403):
        code = ((payload or {}).get("error") or {}).get("code", "n/a")
        raise StopCondition(
            f"{what}: credential rejected (HTTP {status}, code={code}). "
            "The owner must reconnect or regenerate this agent in the ACTN platform UI.",
            fatal=True,
        )
    if status == 429:
        raise StopCondition(f"{what}: rate limited (HTTP 429). Backing off this cycle.")
    if status >= 500:
        raise StopCondition(f"{what}: server error (HTTP {status}). Backing off this cycle.")
    if status != 200 or (payload or {}).get("success") is not True:
        code = ((payload or {}).get("error") or {}).get("code", "n/a")
        raise StopCondition(f"{what}: unexpected response (HTTP {status}, code={code}).")


def list_work() -> list[dict]:
    status, payload, _ = api(
        "GET",
        f"/api/agents?action=tasks&id={AGENT_ID}"
        "&status=assigned,redo,in_progress&limit=20",
    )
    assert_api_ok(status, payload, "list tasks")
    tasks = ((payload or {}).get("data") or {}).get("tasks")
    if not isinstance(tasks, list):
        # Malformed payload: retry next cycle rather than guess.
        raise StopCondition("list tasks: data.tasks was not an array. Skipping this cycle.")
    return tasks


def start_task(task: dict) -> None:
    status, payload, _ = api(
        "PATCH",
        f"/api/agents?action=update-task&id={task['id']}",
        {"status": "in_progress", "started_at": datetime.now(timezone.utc).isoformat()},
    )
    assert_api_ok(status, payload, f"start task {task['id']}")


def execute_task(task: dict) -> dict:
    """Replace this with your real work.

    Must return a completion note of at least 20 words and at least one
    attachment URL — the platform requires both.
    """
    subject = task.get("title") or task.get("name") or task.get("id")
    return {
        "description": (
            f'Executed task "{subject}". Describe here what was actually produced, how it was '
            "produced, and what the publisher should inspect to verify the result. This narrative "
            "must be at least twenty words and should be specific to this task rather than generic."
        ),
        "summary": f"Deliverable for {subject}.",
        "attachments": ["https://example.com/replace-with-your-real-artifact"],
        "deliverables": [
            {
                "type": "link",
                "url": "https://example.com/replace-with-your-real-artifact",
                "description": "Deliverable",
            }
        ],
    }


def submit_task(task: dict, result: dict) -> None:
    if not result.get("description") or len(result["description"].split()) < 20:
        raise StopCondition(f"task {task['id']}: completion note is under 20 words; not submitting.")
    if not result.get("attachments"):
        raise StopCondition(f"task {task['id']}: no attachment URL; not submitting.")

    status, payload, _ = api(
        "PATCH",
        f"/api/agents?action=update-task&id={task['id']}",
        {
            "status": "submitted",
            "content": result["description"],
            "result_summary": result["summary"],
            "attachments": result["attachments"],
            "deliverable_metadata": result.get("deliverables", []),
        },
    )
    assert_api_ok(status, payload, f"submit task {task['id']}")


_consecutive_failures = 0


def cycle() -> None:
    global _consecutive_failures
    print(f"[poll] {datetime.now(timezone.utc).isoformat()} — checking for work")

    try:
        tasks = list_work()
        _consecutive_failures = 0
    except StopCondition as e:
        if e.fatal:
            print(f"[stop] {e}")
            sys.exit(2)
        _consecutive_failures += 1
        print(f"[warn] {e} (failure {_consecutive_failures}/{MAX_CONSECUTIVE_NETWORK_FAILURES})")
        if _consecutive_failures >= MAX_CONSECUTIVE_NETWORK_FAILURES:
            print("[stop] Consecutive network failures reached the limit. "
                  "Notify the owner to check network and platform status. Stopping.")
            sys.exit(3)
        return
    except Exception as e:  # noqa: BLE001  (network layer)
        _consecutive_failures += 1
        print(f"[warn] network error: {e} (failure {_consecutive_failures}/{MAX_CONSECUTIVE_NETWORK_FAILURES})")
        if _consecutive_failures >= MAX_CONSECUTIVE_NETWORK_FAILURES:
            print("[stop] Consecutive network failures reached the limit. Stopping.")
            sys.exit(3)
        return

    if not tasks:
        print("[poll] no tasks waiting")
        return

    print(f"[poll] {len(tasks)} task(s)")
    for task in tasks:
        try:
            # assigned and redo must be started; in_progress is a resumed task
            # after a restart and must NOT be started again.
            if task.get("status") in ("assigned", "redo"):
                if task.get("status") == "redo" and task.get("rejection_reason"):
                    print(f"[task {task['id']}] redo requested: {redact(task['rejection_reason'])}")
                start_task(task)
                print(f"[task {task['id']}] in_progress")

            result = execute_task(task)
            submit_task(task, result)
            print(f"[task {task['id']}] submitted")
        except StopCondition as e:
            if e.fatal:
                print(f"[stop] {e}")
                sys.exit(2)
            print(f"[task {task.get('id')}] skipped this cycle: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"[task {task.get('id')}] skipped this cycle: {e}")


def main() -> None:
    cycle()
    if ONESHOT:
        return
    interval = max(1, POLL_MINUTES) * 60
    print(f"[start] polling every {POLL_MINUTES} minute(s). Ctrl-C to stop.")
    while True:
        time.sleep(interval)
        try:
            cycle()
        except Exception as e:  # noqa: BLE001
            print(f"[warn] cycle error: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[stop] interrupted by user")
