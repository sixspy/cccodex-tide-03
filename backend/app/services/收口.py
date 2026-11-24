"""关窗收口：只读回执与「现行片集」统一口径。

形态一·窗封印指纹回退——关窗事务中途失败时，本趟已写片与已物化缓冲全部退回趟前；
封印账留下失败指纹（有效=0），贯验改读空片集。切片账、入窗缓冲、贯验、收口
共用本模块的 现行片 / 残片 谓词，保证四处读同一套集合。

现行片判据（三条全满足）：
1. 窗切片账.命运 = 已成片 且 事件数 > 0
2. 切片事件挂接行数 = 事件数
3. 挂接事件对应的入窗缓冲命运全部 = 已物化
不满足者为残片（半窗片），下一次关窗前清场。
"""
from __future__ import annotations

from app.db import 连接

策略 = "形态一·窗封印指纹回退"
形态标记 = ("未关窗", "回退", "全量现行", "空窗", "已关", "已占用")

_收口字段 = ("趟号", "窗号", "形态", "现行片数", "归档键", "valid", "失败指纹",
             "指纹", "清残片", "成片数", "时刻", "说明")


def _片判据(conn) -> tuple[list[dict], list[dict]]:
    rows = conn.execute(
        """
        SELECT s.*,
               (SELECT COUNT(*) FROM 切片事件 se WHERE se.切片编号 = s.切片编号) AS 挂事件数,
               (SELECT COUNT(*) FROM 切片事件 se
                  JOIN 入窗缓冲 b ON b.事件编号 = se.事件编号
                 WHERE se.切片编号 = s.切片编号 AND b.命运 = '已物化') AS 挂已物化
          FROM 窗切片账 s
         WHERE s.命运 = '已成片'
         ORDER BY s.物化时, s.切片编号
        """
    ).fetchall()
    现行: list[dict] = []
    残: list[dict] = []
    for r in rows:
        d = dict(r)
        完整 = (
            int(d.get("事件数") or 0) > 0
            and int(d["挂事件数"]) == int(d["事件数"])
            and int(d["挂已物化"]) == int(d["事件数"])
        )
        d.pop("挂事件数", None)
        d.pop("挂已物化", None)
        (现行 if 完整 else 残).append(d)
    return 现行, 残


def 现行片() -> list[dict]:
    conn = 连接()
    try:
        return _片判据(conn)[0]
    finally:
        conn.close()


def 残片() -> list[dict]:
    conn = 连接()
    try:
        return _片判据(conn)[1]
    finally:
        conn.close()


def 切片完整(切片编号: str) -> bool:
    conn = 连接()
    try:
        现行, 残 = _片判据(conn)
        return any(r["切片编号"] == 切片编号 for r in 现行) and not any(
            r["切片编号"] == 切片编号 for r in 残
        )
    finally:
        conn.close()


def 封印列表(限制: int = 20) -> list[dict]:
    conn = 连接()
    try:
        rows = conn.execute(
            "SELECT * FROM 关窗封印 ORDER BY 编号 DESC LIMIT ?", (限制,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _最新收口(conn) -> dict | None:
    r = conn.execute("SELECT * FROM 关窗收口 ORDER BY 编号 DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def _回执(conn) -> dict:
    收 = _最新收口(conn)
    现行, 残 = _片判据(conn)
    if 收 is None:
        return {
            "趟号": "",
            "窗号": "",
            "形态": "未关窗",
            "现行片数": len(现行),
            "归档键": "",
            "valid": False,
            "失败指纹": "",
            "指纹": "",
            "清残片": 0,
            "成片数": 0,
            "时刻": "",
            "说明": "尚未关窗",
            "策略": 策略,
            "残片数": len(残),
            "封印": 封印列表(),
        }
    out = {k: 收.get(k) for k in _收口字段}
    out["valid"] = bool(收["valid"])
    if not out.get("失败指纹"):
        # 失败指纹可跨趟回读：取最近一枚无效封印
        r = conn.execute(
            "SELECT 指纹 FROM 关窗封印 WHERE 有效=0 ORDER BY 编号 DESC LIMIT 1"
        ).fetchone()
        out["失败指纹"] = r["指纹"] if r else ""
    out["策略"] = 策略
    out["残片数"] = len(残)
    out["封印"] = 封印列表()
    return out


def 当前收口() -> dict:
    """只读收口口径：趟号 / 现行片数 / 归档键 / 形态标记 / valid。"""
    conn = 连接()
    try:
        return _回执(conn)
    finally:
        conn.close()


def 快照() -> dict:
    """同一读事务内取收口与现行片集，供并发读不被半窗污染。"""
    conn = 连接()
    try:
        conn.execute("BEGIN")
        收 = _回执(conn)
        现行, 残 = _片判据(conn)
        conn.execute("COMMIT")
        return {
            "收口": 收,
            "现行片": 现行,
            "现行片数": len(现行),
            "残片": 残,
        }
    finally:
        conn.close()
