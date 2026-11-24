"""窗贯验：缓冲 / 切片 / 迟到互证。

形态一·窗封印指纹回退下，贯验改读「现行片集」（与切片账、收口同一谓词）：
- 回退趟：现行片为空，已物化缓冲为 0，对照在缓冲笔数，结论写明失败指纹；
- 全量现行：已物化缓冲事件数 = 现行片事件数；
- 空窗：成功但无片。

模块扩展检查（`关窗半截回滚`.extra_verify_checks）追加一条：收口回执与现行片
集合同票、且无半窗残片；不通过则判撕裂。
"""
from __future__ import annotations

from app.db import 连接
from app.seed import 此刻
from app.services import 收口


def _附加检查(快照: dict) -> list[str]:
    try:
        from app.modules.关窗半截回滚 import extra_verify_checks
    except Exception:  # pragma: no cover - 邻题未装
        return []
    return extra_verify_checks(快照)


def 跑一次() -> dict:
    conn = 连接()
    try:
        缓冲 = conn.execute(
            "SELECT COUNT(*) AS c FROM 入窗缓冲 WHERE 命运='在缓冲'"
        ).fetchone()["c"]
        已物化缓冲 = conn.execute(
            "SELECT COUNT(*) AS c FROM 入窗缓冲 WHERE 命运='已物化'"
        ).fetchone()["c"]
        迟到 = conn.execute("SELECT COUNT(*) AS c FROM 迟到匣").fetchone()["c"]
    finally:
        conn.close()

    收 = 收口.当前收口()
    现行 = 收口.现行片()
    现行事件 = sum(int(r["事件数"] or 0) for r in 现行)
    形态 = 收["形态"]
    归档键 = 收.get("归档键") or ""
    失败指纹 = 收.get("失败指纹") or ""

    if 形态 == "回退":
        对齐 = int(已物化缓冲) == 0 and not 现行
        说明 = (
            f"回退：现行片空，{缓冲}笔退回缓冲；失败指纹 {失败指纹[:10]}"
            if 对齐
            else f"回退却残留：已物化缓冲={已物化缓冲} 现行片={len(现行)}"
        )
    elif 形态 == "空窗":
        对齐 = int(已物化缓冲) == 0 and int(缓冲) == 0 and not 现行
        说明 = "空窗封印：无缓冲可物化" if 对齐 else f"空窗却有残留：在缓冲={缓冲}"
    else:
        对齐 = int(已物化缓冲) == int(现行事件)
        说明 = (
            "缓冲物化与现行片事件数一致"
            if 对齐
            else f"撕裂：已物化缓冲={已物化缓冲} 现行片事件={现行事件}"
        )

    附加 = _附加检查({"收口": 收, "现行片": 现行, "在缓冲": 缓冲})
    if 附加:
        对齐 = False
        说明 = 说明 + "；" + "；".join(附加)

    时刻 = 此刻()
    conn = 连接()
    try:
        conn.execute(
            """INSERT INTO 窗贯验(时刻,缓冲笔数,切片笔数,迟到笔数,是否对齐,说明,形态,现行片数,归档键)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (时刻, 缓冲, 现行事件, 迟到, int(对齐), 说明, 形态, len(现行), 归档键),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "时刻": 时刻,
        "趟号": 收.get("趟号", ""),
        "形态": 形态,
        "在缓冲": 缓冲,
        "已物化缓冲": int(已物化缓冲),
        "切片事件": 现行事件,
        "切片笔数": 现行事件,
        "现行片数": len(现行),
        "归档键": 归档键,
        "失败指纹": 失败指纹,
        "迟到": 迟到,
        "是否对齐": bool(对齐),
        "说明": 说明,
    }
