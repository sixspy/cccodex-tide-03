"""数据层：去重设置、拒绝/覆盖、不符停下、空时刻、重复列表与详情。"""
from datetime import date

from support import SEED_DAY, TODAY, check, finish, reset_db
import db
from db import (
    KIND_CONTENTION,
    KIND_MISMATCH,
    POLICY_OVERWRITE,
    POLICY_REJECT,
    REASON_CONTENTION,
    REASON_MISMATCH,
    DedupMismatch,
    connect,
    duplicate_detail,
    get_setting,
    insert_batch,
    list_duplicates,
    update_setting,
)

failures = []
reset_db()

# 1. 种子数据：拒绝策略
with connect() as conn:
    setting = get_setting(conn, SEED_DAY)
    check("种子日设置为拒绝", setting["policy"] == POLICY_REJECT and setting["locked"], failures)
    batches = conn.execute("SELECT COUNT(*) AS n FROM intake_batches").fetchone()["n"]
    check("种子两批次", batches == 2, failures)
    wg = [d for d in list_duplicates(conn) if d["station_code"] == "WG01"]
    check("种子重复列表含 WG01 拒绝", len(wg) == 1 and wg[0]["handling"] == "拒绝重复", failures)

# 2. 当日首次归集声明策略并锁定
with connect() as conn:
    insert_batch(conn, "午后 A",
                 "ST01|202611140900|100|A\nST01|202611141000|110|A",
                 "admin", POLICY_OVERWRITE, TODAY)
    conn.commit()
    check("首条声明覆盖并锁定",
          get_setting(conn, TODAY)["policy"] == POLICY_OVERWRITE, failures)

# 3. 再来报文：覆盖厘米并留原值
with connect() as conn:
    bid2 = insert_batch(conn, "午后 B 覆盖",
                        "ST01|202611140900|125|M\nST02|202611140900|200|A",
                        "admin", POLICY_OVERWRITE, TODAY)
    conn.commit()
    row = conn.execute(
        "SELECT tide_cm, quality_flag, batch_id FROM landed_rows "
        "WHERE station_code='ST01' AND observed_at='2026-11-14 09:00'"
    ).fetchone()
    check("厘米被覆盖为125",
          row["tide_cm"] == 125 and row["quality_flag"] == "M" and row["batch_id"] == bid2,
          failures)
    hist = conn.execute("SELECT old_tide_cm, new_tide_cm FROM overwrite_history").fetchall()
    check("留痕保留覆盖前原值100",
          len(hist) == 1 and hist[0]["old_tide_cm"] == 100 and hist[0]["new_tide_cm"] == 125,
          failures)
    check("批次覆盖次数=1",
          conn.execute("SELECT overwrite_count FROM intake_batches WHERE id=%s",
                       (bid2,)).fetchone()["overwrite_count"] == 1, failures)

# 4. 请求策略与当日设置不符：写不符并停下
with connect() as conn:
    mismatch_id = None
    raised = False
    try:
        insert_batch(conn, "午后 C 不符",
                     "ST01|202611140900|130|A\nST99|202611140900|300|A",
                     "admin", POLICY_REJECT, TODAY)
    except DedupMismatch as exc:
        raised = True
        mismatch_id = exc.batch_id
    conn.commit()
    check("不符抛 DedupMismatch", raised, failures)
    rj = conn.execute("SELECT reason, kind FROM reject_rows WHERE batch_id=%s",
                      (mismatch_id,)).fetchall()
    check("不符批次只落一条处理不符",
          len(rj) == 1 and rj[0]["kind"] == KIND_MISMATCH and rj[0]["reason"] == REASON_MISMATCH,
          failures)
    check("停下后后续行未处理",
          conn.execute("SELECT 1 FROM landed_rows WHERE station_code='ST99'").fetchone() is None,
          failures)
    bb = conn.execute("SELECT status, rejected_count, accepted_count FROM intake_batches WHERE id=%s",
                      (mismatch_id,)).fetchone()
    check("不符批次状态未接纳",
          bb["status"] == "未接纳" and bb["rejected_count"] == 1 and bb["accepted_count"] == 0,
          failures)

# 5. 当日禁止换策略
with connect() as conn:
    result, _ = update_setting(conn, TODAY, POLICY_REJECT, "admin")
    conn.commit()
    check("已锁定日改设置被拒", result == "locked", failures)
    result_same, row = update_setting(conn, TODAY, POLICY_OVERWRITE, "admin")
    conn.commit()
    check("同值重复提交不算换策略",
          result_same == "ok" and row["policy"] == POLICY_OVERWRITE, failures)

# 6. 重复列表分拒绝/覆盖，时刻详情对照新旧厘米
with connect() as conn:
    st01 = [d for d in list_duplicates(conn)
            if d["station_code"] == "ST01" and str(d["observed_at"]).startswith("2026-11-14 09:00")][0]
    check("重复列表字段",
          st01["overwrite_count"] == 1 and st01["handling"] == "覆盖厘米"
          and st01["section"] == "overwrite", failures)
    detail = duplicate_detail(conn, "ST01", st01["observed_at"])
    check("时刻详情对照新旧厘米",
          detail["landed"]["tide_cm"] == 125 and detail["history"][0]["old_tide_cm"] == 100,
          failures)

# 7. 拒绝模式日：整行留未接纳
day2 = date(2026, 11, 15)
with connect() as conn:
    bidr = insert_batch(conn, "拒绝日", "RR01|202611141100|50|A", "admin", POLICY_REJECT, day2)
    conn.commit()
with connect() as conn:
    bidr2 = insert_batch(conn, "拒绝日重复", "RR01|202611141100|60|A",
                         "admin", POLICY_REJECT, day2)
    conn.commit()
    check("拒绝模式厘米不变",
          conn.execute("SELECT tide_cm FROM landed_rows WHERE station_code='RR01'").fetchone()[
              "tide_cm"] == 50, failures)
    rj = conn.execute("SELECT kind FROM reject_rows WHERE batch_id=%s", (bidr2,)).fetchall()
    check("重复整行留未接纳", len(rj) == 1 and rj[0]["kind"] == "reject", failures)

# 8. 空时刻不参与去重
day3 = date(2026, 11, 16)
with connect() as conn:
    bide = insert_batch(conn, "空时刻", "EE01||77|A\nEE01|202611141200|88|A",
                        "admin", POLICY_OVERWRITE, day3)
    conn.commit()
    rj = conn.execute(
        "SELECT kind, reason, observed_at FROM reject_rows WHERE batch_id=%s", (bide,)
    ).fetchall()
    check("空时刻进未接纳且不带时刻",
          len(rj) == 1 and rj[0]["observed_at"] is None and "空" in rj[0]["reason"], failures)
    check("空时刻不进重复列表",
          all(d["station_code"] != "EE01" for d in list_duplicates(conn)), failures)

# 9. 确定性时刻争用：持锁期间另一批只记争用，不写入
import threading
import time
from datetime import datetime

reset_db()
# 先固化当日设置并提交，后续并发都走共享门闩，不再经过首批固化排他锁
with connect() as conn:
    insert_batch(conn, "占位首批", "PP01|202611140800|1|A",
                 "admin", POLICY_OVERWRITE, TODAY)
    conn.commit()
hold = threading.Event()


def slow_holds_lock():
    c = connect()
    db._lock_today_setting(c, TODAY, POLICY_OVERWRITE, "admin")
    high, low = db._advisory_key("CC01", datetime(2026, 11, 14, 9, 0))
    c.execute("SELECT pg_advisory_xact_lock(%s,%s)", (high, low))
    # 持锁期间真实写入这一站时刻，模拟处理到一半的慢批次
    c.execute(
        """INSERT INTO intake_batches (title, raw_text, status, accepted_count,
               rejected_count, overwrite_count, created_by)
           VALUES ('慢批','CC01|202611140900|101|A','已归集',1,0,0,'admin') RETURNING id"""
    )
    c.execute(
        """INSERT INTO landed_rows (batch_id, line_no, station_code, observed_at,
               tide_cm, quality_flag)
           SELECT id,1,'CC01','2026-11-14 09:00',101,'A' FROM intake_batches
           WHERE title='慢批'"""
    )
    hold.set()
    time.sleep(1.2)
    c.commit()
    c.close()


th = threading.Thread(target=slow_holds_lock)
th.start()
hold.wait()
with connect() as conn:
    insert_batch(conn, "撞锁批", "CC01|202611140900|102|A\nKK01|202611140900|1|A",
                 "admin", POLICY_OVERWRITE, TODAY)
    conn.commit()
th.join()
with connect() as conn:
    check("同一站时刻只写入一批",
          len(conn.execute("SELECT tide_cm FROM landed_rows WHERE station_code='CC01'").fetchall()) == 1,
          failures)
    contention = conn.execute("SELECT kind, reason FROM reject_rows WHERE kind=%s",
                              (KIND_CONTENTION,)).fetchall()
    check("另一批记时刻争用",
          len(contention) == 1 and contention[0]["reason"] == REASON_CONTENTION, failures)
    cc = [d for d in list_duplicates(conn) if d["station_code"] == "CC01"]
    check("争用进重复列表", len(cc) == 1 and cc[0]["handling"] == "时刻争用", failures)
    check("同批其它行照常落库",
          conn.execute("SELECT 1 FROM landed_rows WHERE station_code='KK01'").fetchone() is not None,
          failures)

finish(failures)
