"""容器启动时的建库脚本：等数据库就绪 -> 建表 -> 退出。

为什么需要单独一步，而不是让 API 在 startup 里建表：
    api 与 worker 是同一镜像的两个进程，compose 里谁先起来并不确定。
    两个进程同时执行 metadata.create_all() 时，PostgreSQL 上会撞
    "duplicate key value violates unique constraint pg_type_typname_nsp_index"
    （并发 CREATE TABLE 的典型报错）。让两边都在启动命令里先跑这个脚本，
    建表就被串行化了；create_all 本身带 checkfirst，重复执行是安全的。
"""

from __future__ import annotations

import sys
import time

from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.store import open_store

RETRIES = 30
DELAY_SECONDS = 2.0


def main() -> int:
    settings = get_settings()
    settings.ensure_dirs()
    print(f"[init_db] database_url = {settings.database_url}", flush=True)

    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            store = open_store()  # 内部 create_schema()
        except OperationalError as exc:  # 数据库还没起来
            last_error = exc
            print(f"[init_db] waiting for database ({attempt}/{RETRIES})", flush=True)
            time.sleep(DELAY_SECONDS)
            continue
        try:
            print("[init_db] schema ready: projects, analysis_runs, documents, "
                  "document_revisions, ai_invocations", flush=True)
        finally:
            store.close()
        return 0

    print(f"[init_db] database not reachable: {last_error}", file=sys.stderr, flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
