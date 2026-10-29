"""Run the launch-gate request mix against a running API.

Example: python scripts/load_search.py scenarios.jsonl --rps 100 --seconds 900
Each JSONL line is a POST /v1/pois/search request body.
"""

import argparse
import asyncio
import json
import math
import time
from pathlib import Path

import httpx


async def run(url: str, scenarios: list[dict], rps: int, seconds: int) -> dict:
    semaphore = asyncio.Semaphore(max(200, rps * 2))
    samples: list[float] = []
    failures = 0
    completed = 0
    late_schedules = 0
    pending: set[asyncio.Task] = set()

    async def one(payload: dict, client: httpx.AsyncClient) -> None:
        nonlocal failures, completed
        async with semaphore:
            started = time.perf_counter()
            try:
                response = await client.post(f"{url.rstrip('/')}/v1/pois/search", json=payload)
                if response.status_code != 200:
                    failures += 1
                else:
                    samples.append(time.perf_counter() - started)
            except httpx.HTTPError:
                failures += 1
            completed += 1

    timeout = httpx.Timeout(3.0)
    limits = httpx.Limits(max_connections=max(200, rps * 2), max_keepalive_connections=max(100, rps))
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        start = time.perf_counter()
        total = rps * seconds
        for index in range(total):
            due = start + index / rps
            await asyncio.sleep(max(0, due - time.perf_counter()))
            if len(pending) >= max(1000, rps * 10):
                await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if time.perf_counter() - due > 1 / rps:
                late_schedules += 1
            task = asyncio.create_task(one(scenarios[index % len(scenarios)], client))
            pending.add(task)
            task.add_done_callback(pending.discard)
        if pending:
            await asyncio.gather(*pending)
        elapsed = time.perf_counter() - start
    samples.sort()
    p95 = samples[math.ceil(0.95 * len(samples)) - 1] if samples else None
    achieved_rps = completed / elapsed
    failure_rate = failures / completed if completed else 1
    return {
        "requested_rps": rps,
        "duration_seconds": seconds,
        "actual_duration_seconds": round(elapsed, 2),
        "achieved_rps": round(achieved_rps, 2),
        "requests": completed,
        "failures": failures,
        "failure_rate": failure_rate,
        "late_schedules": late_schedules,
        "p95_seconds": p95,
        "api_gate_met": bool(
            p95 is not None
            and p95 < 1
            and completed == rps * seconds
            and failures == 0
            and late_schedules < rps * seconds * 0.01
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenarios", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:18000")
    parser.add_argument("--rps", type=int, default=100)
    parser.add_argument("--seconds", type=int, default=900)
    args = parser.parse_args()
    if args.rps < 1 or args.seconds < 1:
        parser.error("rps and seconds must be positive")
    scenarios = [
        json.loads(line) for line in args.scenarios.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if not scenarios:
        parser.error("scenarios file is empty")
    print(json.dumps(asyncio.run(run(args.url, scenarios, args.rps, args.seconds)), indent=2))


if __name__ == "__main__":
    main()
