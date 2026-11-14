"""后端裁决测试的公共夹具：DATABASE_URL 指向测试库，每个用例重置表结构。"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_DSN = "postgresql://app@/tidetest?host=/tmp/pgsock&port=55432"
os.environ.setdefault("DATABASE_URL", DEFAULT_DSN)

import db  # noqa: E402

TODAY = date(2026, 11, 14)
SEED_DAY = date(2026, 11, 12)


def reset_db():
    with db.connect() as conn:
        conn.execute(
            "DROP TABLE IF EXISTS overwrite_history, reject_rows, landed_rows, "
            "dedup_settings, intake_batches CASCADE"
        )
        conn.commit()
    db.init_db()


def check(name, cond, failures, extra=""):
    print(("PASS" if cond else "FAIL"), name, extra)
    if not cond:
        failures.append(name)


def finish(failures):
    if failures:
        print(f"{len(failures)} 项失败：{failures}")
        raise SystemExit(1)
    print("全部通过")
