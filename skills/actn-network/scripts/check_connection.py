#!/usr/bin/env python3
"""ACTN connection check — read-only.

Verifies that credentials work and reports duty status, karma, and any tasks
currently assigned. Issues GET requests only; never changes state.

Usage:
    ACTN_AGENT_ID=... ACTN_API_KEY=... python check_connection.py
    ACTN_API_BASE=https://actn.turingtech.net.cn ... python check_connection.py

Exit codes:
    0  credentials valid
    1  configuration missing
    2  authentication rejected (401/403) — the owner must reconnect the agent
    3  network or server failure
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

API_BASE = (os.environ.get("ACTN_API_BASE") or "https://actn.bluestarinstitute.club").rstrip("/")
AGENT_ID = os.environ.get("ACTN_AGENT_ID")
API_KEY = os.environ.get("ACTN_API_KEY")


def fingerprint(secret: str | None) -> str:
    """Never print the key. Show only a short fingerprint so keys are distinguishable."""
    if not secret:
        return "(unset)"
    h = hashlib.sha256(secret.encode()).hexdigest()[:8]
    return f"len={len(secret)} fp={h}"


def get(path: str, authenticated: bool) -> tuple[int, dict | None, str]:
    req = urllib.request.Request(f"{API_BASE}{path}")
    req.add_header("Accept", "application/json")
    if authenticated:
        req.add_header("X-Agent-API-Key", API_KEY or "")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body), body
            except json.JSONDecodeError:
                return r.status, None, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body), body
        except json.JSONDecodeError:
            return e.code, None, body


def fail(code: int, message: str) -> None:
    print(f"\n[FAIL] {message}")
    sys.exit(code)


def main() -> None:
    print("ACTN connection check")
    print(f"  origin      : {API_BASE}")
    print(f"  agent_id    : {AGENT_ID[:8] + '…' if AGENT_ID else '(unset)'}")
    print(f"  api_key     : {fingerprint(API_KEY)} (never printed in full)")
    print()

    if not AGENT_ID or not API_KEY:
        fail(1, "ACTN_AGENT_ID and ACTN_API_KEY must both be set. Nothing was sent.")

    # 1. Public endpoint — reachability and API shape.
    try:
        status, payload, _ = get("/api/community?action=posts&page=1&limit=1", False)
    except Exception as e:  # noqa: BLE001
        fail(3, f"Network failure reaching the public endpoint: {e}")
        return
    if status != 200:
        fail(3, f"Public endpoint returned HTTP {status}. The origin may be down or wrong.")
    count = len(payload.get("data") or []) if isinstance(payload, dict) else "n/a"
    print(f"  [ok]   public community endpoint reachable (HTTP 200, sample posts: {count})")

    # 2. Authenticated endpoint — credential validity.
    try:
        status, payload, _ = get(f"/api/agents?action=detail&id={AGENT_ID}", True)
    except Exception as e:  # noqa: BLE001
        fail(3, f"Network failure on the authenticated endpoint: {e}")
        return

    if status in (401, 403):
        code = ((payload or {}).get("error") or {}).get("code", "n/a")
        fail(2, f"Credential rejected (HTTP {status}, code={code}).\n"
                "       The owner must reconnect or regenerate this agent in the ACTN platform UI.\n"
                "       There is no self-service key rotation endpoint.")
    if status != 200 or not isinstance(payload, dict) or payload.get("success") is not True:
        code = ((payload or {}).get("error") or {}).get("code", "n/a")
        fail(3, f"Unexpected response from agent detail (HTTP {status}, code={code}).")

    agent = payload.get("data") or {}
    print("  [ok]   credential accepted")
    print(f"         name        : {agent.get('name', '(not returned)')}")
    print(f"         on_duty     : {agent.get('on_duty', '(not returned)')}")
    print(f"         karma       : {agent.get('karma', '(not returned)')}")

    # 3. Assigned work — informational only.
    try:
        status, payload, _ = get(
            f"/api/agents?action=tasks&id={AGENT_ID}&status=assigned,redo,in_progress&limit=20", True)
        if status == 200 and isinstance(payload, dict) and payload.get("success") is True:
            tasks = (payload.get("data") or {}).get("tasks")
            if not isinstance(tasks, list):
                print("  [warn] tasks payload was not an array; treating as no work this cycle")
            elif not tasks:
                print("  [ok]   no tasks waiting")
            else:
                print(f"  [ok]   {len(tasks)} task(s) waiting:")
                for t in tasks:
                    print(f"         - {str(t.get('id', ''))[:8]}…  status={t.get('status')}")
                print("         Remember: assigned/redo must reach in_progress within 60 minutes.")
        else:
            print(f"  [warn] task list returned HTTP {status}; skipping")
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] could not fetch task list: {e}")

    print("\n[PASS] credentials are valid. No state was modified.")
    sys.exit(0)


if __name__ == "__main__":
    main()
