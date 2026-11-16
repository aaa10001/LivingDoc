#!/usr/bin/env python
"""LivingDoc 只读/投递混合压测（不依赖 locust，避免再装一套运行时）。

为什么自己写：
- 项目里已经有 httpx，加 locust 只会让演示环境更重；
- 我们要压的不是"任意 HTTP"，而是评审链路：项目列表、文档详情、版本、diff、
  以及并发 publish（这条会真的写 publish_records 与知识库文件）；
- 需要能在容器外对 compose 里的 PostgreSQL 施加并发。

用法（本机 SQLite）：
    .\.venv\Scripts\python.exe scripts\load_test.py --requests 200 --concurrency 10

用法（compose 里的 PostgreSQL）：
    python scripts/load_test.py --base-url http://127.0.0.1:8000 \
        --requests 500 --concurrency 25 --mode mixed --publish-target local_wiki

判定：
- 非 2xx 或连接错误都计为失败；
- --max-error-rate 默认 0（任何失败都返回非零），压测时请显式放宽；
- --json-out 把原始统计写到文件，便于贴进报告。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

READ_PATHS = ("projects", "documents", "detail", "revisions", "diff")


@dataclass
class Sample:
    endpoint: str
    status: int
    latency_ms: float
    error: str | None = None


@dataclass
class Stats:
    samples: list[Sample] = field(default_factory=list)

    def add(self, sample: Sample) -> None:
        self.samples.append(sample)

    @property
    def failures(self) -> list[Sample]:
        return [s for s in self.samples if s.error or not 200 <= s.status < 300]

    def summary(self) -> dict:
        ok = [s for s in self.samples if not s.error and 200 <= s.status < 300]
        latencies = sorted(s.latency_ms for s in ok) or [0.0]
        by_endpoint: dict[str, dict] = {}
        for sample in self.samples:
            bucket = by_endpoint.setdefault(
                sample.endpoint, {"count": 0, "errors": 0, "latencies": []}
            )
            bucket["count"] += 1
            if sample.error or not 200 <= sample.status < 300:
                bucket["errors"] += 1
            else:
                bucket["latencies"].append(sample.latency_ms)
        for bucket in by_endpoint.values():
            bucket["p50_ms"] = round(_percentile(bucket["latencies"], 50), 2)
            bucket["p95_ms"] = round(_percentile(bucket["latencies"], 95), 2)
            bucket.pop("latencies")
        return {
            "requests": len(self.samples),
            "ok": len(ok),
            "failed": len(self.failures),
            "error_rate": round(len(self.failures) / max(len(self.samples), 1), 4),
            "p50_ms": round(_percentile(latencies, 50), 2),
            "p95_ms": round(_percentile(latencies, 95), 2),
            "p99_ms": round(_percentile(latencies, 99), 2),
            "max_ms": round(max(latencies), 2),
            "by_endpoint": by_endpoint,
        }


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    index = min(len(values) - 1, max(0, int(round((pct / 100) * (len(values) - 1)))))
    return values[index]


async def discover(client: httpx.AsyncClient, project_id: str | None, document_id: str | None) -> dict:
    """找一组真实存在的项目/文档；压测必须打在真实数据上，不造假数据。"""
    if document_id:
        detail = (await client.get(f"/api/v1/documents/{document_id}")).raise_for_status().json()
        return {"project_id": detail["project_id"], "document_id": detail["id"], "title": detail["title"]}

    projects = (await client.get("/api/v1/projects")).raise_for_status().json()
    if project_id:
        projects = [p for p in projects if p["id"] == project_id]
    if not projects:
        raise SystemExit(
            "没有可用项目。先触发一次 webhook 生成文档，或传 --project-id / --document-id。"
        )
    for project in projects:
        docs = (
            await client.get(f"/api/v1/projects/{project['id']}/documents")
        ).raise_for_status().json()
        if docs:
            doc = docs[0]
            return {"project_id": project["id"], "document_id": doc["id"], "title": doc["title"]}
    raise SystemExit("项目里还没有文档；先跑一次分析，或改用 --document-id。")


async def one_request(
    client: httpx.AsyncClient,
    target: dict,
    mode: str,
    publish_target: str | None,
    stats: Stats,
) -> None:
    kind = READ_PATHS[len(stats.samples) % len(READ_PATHS)] if mode == "read" else None
    if mode == "mixed":
        kind = READ_PATHS[len(stats.samples) % len(READ_PATHS)]
        if len(stats.samples) % 2 == 0:
            kind = "publish"
    if mode == "publish":
        kind = "publish"

    started = time.perf_counter()
    try:
        if kind == "projects":
            response = await client.get("/api/v1/projects")
        elif kind == "documents":
            response = await client.get(f"/api/v1/projects/{target['project_id']}/documents")
        elif kind == "detail":
            response = await client.get(f"/api/v1/documents/{target['document_id']}")
        elif kind == "revisions":
            response = await client.get(f"/api/v1/documents/{target['document_id']}/revisions")
        elif kind == "diff":
            response = await client.get(f"/api/v1/documents/{target['document_id']}/diff")
        else:
            body = {"target": publish_target} if publish_target else {}
            response = await client.post(
                f"/api/v1/documents/{target['document_id']}/publish", json=body
            )
        latency = (time.perf_counter() - started) * 1000
        error = None
        if response.status_code >= 400:
            error = response.text[:200]
        stats.add(Sample(kind or "publish", response.status_code, latency, error))
    except Exception as exc:  # noqa: BLE001 - 压测脚本要把任何异常记成一笔失败
        latency = (time.perf_counter() - started) * 1000
        stats.add(Sample(kind or "publish", 0, latency, f"{type(exc).__name__}: {exc}"))


async def run(args: argparse.Namespace) -> int:
    limits = httpx.Limits(max_connections=args.concurrency * 2, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"), timeout=args.timeout, limits=limits
    ) as client:
        try:
            health = await client.get("/healthz")
            health.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"服务不可用：{args.base_url} ({type(exc).__name__}: {exc})", file=sys.stderr)
            return 2

        target = await discover(client, args.project_id, args.document_id)
        print(f"目标项目：{target['project_id']}")
        print(f"目标文档：{target['document_id']} · {target.get('title', '')}")
        print(f"模式：{args.mode} · 并发 {args.concurrency} · 请求 {args.requests}")

        for _ in range(min(args.warmup, args.requests)):
            await one_request(client, target, args.mode, args.publish_target, Stats())

        stats = Stats()
        started = time.perf_counter()
        queue: asyncio.Queue[int] = asyncio.Queue()
        for index in range(args.requests):
            queue.put_nowait(index)

        async def worker() -> None:
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await one_request(client, target, args.mode, args.publish_target, stats)
                queue.task_done()

        await asyncio.gather(*(worker() for _ in range(args.concurrency)))
        elapsed = time.perf_counter() - started

    report = stats.summary()
    report["rps"] = round(report["requests"] / elapsed, 2) if elapsed else 0.0
    report["elapsed_s"] = round(elapsed, 3)
    report["base_url"] = args.base_url
    report["mode"] = args.mode

    print("\n=== 结果 ===")
    print(f"RPS            : {report['rps']}")
    print(f"成功 / 失败     : {report['ok']} / {report['failed']}  (error_rate={report['error_rate']:.2%})")
    print(f"延迟 p50/p95/p99: {report['p50_ms']} / {report['p95_ms']} / {report['p99_ms']} ms")
    print(f"最长            : {report['max_ms']} ms")
    print("\n按端点：")
    for name, bucket in sorted(report["by_endpoint"].items()):
        print(
            f"  {name:<10} n={bucket['count']:<4} err={bucket['errors']:<3} "
            f"p50={bucket['p50_ms']}ms p95={bucket['p95_ms']}ms"
        )

    failures = stats.failures[:5]
    if failures:
        print("\n失败样例：")
        for sample in failures:
            print(f"  [{sample.status}] {sample.endpoint} {sample.error or ''}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n统计已写入 {args.json_out}")

    return 0 if report["error_rate"] <= args.max_error_rate else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LivingDoc 评审链路压测")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--mode", choices=("read", "publish", "mixed"), default="read")
    parser.add_argument("--publish-target", default="local_wiki", help="publish 模式下的 target；传空字符串表示按配置")
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--document-id", default=None)
    parser.add_argument("--max-error-rate", type=float, default=0.0)
    parser.add_argument("--json-out", default=None)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.requests < 1 or args.concurrency < 1:
        print("--requests / --concurrency 必须 >= 1", file=sys.stderr)
        return 2
    if not args.publish_target:
        args.publish_target = None
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())