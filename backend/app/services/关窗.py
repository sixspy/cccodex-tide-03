"""关窗：缓冲按运单物化为窗切片。

形态一·窗封印指纹回退。一次关窗是一趟：
1. 取关窗互斥锁（BEGIN IMMEDIATE）——两位同时点关窗只会成片一次；
2. 清场：上一趟崩溃残留的半窗残片（本趟若失败，清场随外层一起回滚）；
3. 物化：写切片账 + 切片事件 + 缓冲改已物化 + 观测点改已成片；
4. 收口回执、封印指纹、切片与批次同事务提交。

任一环节抛错：ROLLBACK + 兜底清本趟趟号痕迹，封印账另起事务写失败指纹
（有效=0），贯验随即读到「回退」形态与空片集。
"""
from __future__ import annotations

import json
import uuid
from collections import defaultdict

from app.db import 连接
from app.seed import 此刻
from app.services import 目录


def _before_close(窗号: str) -> None:
    """模块前置钩子：可抛错阻断关窗（本仓默认放行）。"""
    try:
        from app.modules.关窗半截回滚 import before_close
    except Exception:  # pragma: no cover - 邻题未装
        return
    before_close(窗号)


def _after_close(报告: dict) -> dict:
    try:
        from app.modules.关窗半截回滚 import after_close
    except Exception:  # pragma: no cover - 邻题未装
        return 报告
    return after_close(报告) or 报告


def _清残片(conn) -> int:
    """删除未完整挂接的已成片及其切片事件，返回清掉的片数。"""
    rows = conn.execute(
        """
        SELECT s.切片编号, s.事件数,
               (SELECT COUNT(*) FROM 切片事件 se WHERE se.切片编号 = s.切片编号) AS 挂事件数,
               (SELECT COUNT(*) FROM 切片事件 se
                  JOIN 入窗缓冲 b ON b.事件编号 = se.事件编号
                 WHERE se.切片编号 = s.切片编号 AND b.命运 = '已物化') AS 挂已物化
          FROM 窗切片账 s
         WHERE s.命运 = '已成片'
        """
    ).fetchall()
    坏 = [
        r["切片编号"]
        for r in rows
        if not (
            int(r["事件数"] or 0) > 0
            and int(r["挂事件数"]) == int(r["事件数"])
            and int(r["挂已物化"]) == int(r["事件数"])
        )
    ]
    for 片 in 坏:
        conn.execute("DELETE FROM 切片事件 WHERE 切片编号=?", (片,))
        conn.execute("DELETE FROM 窗切片账 WHERE 切片编号=?", (片,))
    return len(坏)


def _回退(conn, 趟号: str, 窗号: str, 指纹: str, 错: str) -> None:
    try:
        conn.rollback()
    except Exception:
        pass
    # 兜底：即便回滚未生效，也不把本趟痕迹留在现行片集
    try:
        conn.execute(
            "DELETE FROM 切片事件 WHERE 切片编号 IN "
            "(SELECT 切片编号 FROM 窗切片账 WHERE 趟号=?)",
            (趟号,),
        )
        conn.execute("DELETE FROM 窗切片账 WHERE 趟号=?", (趟号,))
        conn.execute(
            "UPDATE 观测点 SET 命运='已入缓冲' WHERE 命运='已成片' AND 事件编号 IN "
            "(SELECT 事件编号 FROM 入窗缓冲 WHERE 趟号=?)",
            (趟号,),
        )
        conn.execute(
            "UPDATE 入窗缓冲 SET 命运='在缓冲' WHERE 趟号=? AND 命运='已物化'",
            (趟号,),
        )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    conn.execute(
        """INSERT INTO 关窗封印(窗号,趟号,指纹,有效,时刻,说明)
           VALUES (?,?,?,?,?,?)""",
        (窗号, 趟号, 指纹, 0, 此刻(), f"回退：{错}"),
    )
    conn.execute(
        """INSERT INTO 关窗收口(趟号,窗号,形态,现行片数,归档键,valid,失败指纹,指纹,
                                清残片,成片数,时刻,说明)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (趟号, 窗号, "回退", 0, "", 0, 指纹, 指纹, 0, 0, 此刻(), f"回退：{错}"),
    )
    conn.commit()


def _回退报告(趟号: str, 窗号: str, 指纹: str, 错: str) -> dict:
    return {
        "趟号": 趟号,
        "窗号": 窗号,
        "成片数": 0,
        "缓冲消耗": 0,
        "形态": "回退",
        "现行片数": 0,
        "归档键": "",
        "valid": False,
        "指纹": 指纹,
        "清残片": 0,
        "说明": f"回退：{错}",
    }


def _已占用(conn, 趟号: str, 窗号: str) -> dict:
    """拿不到互斥锁：不成片，也不写收口（状态未变，回执不落账）。"""
    try:
        conn.rollback()
    except Exception:
        pass
    return {
        "趟号": 趟号,
        "窗号": 窗号,
        "成片数": 0,
        "缓冲消耗": 0,
        "形态": "已占用",
        "现行片数": 0,
        "归档键": "",
        "valid": False,
        "指纹": "",
        "清残片": 0,
        "说明": "另一位关窗正在进行，本趟未再成片",
    }


def _窗内现行片数(conn, 窗号: str) -> int:
    rows = conn.execute(
        """
        SELECT s.事件数,
               (SELECT COUNT(*) FROM 切片事件 se WHERE se.切片编号 = s.切片编号) AS 挂事件数,
               (SELECT COUNT(*) FROM 切片事件 se
                  JOIN 入窗缓冲 b ON b.事件编号 = se.事件编号
                 WHERE se.切片编号 = s.切片编号 AND b.命运 = '已物化') AS 挂已物化
          FROM 窗切片账 s
         WHERE s.命运='已成片' AND s.窗号=?
        """,
        (窗号,),
    ).fetchall()
    return sum(
        1
        for r in rows
        if int(r["事件数"] or 0) > 0
        and int(r["挂事件数"]) == int(r["事件数"])
        and int(r["挂已物化"]) == int(r["事件数"])
    )


def 关当前窗() -> dict:
    规 = 目录.窗规()
    窗号 = 规.get("当前窗号", "W-demo")
    窗起 = 规.get("窗起", "")
    窗止 = 规.get("窗止", "")
    故障 = str(规.get("关窗故障") or "").strip()
    趟号 = f"趟-{uuid.uuid4().hex[:10]}"
    指纹 = uuid.uuid4().hex
    已提交 = False
    conn = 连接()
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except Exception:
            return _已占用(conn, 趟号, 窗号)

        # —— 同一写事务：清场 + 物化 + 收口 + 封印 ——
        _before_close(窗号)
        清残 = _清残片(conn)
        rows = conn.execute(
            "SELECT * FROM 入窗缓冲 WHERE 窗号=? AND 命运='在缓冲'", (窗号,)
        ).fetchall()
        by_waybill: dict[str, list] = defaultdict(list)
        for r in rows:
            by_waybill[r["运单键"]].append(r)
        时刻 = 此刻()
        成片 = 0
        for 运单, evs in by_waybill.items():
            切片 = f"片-{窗号}-{运单}-{uuid.uuid4().hex[:6]}"
            conn.execute(
                """INSERT INTO 窗切片账(切片编号,窗号,运单键,事件数,窗起,窗止,物化时,命运,趟号)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (切片, 窗号, 运单, len(evs), 窗起, 窗止, 时刻, "已成片", 趟号),
            )
            for e in evs:
                conn.execute(
                    "INSERT INTO 切片事件(切片编号,事件编号) VALUES (?,?)",
                    (切片, e["事件编号"]),
                )
                conn.execute(
                    "UPDATE 入窗缓冲 SET 命运=?,趟号=? WHERE 事件编号=?",
                    ("已物化", 趟号, e["事件编号"]),
                )
                conn.execute(
                    "UPDATE 观测点 SET 命运=? WHERE 事件编号=?",
                    ("已成片", e["事件编号"]),
                )
            成片 += 1
            if 故障 == "物化半截":
                raise RuntimeError("人为中断：物化半截")

        形态 = "全量现行" if 成片 else "空窗"
        旧片 = 0 if 成片 else _窗内现行片数(conn, 窗号)
        if not 成片 and 旧片:
            # 幂等重关：窗内现行片已完整，空手而回，不覆盖已有收口
            conn.commit()
            return {
                "趟号": 趟号,
                "窗号": 窗号,
                "成片数": 0,
                "缓冲消耗": len(rows),
                "形态": "已关",
                "现行片数": 旧片,
                "归档键": "",
                "valid": True,
                "指纹": "",
                "清残片": 清残,
                "说明": f"窗已关：现行{旧片}片，未再物化",
            }
        报告 = {
            "趟号": 趟号,
            "窗号": 窗号,
            "成片数": 成片,
            "缓冲消耗": len(rows),
            "形态": 形态,
            "现行片数": 成片,
            "归档键": "",
            "valid": True,
            "指纹": 指纹,
            "清残片": 清残,
            "说明": f"{形态}：现行{成片}片",
        }
        conn.execute(
            """INSERT INTO 关窗批次(编号,窗号,时刻,成片数,报告) VALUES (?,?,?,?,?)""",
            (趟号, 窗号, 时刻, 成片, json.dumps(报告, ensure_ascii=False)),
        )
        conn.execute(
            """INSERT INTO 关窗收口(趟号,窗号,形态,现行片数,归档键,valid,失败指纹,指纹,
                                    清残片,成片数,时刻,说明)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (趟号, 窗号, 形态, 成片, "", 1, "", 指纹, 清残, 成片, 时刻,
             f"{形态}：现行{成片}片"),
        )
        conn.execute(
            """INSERT INTO 关窗封印(窗号,趟号,指纹,有效,时刻,说明)
               VALUES (?,?,?,?,?,?)""",
            (窗号, 趟号, 指纹, 1, 时刻, f"{形态}：现行{成片}片"),
        )
        conn.commit()
        已提交 = True
        return _after_close(报告)
    except Exception as e:
        if 已提交:
            # 事务已成片提交，收尾钩子的异常不得反向写成「回退」假回执
            raise
        _回退(conn, 趟号, 窗号, 指纹, f"{type(e).__name__}:{e}")
        return _回退报告(趟号, 窗号, 指纹, f"{type(e).__name__}:{e}")
    finally:
        conn.close()
