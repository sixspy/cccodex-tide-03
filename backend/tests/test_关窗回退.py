"""关窗半截回滚（形态一·窗封印指纹回退）服务测试。"""
from __future__ import annotations

import threading

from app.db import 连接
from app.services import 装载, 关窗, 收口, 目录, 贯验, 夹具


def _缓冲命运() -> dict[str, int]:
    conn = 连接()
    try:
        rows = conn.execute(
            "SELECT 命运, COUNT(*) AS c FROM 入窗缓冲 GROUP BY 命运"
        ).fetchall()
        return {r["命运"]: r["c"] for r in rows}
    finally:
        conn.close()


def _片内事件数(切片编号: str) -> int:
    conn = 连接()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM 切片事件 WHERE 切片编号=?", (切片编号,)
        ).fetchone()["c"]
    finally:
        conn.close()


def test_中断回退现行片空且失败指纹可回读():
    报告 = 装载.装一批(夹具.读取("关窗中断样例"))
    assert 报告["入缓冲"] == 3
    assert _缓冲命运() == {"在缓冲": 3}

    关 = 关窗.关当前窗()
    assert 关["形态"] == "回退"
    assert 关["valid"] is False
    assert 关["现行片数"] == 0
    assert 关["归档键"] == ""
    assert 关["指纹"]

    # 本趟已写片与已物化缓冲全部退回趟前
    assert 目录.切片列表() == []
    assert _缓冲命运() == {"在缓冲": 3}
    conn = 连接()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM 观测点 WHERE 命运='已入缓冲'"
        ).fetchone()["c"] == 3
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM 关窗批次 WHERE 成片数>0"
        ).fetchone()["c"] == 0
    finally:
        conn.close()

    # 封印账可回读失败指纹
    封 = 收口.封印列表()
    assert 封 and 封[0]["有效"] == 0
    assert 封[0]["指纹"] == 关["指纹"]
    assert 封[0]["窗号"] == 关["窗号"]

    # 收口只读口径：趟号 / 现行片数 / 归档键 / 形态标记 / valid
    收 = 收口.当前收口()
    for 字 in ("趟号", "现行片数", "归档键", "形态", "valid"):
        assert 字 in 收
    assert 收["趟号"] == 关["趟号"]
    assert 收["形态"] == "回退"
    assert 收["valid"] is False
    assert 收["失败指纹"] == 关["指纹"]


def test_回退后清故障再关全量现行():
    装载.装一批(夹具.读取("关窗中断样例"))
    assert 关窗.关当前窗()["形态"] == "回退"

    目录.改窗规({"关窗故障": ""})
    关 = 关窗.关当前窗()
    assert 关["形态"] == "全量现行"
    assert 关["valid"] is True
    assert 关["成片数"] == 2
    assert 关["现行片数"] == 2
    assert 关["清残片"] == 0

    片 = 目录.切片列表()
    assert len(片) == 2
    assert sum(r["事件数"] for r in 片) == 3
    assert all(_片内事件数(r["切片编号"]) == r["事件数"] for r in 片)
    assert _缓冲命运() == {"已物化": 3}
    assert 贯验.跑一次()["是否对齐"] is True


def test_全程成功全量现行():
    装载.装一批(夹具.读取("成功成片对照"))
    关 = 关窗.关当前窗()
    assert 关["形态"] == "全量现行"
    assert 关["valid"] is True
    assert 关["成片数"] == 2
    assert 关["现行片数"] == 2  # 运单-M / 运单-N
    片 = 目录.切片列表()
    assert {r["运单键"] for r in 片} == {"运单-M", "运单-N"}
    assert sum(r["事件数"] for r in 片) == 3

    # 收口回执与页面集合同票
    assert 收口.当前收口()["现行片数"] == len(片) == 2


def test_空缓冲对照写空窗收口():
    报告 = 装载.装一批(夹具.读取("空缓冲对照"))
    assert 报告["入缓冲"] == 0
    assert 报告["迟到"] == 2
    assert _缓冲命运() == {}

    关 = 关窗.关当前窗()
    assert 关["形态"] == "空窗"
    assert 关["valid"] is True
    assert 关["成片数"] == 0
    assert 关["现行片数"] == 0
    assert 目录.切片列表() == []
    assert 收口.当前收口()["形态"] == "空窗"


def test_两位同时关窗只成片一次且无半窗():
    装载.装一批(夹具.读取("成功成片对照"))
    栏 = threading.Barrier(2)
    结果: list[dict] = []

    def 跑():
        栏.wait()
        try:
            结果.append(关窗.关当前窗())
        except Exception as e:  # pragma: no cover - 记录而不是掩盖
            结果.append({"错误": type(e).__name__ + ":" + str(e)})

    线 = [threading.Thread(target=跑) for _ in range(2)]
    for t in 线:
        t.start()
    for t in 线:
        t.join(timeout=30)
    assert not [r for r in 结果 if "错误" in r], 结果

    形态 = [r["形态"] for r in 结果]
    assert 形态.count("全量现行") == 1, 形态
    assert set(形态) <= {"全量现行", "已关", "空窗", "已占用"}, 形态

    片 = 目录.切片列表()
    assert len(片) == 2
    assert sum(r["事件数"] for r in 片) == 3
    assert _缓冲命运() == {"已物化": 3}
    assert 贯验.跑一次()["是否对齐"] is True
    assert 收口.当前收口()["形态"] == "全量现行"
    assert 收口.当前收口()["现行片数"] == len(片)


def test_残半片下次关窗前清场():
    装载.装一批(夹具.读取("成功成片对照"))
    # 人为制造上一趟崩溃残留：已成片记录 + 指向仍在缓冲事件的切片事件
    conn = 连接()
    try:
        conn.execute(
            """INSERT INTO 窗切片账(切片编号,窗号,运单键,事件数,窗起,窗止,物化时,命运)
               VALUES (?,?,?,?,?,?,?,?)""",
            ("片-脏", "W-demo", "运单-M", 1, "2026-11-24T00:00:00Z",
             "2026-11-24T01:00:00Z", "2026-11-24T00:30:00Z", "已成片"),
        )
        conn.execute(
            "INSERT INTO 切片事件(切片编号,事件编号) VALUES (?,?)",
            ("片-脏", "E-ok-1"),
        )
        conn.commit()
    finally:
        conn.close()
    assert 目录.切片列表() == []  # 残片不算现行

    关 = 关窗.关当前窗()
    assert 关["清残片"] == 1
    assert 关["形态"] == "全量现行"
    assert 关["现行片数"] == 2
    片 = 目录.切片列表()
    assert "片-脏" not in {r["切片编号"] for r in 片}
    assert len(片) == 2
    assert sum(r["事件数"] for r in 片) == 3
    assert 贯验.跑一次()["是否对齐"] is True


def test_收口与再次关窗并发不残留半窗():
    装载.装一批(夹具.读取("关窗中断样例"))
    assert 关窗.关当前窗()["形态"] == "回退"
    目录.改窗规({"关窗故障": ""})

    停 = threading.Event()
    错: list[str] = []
    快照数 = [0]

    def 读():
        while not 停.is_set():
            快 = 收口.快照()
            收 = 快["收口"]
            if 收["现行片数"] != len(快["现行片"]):
                错.append(f"收口与片集不同票:{收['现行片数']}!={len(快['现行片'])}")
            if 快["现行片数"] != len(快["现行片"]):
                错.append("现行片数与片集不同票")
            for 片 in 快["现行片"]:
                if not 收口.切片完整(片["切片编号"]):
                    错.append(f"半窗片:{片['切片编号']}")
            快照数[0] += 1

    读线 = threading.Thread(target=读)
    读线.start()
    try:
        关 = 关窗.关当前窗()
    finally:
        停.set()
        读线.join(timeout=10)

    assert not 错, 错[:5]
    assert 快照数[0] > 0
    assert 关["形态"] == "全量现行"
    assert 收口.残片() == []
    assert 收口.当前收口()["现行片数"] == len(目录.切片列表()) == 2


def test_旧库基线切片不当残片清掉():
    """升级前基线写的已成片没有切片事件挂接，初始化库时应回挂，不清场。"""
    装载.装一批(夹具.读取("成功成片对照"))
    conn = 连接()
    try:
        # 模拟基线旧库：成片 + 缓冲已物化，但无 切片事件、趟号为空
        conn.execute(
            """INSERT INTO 窗切片账(切片编号,窗号,运单键,事件数,窗起,窗止,物化时,命运)
               VALUES (?,?,?,?,?,?,?,?)""",
            ("片-旧-M", "W-demo", "运单-M", 2, "2026-11-24T00:00:00Z",
             "2026-11-24T01:00:00Z", "2026-11-24T00:30:00Z", "已成片"),
        )
        conn.execute("UPDATE 入窗缓冲 SET 命运='已物化' WHERE 运单键='运单-M'")
        conn.commit()
    finally:
        conn.close()

    from app import seed
    seed.初始化库()  # 升级钩子回挂旧片事件

    assert 收口.切片完整("片-旧-M") is True
    assert "片-旧-M" not in {r["切片编号"] for r in 收口.残片()}

    关 = 关窗.关当前窗()
    assert 关["清残片"] == 0
    assert "片-旧-M" in {r["切片编号"] for r in 目录.切片列表()}


def test_收尾钩子抛错不伪造回退回执():
    """事务已提交后收尾钩子异常，不得把已关窗反向写成「回退」。"""
    from app.modules import 关窗半截回滚 as 模

    装载.装一批(夹具.读取("成功成片对照"))
    原 = 模.after_close

    def 炸(报告):
        raise RuntimeError("收尾钩子炸了")

    模.after_close = 炸
    try:
        import pytest
        with pytest.raises(RuntimeError):
            关窗.关当前窗()
    finally:
        模.after_close = 原

    收 = 收口.当前收口()
    assert 收["形态"] == "全量现行"
    assert 收["valid"] is True
    assert len(目录.切片列表()) == 2
    assert 贯验.跑一次()["是否对齐"] is True
