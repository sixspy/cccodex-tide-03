import os
import time
from datetime import date

import psycopg
from psycopg.rows import dict_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS intake_batches (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    status TEXT NOT NULL,
    accepted_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    overwrite_count INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS landed_rows (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    line_no INTEGER NOT NULL,
    station_code TEXT NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    tide_cm INTEGER NOT NULL,
    quality_flag TEXT NOT NULL,
    UNIQUE (station_code, observed_at)
);

CREATE TABLE IF NOT EXISTS reject_rows (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    line_no INTEGER NOT NULL,
    station_code TEXT,
    observed_at TIMESTAMP,
    raw_line TEXT NOT NULL,
    reason TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'reject',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dedup_settings (
    setting_date DATE PRIMARY KEY,
    policy TEXT NOT NULL,
    locked BOOLEAN NOT NULL DEFAULT TRUE,
    updated_by TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS overwrite_history (
    id BIGSERIAL PRIMARY KEY,
    station_code TEXT NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    line_no INTEGER NOT NULL,
    old_tide_cm INTEGER NOT NULL,
    new_tide_cm INTEGER NOT NULL,
    quality_flag TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reject_station_time
    ON reject_rows (station_code, observed_at);
CREATE INDEX IF NOT EXISTS idx_overwrite_station_time
    ON overwrite_history (station_code, observed_at);
"""

# 拒绝行的类别，页面据此分出拒绝与覆盖两个区
KIND_REJECT = "reject"          # 普通未接纳（解析失败、重复拒绝、同批重复、空时刻）
KIND_CONTENTION = "contention"  # 两批同时撞同一站同一时刻，锁被另一批拿走
KIND_MISMATCH = "mismatch"      # 请求里的处理方式与当日去重设置对不上

REASON_EMPTY_TIME = "观测时刻空着，不参与去重"
REASON_INTRA_DUP = "同一批里观测时刻重复"
REASON_DUP_REJECT = "这个站这个时刻已经落过库，按当日设置整行留在未接纳"
REASON_CONTENTION = "时刻争用：另一批报文正在写同一站同一时刻"
REASON_MISMATCH = "时刻处理不符：请求处理方式与当日去重设置不一致"

POLICY_REJECT = "reject"
POLICY_OVERWRITE = "overwrite"
POLICIES = {POLICY_REJECT: "拒绝重复", POLICY_OVERWRITE: "覆盖厘米"}

SEED_CLEAN = """吴淞口 11-12 早班
WG01|202611120830|186|A
WG01|202611120900|191|A
BG02|202611120830|142|M
"""

SEED_BROKEN = """外高桥 11-12 错报
LG04|202611121000|205|A
WG01|202611120830|186|A
WG0X|202611121000|10|A
"""

SEED_DATE = date(2026, 11, 12)


class DedupMismatch(Exception):
    """请求里的处理方式与当日去重设置对不上，本批停下。"""

    def __init__(self, message: str, batch_id: int | None = None):
        super().__init__(message)
        self.batch_id = batch_id


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def _column_names(conn, table: str):
    rows = conn.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_name = %s
        """,
        (table,),
    ).fetchall()
    return {row["column_name"] for row in rows}


def _migrate(conn):
    """给既有库补列，CREATE TABLE IF NOT EXISTS 不会改老表。"""
    reject_cols = _column_names(conn, "reject_rows")
    if "kind" not in reject_cols:
        conn.execute("ALTER TABLE reject_rows ADD COLUMN kind TEXT NOT NULL DEFAULT 'reject'")
    if "station_code" not in reject_cols:
        conn.execute("ALTER TABLE reject_rows ADD COLUMN station_code TEXT")
    if "observed_at" not in reject_cols:
        conn.execute("ALTER TABLE reject_rows ADD COLUMN observed_at TIMESTAMP")
    if "created_at" not in reject_cols:
        conn.execute(
            "ALTER TABLE reject_rows ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
        )
    batch_cols = _column_names(conn, "intake_batches")
    if "overwrite_count" not in batch_cols:
        conn.execute(
            "ALTER TABLE intake_batches ADD COLUMN overwrite_count INTEGER NOT NULL DEFAULT 0"
        )


def init_db():
    last_error = None
    for _ in range(30):
        try:
            with connect() as conn:
                conn.execute(SCHEMA)
                _migrate(conn)
                count = conn.execute("SELECT COUNT(*) AS n FROM intake_batches").fetchone()["n"]
                if count == 0:
                    _seed(conn)
                conn.commit()
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1)
    raise last_error


def _advisory_key(station_code: str, observed_at):
    """把站码+时刻折成双参数咨询锁用的两个有符号 32 位整数。"""
    import hashlib

    digest = hashlib.blake2b(
        f"{station_code}|{observed_at.isoformat()}".encode(), digest_size=8
    ).digest()
    high = int.from_bytes(digest[:4], "big", signed=True)
    low = int.from_bytes(digest[4:], "big", signed=True)
    return high, low


def _setting_lock_key(today: date):
    """当日设置门闩：单 bigint 咨询锁，批次共享、设置修改排他。"""
    import hashlib

    digest = hashlib.blake2b(f"dedup-setting|{today.isoformat()}".encode(),
                             digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _fixation_lock_key(today: date):
    """新一日策略固化锁：只在当日还没有设置行时短暂使用。"""
    import hashlib

    digest = hashlib.blake2b(f"dedup-fixation|{today.isoformat()}".encode(),
                             digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _lock_today_setting(conn, today: date, policy: str, username: str):
    """
    返回当日生效策略，并保证请求策略与其一致（不一致由调用方处理）。
    - 共享门闩：批次之间相容，可真正并发，站时刻争用仍由各站时刻锁裁决；
      与设置修改的排他锁互斥，覆盖进行中改不了去重设置
    - 固化排他锁：仅当日尚无设置行时短暂持有，串行化“首批声明”，
      避免两个不同策略并发 INSERT 触发主键冲突
    """
    key = _setting_lock_key(today)
    conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (key,))
    row = conn.execute(
        "SELECT policy FROM dedup_settings WHERE setting_date = %s",
        (today,),
    ).fetchone()
    if row is None:
        fix_key = _fixation_lock_key(today)
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (fix_key,))
        # 双检：等锁期间别的首批可能已经固化
        row = conn.execute(
            "SELECT policy FROM dedup_settings WHERE setting_date = %s",
            (today,),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO dedup_settings (setting_date, policy, locked, updated_by)
                VALUES (%s, %s, TRUE, %s)
                """,
                (today, policy, username),
            )
            row = {"policy": policy}
    return row["policy"]


def get_setting(conn, today: date):
    row = conn.execute(
        "SELECT setting_date, policy, locked, updated_by, updated_at FROM dedup_settings WHERE setting_date = %s",
        (today,),
    ).fetchone()
    return row


def update_setting(conn, today: date, policy: str, username: str):
    """
    改当日去重设置。
    - 覆盖进行中（在途批次持有当日门闩共享锁）→ 排他锁试取失败，立刻 busy
    - 当日已锁定且要换成另一套处理 → locked
    锁序与批次一致（先设置门闩），设置侧不碰站时刻咨询锁，无死锁环。
    """
    key = _setting_lock_key(today)
    got_gate = conn.execute(
        "SELECT pg_try_advisory_xact_lock(%s) AS ok", (key,)
    ).fetchone()["ok"]
    if not got_gate:
        return "busy", get_setting(conn, today)

    row = get_setting(conn, today)
    if row is None:
        conn.execute(
            """
            INSERT INTO dedup_settings (setting_date, policy, locked, updated_by)
            VALUES (%s, %s, TRUE, %s)
            """,
            (today, policy, username),
        )
        row = {"policy": policy, "locked": True}
    elif row["locked"] and row["policy"] != policy:
        return "locked", row

    conn.execute(
        """
        UPDATE dedup_settings
        SET policy = %s, locked = TRUE, updated_by = %s, updated_at = NOW()
        WHERE setting_date = %s
        """,
        (policy, username, today),
    )
    return "ok", get_setting(conn, today)


def _insert_reject(conn, batch_id, line_no, raw_line, reason, kind,
                   station_code=None, observed_at=None):
    conn.execute(
        """
        INSERT INTO reject_rows
            (batch_id, line_no, station_code, observed_at, raw_line, reason, kind)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (batch_id, line_no, station_code, observed_at, raw_line, reason, kind),
    )


def insert_batch(conn, title: str, raw_text: str, created_by: str,
                 policy: str, today: date):
    """
    服务端去重裁决。调用方负责 commit/rollback。
    策略与当日设置不符时抛 DedupMismatch，并已写入一条时刻处理不符的拒绝行。
    """
    from parser import parse_telegram_line

    if policy not in POLICIES:
        raise ValueError("处理方式只能是 reject 或 overwrite")

    # 锁序第一步：设置行锁。同刻并发的批次在此排队，看到同一份当日策略；
    # 设置修改也拿这把行锁，因此覆盖进行中改不了去重设置。
    effective_policy = _lock_today_setting(conn, today, policy, created_by)
    if effective_policy != policy:
        # 处理方式与当日去重设置对不上：写时刻处理不符并停下。
        # 先落一条未接纳批次和不符记录，交回调用方提交，整批不再处理后续行。
        batch = conn.execute(
            """
            INSERT INTO intake_batches
                (title, raw_text, status, accepted_count, rejected_count,
                 overwrite_count, created_by)
            VALUES (%s, %s, '未接纳', 0, 1, 0, %s)
            RETURNING id
            """,
            (title, raw_text, created_by),
        ).fetchone()
        first_line = next((ln.strip() for ln in raw_text.splitlines() if ln.strip()), "")
        _insert_reject(conn, batch["id"], 1, first_line,
                       REASON_MISMATCH, KIND_MISMATCH)
        raise DedupMismatch(REASON_MISMATCH, batch["id"])

    batch = conn.execute(
        """
        INSERT INTO intake_batches
            (title, raw_text, status, accepted_count, rejected_count,
             overwrite_count, created_by)
        VALUES (%s, %s, '处理中', 0, 0, 0, %s)
        RETURNING id
        """,
        (title, raw_text, created_by),
    ).fetchone()
    batch_id = batch["id"]

    accepted = 0
    rejected = 0
    overwritten = 0
    seen = set()

    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        parsed, reason = parse_telegram_line(raw_line)
        if reason:
            _insert_reject(conn, batch_id, line_no, raw_line.strip(), reason, KIND_REJECT)
            rejected += 1
            continue

        station_code = parsed["station_code"]
        observed_at = parsed["observed_at"]

        # 观测时刻空着不能参与去重：解析阶段已挡，这里再兜底一次
        if not observed_at:
            _insert_reject(conn, batch_id, line_no, raw_line.strip(),
                           REASON_EMPTY_TIME, KIND_REJECT,
                           station_code=station_code, observed_at=None)
            rejected += 1
            continue

        key = (station_code, observed_at)
        high, low = _advisory_key(station_code, observed_at)
        got_lock = conn.execute(
            "SELECT pg_try_advisory_xact_lock(%s, %s) AS ok", (high, low)
        ).fetchone()["ok"]
        if not got_lock:
            # 两批同时撞上同一站同一时刻：锁被另一批拿走，本批记时刻争用
            _insert_reject(conn, batch_id, line_no, raw_line.strip(),
                           REASON_CONTENTION, KIND_CONTENTION,
                           station_code=station_code, observed_at=observed_at)
            rejected += 1
            continue

        if key in seen:
            _insert_reject(conn, batch_id, line_no, raw_line.strip(),
                           REASON_INTRA_DUP, KIND_REJECT,
                           station_code=station_code, observed_at=observed_at)
            rejected += 1
            continue

        landed = conn.execute(
            "SELECT id, tide_cm FROM landed_rows WHERE station_code = %s AND observed_at = %s",
            key,
        ).fetchone()

        if landed:
            if effective_policy == POLICY_REJECT:
                _insert_reject(conn, batch_id, line_no, raw_line.strip(),
                               REASON_DUP_REJECT, KIND_REJECT,
                               station_code=station_code, observed_at=observed_at)
                rejected += 1
                continue
            # 覆盖厘米：详情里留下覆盖前的原值
            conn.execute(
                """
                INSERT INTO overwrite_history
                    (station_code, observed_at, batch_id, line_no,
                     old_tide_cm, new_tide_cm, quality_flag)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (station_code, observed_at, batch_id, line_no,
                 landed["tide_cm"], parsed["tide_cm"], parsed["quality_flag"]),
            )
            conn.execute(
                "UPDATE landed_rows SET tide_cm = %s, quality_flag = %s, batch_id = %s, line_no = %s WHERE id = %s",
                (parsed["tide_cm"], parsed["quality_flag"],
                 batch_id, line_no, landed["id"]),
            )
            overwritten += 1
        else:
            conn.execute(
                """
                INSERT INTO landed_rows
                    (batch_id, line_no, station_code, observed_at, tide_cm, quality_flag)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (batch_id, line_no, station_code, observed_at,
                 parsed["tide_cm"], parsed["quality_flag"]),
            )
            accepted += 1
        seen.add(key)

    if rejected == 0:
        status = "已归集"
    elif accepted or overwritten:
        status = "部分接纳"
    else:
        status = "未接纳"
    conn.execute(
        """
        UPDATE intake_batches
        SET status = %s, accepted_count = %s, rejected_count = %s, overwrite_count = %s
        WHERE id = %s
        """,
        (status, accepted, rejected, overwritten, batch_id),
    )
    return batch_id


def list_duplicates(conn):
    """
    重复列表：每个站码+时刻一行，给出覆盖次数和最新处理名。
    处理名：拒绝重复 / 覆盖厘米 / 时刻争用 / 时刻处理不符。
    """
    rows = conn.execute(
        """
        WITH marks AS (
            SELECT station_code, observed_at,
                   reason, kind, 1 AS is_reject, 0 AS is_overwrite,
                   created_at AS at FROM reject_rows
            WHERE station_code IS NOT NULL AND observed_at IS NOT NULL
            UNION ALL
            SELECT station_code, observed_at,
                   NULL AS reason, 'overwrite' AS kind, 0, 1,
                   created_at FROM overwrite_history
        )
        SELECT station_code,
               observed_at,
               SUM(is_overwrite) AS overwrite_count,
               COUNT(*) FILTER (WHERE is_reject = 1) AS reject_count,
               (ARRAY_AGG(kind ORDER BY at DESC))[1] AS last_kind,
               (ARRAY_AGG(reason ORDER BY at DESC))[1] AS last_reason,
               MAX(at) AS last_at
        FROM marks
        GROUP BY station_code, observed_at
        ORDER BY MAX(at) DESC, station_code, observed_at
        """
    ).fetchall()
    kind_label = {
        KIND_REJECT: "拒绝重复",
        KIND_CONTENTION: "时刻争用",
        KIND_MISMATCH: "时刻处理不符",
        "overwrite": "覆盖厘米",
    }
    for row in rows:
        row["handling"] = kind_label.get(row["last_kind"], row["last_kind"])
        # 争用/不符的行本身不算"拒绝重复"处理，但页面仍归在未接纳一侧
        row["section"] = "overwrite" if row["last_kind"] == "overwrite" else "reject"
    return rows


def duplicate_detail(conn, station_code: str, observed_at):
    """时刻详情：新旧厘米对照 + 每次覆盖留痕 + 全部未接纳行。"""
    landed = conn.execute(
        """
        SELECT id, batch_id, line_no, tide_cm, quality_flag
        FROM landed_rows WHERE station_code = %s AND observed_at = %s
        """,
        (station_code, observed_at),
    ).fetchone()
    history = conn.execute(
        """
        SELECT id, batch_id, line_no, old_tide_cm, new_tide_cm,
               quality_flag, created_at
        FROM overwrite_history
        WHERE station_code = %s AND observed_at = %s
        ORDER BY id
        """,
        (station_code, observed_at),
    ).fetchall()
    rejects = conn.execute(
        """
        SELECT id, batch_id, line_no, raw_line, reason, kind, created_at
        FROM reject_rows
        WHERE station_code = %s AND observed_at = %s
        ORDER BY id
        """,
        (station_code, observed_at),
    ).fetchall()
    return {"landed": landed, "history": history, "rejects": rejects}


def _seed(conn):
    # 种子日固定走拒绝策略，保持与初始快照相同的去重行为
    conn.execute(
        """
        INSERT INTO dedup_settings (setting_date, policy, locked, updated_by)
        VALUES (%s, %s, TRUE, 'admin')
        ON CONFLICT (setting_date) DO NOTHING
        """,
        (SEED_DATE, POLICY_REJECT),
    )
    title, _, body = SEED_CLEAN.partition("\n")
    insert_batch(conn, title, body.strip(), "admin", POLICY_REJECT, SEED_DATE)
    title, _, body = SEED_BROKEN.partition("\n")
    insert_batch(conn, title, body.strip(), "admin", POLICY_REJECT, SEED_DATE)
