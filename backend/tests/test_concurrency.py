"""并发：多批同刻、共享门闩、策略固化、设置 busy、确定性时刻争用。"""
import threading
import time
from datetime import date, datetime

from support import TODAY, check, finish, reset_db
import db
from db import (
    KIND_CONTENTION,
    POLICY_OVERWRITE,
    POLICY_REJECT,
    DedupMismatch,
    connect,
    get_setting,
    insert_batch,
    update_setting,
)

failures = []
reset_db()

N = 8
results = []
barrier = threading.Barrier(N)


def worker(i):
    c = connect()
    barrier.wait()
    try:
        raw = (f"ZZ01|202611140900|{100 + i}|A\n"
               f"QQ{i:02d}|202611141000|{200 + i}|A")
        bid = insert_batch(c, f"并发批{i}", raw, "admin", POLICY_OVERWRITE, TODAY)
        c.commit()
        results.append(("ok", i, bid))
    except Exception as exc:  # noqa: BLE001
        c.rollback()
        results.append(("err", i, repr(exc)))
    finally:
        c.close()


for t in [threading.Thread(target=worker, args=(i,)) for i in range(N)]:
    t.start()
for t in threading.enumerate():
    if t is not threading.current_thread():
        t.join(30)

check("8 个并发批次全部返回", len(results) == N, failures, str(results))
check("无异常批次", all(r[0] == "ok" for r in results), failures)

with connect() as c:
    independent = c.execute(
        "SELECT COUNT(*) AS n FROM landed_rows WHERE station_code LIKE 'QQ__'"
    ).fetchone()["n"]
    check("N 个独立时刻全部落库", independent == N, failures, str(independent))
    winner = c.execute(
        "SELECT tide_cm FROM landed_rows WHERE station_code='ZZ01' "
        "AND observed_at='2026-11-14 09:00'"
    ).fetchone()
    check("争用时刻只保留一个值",
          winner is not None and 100 <= winner["tide_cm"] <= 100 + N - 1, failures)
    setting = get_setting(c, TODAY)
    check("策略固化为覆盖",
          setting["policy"] == POLICY_OVERWRITE and setting["locked"] is True, failures)

# 确定性时刻争用
hold = threading.Event()


def slow_holds_station_lock():
    c = connect()
    db._lock_today_setting(c, TODAY, POLICY_OVERWRITE, "admin")
    high, low = db._advisory_key("ZZ01", datetime(2026, 11, 14, 9, 0))
    c.execute("SELECT pg_advisory_xact_lock(%s, %s)", (high, low))
    hold.set()
    time.sleep(1.2)
    c.rollback()
    c.close()


th = threading.Thread(target=slow_holds_station_lock)
th.start()
hold.wait()
with connect() as c:
    bid = insert_batch(c, "撞锁快批",
                       "ZZ01|202611140900|999|A\nKK01|202611140900|1|A",
                       "admin", POLICY_OVERWRITE, TODAY)
    c.commit()
th.join()
with connect() as c:
    contention = c.execute(
        "SELECT reason FROM reject_rows WHERE batch_id=%s AND kind=%s",
        (bid, KIND_CONTENTION),
    ).fetchall()
    check("撞锁批记时刻争用", len(contention) == 1, failures, str(contention))
    check("同批其它行照常落库",
          c.execute("SELECT 1 FROM landed_rows WHERE station_code='KK01'").fetchone() is not None,
          failures)
    check("争用时刻未被快批覆盖",
          c.execute("SELECT tide_cm FROM landed_rows WHERE station_code='ZZ01' "
                    "AND observed_at='2026-11-14 09:00'").fetchone()["tide_cm"] != 999, failures)

# 在途批次期间改设置 → busy
ev = threading.Event()


def slow_batch():
    c = connect()
    db._lock_today_setting(c, TODAY, POLICY_OVERWRITE, "admin")
    ev.set()
    time.sleep(1.2)
    c.rollback()
    c.close()


th = threading.Thread(target=slow_batch)
th.start()
ev.wait()
with connect() as c:
    result, _ = update_setting(c, TODAY, POLICY_REJECT, "admin")
    c.commit()
    check("在途期间改设置 busy", result == "busy", failures)
th.join()
with connect() as c:
    result, _ = update_setting(c, TODAY, POLICY_REJECT, "admin")
    c.commit()
    check("锁定日换策略 locked", result == "locked", failures)

# 并发首批：不同策略抢一个全新日期，只固化一套，另一批不符停下
new_day = date(2026, 11, 20)
b2 = threading.Barrier(2)
wins = []


def first_batch(pol):
    c = connect()
    b2.wait()
    try:
        insert_batch(c, f"首批-{pol}", "QA01|202611200900|1|A", "admin", pol, new_day)
        c.commit()
        wins.append(pol)
    except DedupMismatch:
        c.commit()
        wins.append("mismatch")
    finally:
        c.close()


t1 = threading.Thread(target=first_batch, args=(POLICY_REJECT,))
t2 = threading.Thread(target=first_batch, args=(POLICY_OVERWRITE,))
t1.start(); t2.start(); t1.join(20); t2.join(20)
with connect() as c:
    fixed = get_setting(c, new_day)
    check("新日期只固化一套策略", fixed["policy"] in (POLICY_REJECT, POLICY_OVERWRITE), failures)
    check("另一批以不符停下", "mismatch" in wins, failures, str(wins))

finish(failures)
