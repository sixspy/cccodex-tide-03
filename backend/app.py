from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from auth import authenticate, issue_token, read_user
from db import (
    KIND_CONTENTION,
    KIND_MISMATCH,
    KIND_REJECT,
    POLICIES,
    POLICY_OVERWRITE,
    POLICY_REJECT,
    REASON_MISMATCH,
    DedupMismatch,
    connect,
    duplicate_detail,
    get_setting,
    init_db,
    insert_batch,
    list_duplicates,
    update_setting,
)
from parser import FLAGS


class LoginBody(BaseModel):
    username: str
    password: str


class BatchBody(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    raw_text: str = Field(min_length=1)
    # 后来报文的处理方式：reject 整行留在未接纳 / overwrite 覆盖厘米
    duplicate_policy: str = Field(min_length=1)


class SettingBody(BaseModel):
    policy: str = Field(min_length=1)


KIND_LABELS = {
    KIND_REJECT: "拒绝",
    KIND_CONTENTION: "争用",
    KIND_MISMATCH: "处理不符",
    "overwrite": "覆盖",
}


def _stamp(value):
    return value.isoformat(sep=" ", timespec="minutes") if value else None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)


def _user_or_401(authorization: str | None):
    user = read_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _writer_or_403(user):
    if user["role"] != "writer":
        raise HTTPException(status_code=403, detail="档案员不能归集报文或改去重设置")


@app.get("/api/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "service": "tide-telegram-intake"}


@app.post("/api/login")
def login(body: LoginBody):
    user = authenticate(body.username.strip(), body.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {
        "access_token": issue_token(user),
        "username": user["username"],
        "role": user["role"],
        "title": user["title"],
    }


@app.get("/api/dedup-setting")
def read_dedup_setting(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    today = date.today()
    with connect() as conn:
        row = get_setting(conn, today)
    if not row:
        return {
            "setting_date": today.isoformat(),
            "policy": None,
            "policy_label": None,
            "locked": False,
            "updated_by": None,
            "updated_at": None,
            "options": [{"value": k, "label": v} for k, v in POLICIES.items()],
        }
    return {
        "setting_date": row["setting_date"].isoformat(),
        "policy": row["policy"],
        "policy_label": POLICIES.get(row["policy"], row["policy"]),
        "locked": row["locked"],
        "updated_by": row["updated_by"],
        "updated_at": _stamp(row["updated_at"]),
        "options": [{"value": k, "label": v} for k, v in POLICIES.items()],
    }


@app.put("/api/dedup-setting")
def write_dedup_setting(body: SettingBody,
                        authorization: str | None = Header(default=None)):
    user = _user_or_401(authorization)
    _writer_or_403(user)
    if body.policy not in POLICIES:
        raise HTTPException(status_code=400, detail="处理方式只能是拒绝重复或覆盖厘米")
    today = date.today()
    with connect() as conn:
        result, row = update_setting(conn, today, body.policy, user["username"])
        conn.commit()
    if result == "locked":
        raise HTTPException(
            status_code=409,
            detail="当日去重设置已随第一批报文锁定，今天不能再换处理方式",
        )
    if result == "busy":
        raise HTTPException(
            status_code=409,
            detail="覆盖进行中，不许改去重设置",
        )
    return {
        "setting_date": row["setting_date"].isoformat(),
        "policy": row["policy"],
        "policy_label": POLICIES.get(row["policy"], row["policy"]),
        "locked": row["locked"],
        "updated_by": row["updated_by"],
        "updated_at": _stamp(row["updated_at"]),
    }


@app.get("/api/duplicates")
def duplicates(authorization: str | None = Header(default=None)):
    """重复列表：站码、时刻、覆盖次数、处理名；按拒绝/覆盖分两区。"""
    _user_or_401(authorization)
    with connect() as conn:
        rows = list_duplicates(conn)
    for row in rows:
        row["observed_at"] = _stamp(row["observed_at"])
        row["last_at"] = _stamp(row["last_at"])
    return rows


@app.get("/api/duplicates/{station_code}/{observed_at}")
def duplicate(station_code: str, observed_at: str,
              authorization: str | None = Header(default=None)):
    """时刻详情：对照新旧厘米，附覆盖留痕和未接纳行。"""
    _user_or_401(authorization)
    from datetime import datetime

    try:
        moment = datetime.strptime(observed_at, "%Y%m%d%H%M")
    except ValueError:
        raise HTTPException(status_code=400, detail="时刻应为十二位数字")
    with connect() as conn:
        detail = duplicate_detail(conn, station_code, moment)
    landed = detail["landed"]
    if landed:
        landed["flag_label"] = FLAGS.get(landed["quality_flag"], landed["quality_flag"])
    for item in detail["history"]:
        item["created_at"] = _stamp(item["created_at"])
        item["flag_label"] = FLAGS.get(item["quality_flag"], item["quality_flag"])
    for item in detail["rejects"]:
        item["created_at"] = _stamp(item["created_at"])
        item["kind_label"] = KIND_LABELS.get(item["kind"], item["kind"])
    detail["observed_at"] = _stamp(moment)
    detail["station_code"] = station_code
    return detail


@app.get("/api/batches")
def list_batches(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, title, status, accepted_count, rejected_count,
                   overwrite_count, created_by, created_at
            FROM intake_batches
            ORDER BY id DESC
            """
        ).fetchall()
    for row in rows:
        row["created_at"] = _stamp(row["created_at"])
    return rows


@app.get("/api/batches/{batch_id}")
def batch_detail(batch_id: int, authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        batch = conn.execute(
            """
            SELECT id, title, raw_text, status, accepted_count, rejected_count,
                   overwrite_count, created_by, created_at
            FROM intake_batches WHERE id = %s
            """,
            (batch_id,),
        ).fetchone()
        if not batch:
            raise HTTPException(status_code=404, detail="没有这一批报文")
        landed = conn.execute(
            """
            SELECT line_no, station_code, observed_at, tide_cm, quality_flag
            FROM landed_rows WHERE batch_id = %s ORDER BY line_no
            """,
            (batch_id,),
        ).fetchall()
        rejected = conn.execute(
            """
            SELECT line_no, station_code, observed_at, raw_line, reason, kind
            FROM reject_rows WHERE batch_id = %s ORDER BY line_no
            """,
            (batch_id,),
        ).fetchall()
        overwrites = conn.execute(
            """
            SELECT line_no, station_code, observed_at, old_tide_cm, new_tide_cm,
                   quality_flag, created_at
            FROM overwrite_history WHERE batch_id = %s ORDER BY line_no
            """,
            (batch_id,),
        ).fetchall()
    batch["created_at"] = _stamp(batch["created_at"])
    for row in landed:
        row["observed_at"] = _stamp(row["observed_at"])
        row["flag_label"] = FLAGS.get(row["quality_flag"], row["quality_flag"])
    for row in rejected:
        row["observed_at"] = _stamp(row["observed_at"])
        row["kind_label"] = KIND_LABELS.get(row["kind"], row["kind"])
    for row in overwrites:
        row["observed_at"] = _stamp(row["observed_at"])
        row["flag_label"] = FLAGS.get(row["quality_flag"], row["quality_flag"])
        row["created_at"] = _stamp(row["created_at"])
    return {
        "batch": batch,
        "landed": landed,
        "rejected": rejected,
        "overwrites": overwrites,
    }


@app.post("/api/batches")
def create_batch(body: BatchBody, authorization: str | None = Header(default=None)):
    user = _user_or_401(authorization)
    _writer_or_403(user)
    if body.duplicate_policy not in (POLICY_REJECT, POLICY_OVERWRITE):
        raise HTTPException(status_code=400, detail="处理方式只能是拒绝重复或覆盖厘米")

    title = body.title.strip()
    raw_text = body.raw_text.strip()
    if not title or not raw_text:
        raise HTTPException(status_code=400, detail="标题和报文都不能空")

    today = date.today()
    with connect() as conn:
        try:
            batch_id = insert_batch(
                conn, title, raw_text, user["username"],
                body.duplicate_policy, today,
            )
            conn.commit()
        except DedupMismatch as exc:
            # 时刻处理不符：不符记录随本批一起落库，然后停下，不再处理后面的行
            conn.commit()
            mismatch_batch = exc.batch_id
        else:
            mismatch_batch = None
    if mismatch_batch is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": REASON_MISMATCH,
                "batch": batch_detail(mismatch_batch, authorization),
            },
        )
    return batch_detail(batch_id, authorization)


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
