#!/usr/bin/env python3
"""Concurrent image generation load test with returned-image URL validation.

Examples:
  python tools/image_load_test.py --api-key "$CHATGPT2API_AUTH_KEY" -n 20 -c 5
  python tools/image_load_test.py --base-url http://127.0.0.1:31000 --endpoint /v1/images/generations -n 40 -c 10
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class Result:
    index: int
    elapsed_s: float
    status: int | None = None
    image_status: int | None = None
    image_bytes: int = 0
    image_url_found: bool = False
    error: str = ""


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def extract_url(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    items = payload.get("data")
    if not isinstance(items, list):
        return ""
    for item in items:
        if isinstance(item, dict):
            url = item.get("url")
            if isinstance(url, str) and url.strip():
                return url.strip()
    return ""


def request_json(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def fetch_image(url: str, timeout: float, max_bytes: int) -> tuple[int, int]:
    request = urllib.request.Request(url, headers={"User-Agent": "image-load-test/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = response.status
        size = 0
        while True:
            chunk = response.read(min(1024 * 1024, max_bytes - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise ValueError(f"image response exceeds {max_bytes} bytes")
        return status, size


def run_one(index: int, args: argparse.Namespace, headers: dict[str, str]) -> Result:
    started = time.perf_counter()
    result = Result(index=index, elapsed_s=0.0)
    body = json.dumps(
        {
            "model": args.model,
            "prompt": f"{args.prompt} [load-test-{index}]",
            "size": args.size,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        result.status, raw = request_json(args.url, headers, body, args.request_timeout)
        if result.status < 200 or result.status >= 300:
            result.error = raw.decode("utf-8", errors="replace")[:500]
        else:
            image_url = extract_url(json.loads(raw))
            result.image_url_found = bool(image_url)
            if not image_url:
                result.error = "response does not contain data[].url"
            else:
                try:
                    result.image_status, result.image_bytes = fetch_image(
                        image_url, args.image_timeout, args.max_image_bytes
                    )
                    if result.image_status < 200 or result.image_status >= 300:
                        result.error = f"image URL returned HTTP {result.image_status}"
                except Exception as exc:  # URL validation failure belongs to this request.
                    result.error = f"image URL check failed: {exc}"
    except urllib.error.HTTPError as exc:
        result.status = exc.code
        result.error = exc.read(500).decode("utf-8", errors="replace")
    except Exception as exc:
        result.error = f"request failed: {exc}"
    finally:
        result.elapsed_s = time.perf_counter() - started
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Concurrent image generation and image URL load test")
    parser.add_argument("--base-url", default=os.getenv("IMAGE_TEST_BASE_URL", "http://127.0.0.1:31000"))
    parser.add_argument("--endpoint", default="/v1/images/generations", help="API path")
    parser.add_argument("--api-key", default=os.getenv("CHATGPT2API_AUTH_KEY", ""))
    parser.add_argument("-n", "--requests", type=int, default=10, help="total requests")
    parser.add_argument("-c", "--concurrency", type=int, default=1)
    parser.add_argument("--model", default="gpt-image-2.5")
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--prompt", default="A simple red apple on a white background")
    parser.add_argument("--request-timeout", type=float, default=190.0)
    parser.add_argument("--image-timeout", type=float, default=30.0)
    parser.add_argument("--max-image-bytes", type=int, default=50 * 1024 * 1024)
    parser.add_argument("--jsonl", help="write one result per line to this file")
    args = parser.parse_args()

    if args.requests < 1 or args.concurrency < 1:
        parser.error("--requests and --concurrency must be positive")
    if not args.api_key:
        parser.error("missing --api-key or CHATGPT2API_AUTH_KEY")
    args.url = args.base_url.rstrip("/") + "/" + args.endpoint.lstrip("/")
    headers = {"Authorization": f"Bearer {args.api_key}", "Content-Type": "application/json"}

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        results = list(executor.map(lambda i: run_one(i, args, headers), range(1, args.requests + 1)))
    wall = time.perf_counter() - started
    values = [item.elapsed_s for item in results]
    http_ok = sum(item.status is not None and 200 <= item.status < 300 for item in results)
    image_ok = sum(item.image_status is not None and 200 <= item.image_status < 300 for item in results)
    summary = {
        "url": args.url,
        "requests": args.requests,
        "concurrency": args.concurrency,
        "wall_s": round(wall, 3),
        "http_2xx": http_ok,
        "image_url_2xx": image_ok,
        "missing_or_invalid_image_url": args.requests - image_ok,
        "min_s": round(min(values), 3),
        "avg_s": round(statistics.mean(values), 3),
        "p50_s": round(percentile(values, 0.50), 3),
        "p95_s": round(percentile(values, 0.95), 3),
        "max_s": round(max(values), 3),
        "over_140s": sum(value > 140 for value in values),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for item in results:
        if item.error:
            print(json.dumps({"result": asdict(item)}, ensure_ascii=False), file=sys.stderr)
    if args.jsonl:
        with open(args.jsonl, "w", encoding="utf-8") as output:
            for item in results:
                output.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")
    return 0 if http_ok == args.requests and image_ok == args.requests else 1


if __name__ == "__main__":
    raise SystemExit(main())
