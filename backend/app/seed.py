from datetime import datetime, timezone
from app.db import 连接

建表 = """
CREATE TABLE IF NOT EXISTS 窗规 (
  键 TEXT PRIMARY KEY,
  值 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS 网点源 (
  源键 TEXT PRIMARY KEY,
  名称 TEXT NOT NULL,
  区域 TEXT NOT NULL DEFAULT '',
  说明 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 观测点 (
  事件编号 TEXT PRIMARY KEY,
  运单键 TEXT NOT NULL,
  网点键 TEXT NOT NULL,
  事件时 TEXT NOT NULL,
  处理时 TEXT NOT NULL,
  命运 TEXT NOT NULL DEFAULT '已装载',
  批次编号 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 入窗缓冲 (
  事件编号 TEXT PRIMARY KEY,
  运单键 TEXT NOT NULL,
  网点键 TEXT NOT NULL,
  事件时 TEXT NOT NULL,
  窗号 TEXT NOT NULL,
  命运 TEXT NOT NULL DEFAULT '在缓冲',
  入缓冲时 TEXT NOT NULL,
  趟号 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 窗切片账 (
  切片编号 TEXT PRIMARY KEY,
  窗号 TEXT NOT NULL,
  运单键 TEXT NOT NULL,
  事件数 INTEGER NOT NULL,
  窗起 TEXT NOT NULL,
  窗止 TEXT NOT NULL,
  物化时 TEXT NOT NULL,
  命运 TEXT NOT NULL DEFAULT '已成片',
  趟号 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 迟到匣 (
  编号 INTEGER PRIMARY KEY AUTOINCREMENT,
  事件编号 TEXT NOT NULL,
  运单键 TEXT NOT NULL,
  原因 TEXT NOT NULL,
  事件时 TEXT NOT NULL,
  入匣时 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS 切片事件 (
  切片编号 TEXT NOT NULL,
  事件编号 TEXT NOT NULL,
  PRIMARY KEY (切片编号, 事件编号)
);
CREATE TABLE IF NOT EXISTS 窗贯验 (
  编号 INTEGER PRIMARY KEY AUTOINCREMENT,
  时刻 TEXT NOT NULL,
  缓冲笔数 INTEGER NOT NULL,
  切片笔数 INTEGER NOT NULL,
  迟到笔数 INTEGER NOT NULL,
  是否对齐 INTEGER NOT NULL,
  说明 TEXT NOT NULL,
  形态 TEXT NOT NULL DEFAULT '',
  现行片数 INTEGER NOT NULL DEFAULT 0,
  归档键 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 关窗封印 (
  编号 INTEGER PRIMARY KEY AUTOINCREMENT,
  窗号 TEXT NOT NULL,
  趟号 TEXT NOT NULL,
  指纹 TEXT NOT NULL,
  有效 INTEGER NOT NULL DEFAULT 0,
  时刻 TEXT NOT NULL,
  说明 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 关窗收口 (
  编号 INTEGER PRIMARY KEY AUTOINCREMENT,
  趟号 TEXT NOT NULL UNIQUE,
  窗号 TEXT NOT NULL,
  形态 TEXT NOT NULL,
  现行片数 INTEGER NOT NULL DEFAULT 0,
  归档键 TEXT NOT NULL DEFAULT '',
  valid INTEGER NOT NULL DEFAULT 0,
  失败指纹 TEXT NOT NULL DEFAULT '',
  指纹 TEXT NOT NULL DEFAULT '',
  清残片 INTEGER NOT NULL DEFAULT 0,
  成片数 INTEGER NOT NULL DEFAULT 0,
  时刻 TEXT NOT NULL,
  说明 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 装载批次 (
  编号 TEXT PRIMARY KEY,
  时刻 TEXT NOT NULL,
  入流 INTEGER NOT NULL DEFAULT 0,
  入缓冲 INTEGER NOT NULL DEFAULT 0,
  迟到 INTEGER NOT NULL DEFAULT 0,
  报告 TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS 关窗批次 (
  编号 TEXT PRIMARY KEY,
  窗号 TEXT NOT NULL,
  时刻 TEXT NOT NULL,
  成片数 INTEGER NOT NULL DEFAULT 0,
  报告 TEXT NOT NULL DEFAULT '{}'
);
"""


def 此刻() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _补列(conn, 表: str, 列: str, 定义: str) -> None:
    """旧库平滑补列（SQLite 无 ADD COLUMN IF NOT EXISTS）。"""
    有 = {r["name"] for r in conn.execute(f"PRAGMA table_info({表})")}
    if 列 not in 有:
        conn.execute(f"ALTER TABLE {表} ADD COLUMN {列} {定义}")


def _补旧切片事件(conn) -> None:
    """旧库升级：基线时期的已成片没有切片事件挂接，按运单+窗种回挂，
    避免新判据把它们当残片清掉。只补旧片（无任何切片事件者）。"""
    旧片 = conn.execute(
        """SELECT s.切片编号, s.窗号, s.运单键
             FROM 窗切片账 s
            WHERE s.命运='已成片'
              AND NOT EXISTS (SELECT 1 FROM 切片事件 se WHERE se.切片编号=s.切片编号)
              AND s.趟号 = ''"""
    ).fetchall()
    for 片 in 旧片:
        事件 = conn.execute(
            """SELECT 事件编号 FROM 入窗缓冲
                WHERE 运单键=? AND 窗号=? AND 命运='已物化'
                ORDER BY 事件时""",
            (片["运单键"], 片["窗号"]),
        ).fetchall()
        for e in 事件:
            conn.execute(
                "INSERT OR IGNORE INTO 切片事件(切片编号,事件编号) VALUES (?,?)",
                (片["切片编号"], e["事件编号"]),
            )


def 初始化库() -> None:
    conn = 连接()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(建表)
    for 列, 定义 in (
        ("形态", "TEXT NOT NULL DEFAULT ''"),
        ("现行片数", "INTEGER NOT NULL DEFAULT 0"),
        ("归档键", "TEXT NOT NULL DEFAULT ''"),
    ):
        _补列(conn, "窗贯验", 列, 定义)
    _补列(conn, "入窗缓冲", "趟号", "TEXT NOT NULL DEFAULT ''")
    _补列(conn, "窗切片账", "趟号", "TEXT NOT NULL DEFAULT ''")
    _补旧切片事件(conn)
    if conn.execute("SELECT COUNT(*) AS c FROM 窗规").fetchone()["c"] == 0:
        for k, v in {
            "窗长秒": "3600",
            "允许迟到秒": "0",
            "关窗策略": "按事件时整点",
            "当前窗号": "W-demo",
            "窗起": "2026-11-24T00:00:00Z",
            "窗止": "2026-11-24T01:00:00Z",
        }.items():
            conn.execute("INSERT INTO 窗规(键,值) VALUES (?,?)", (k, v))
    if conn.execute("SELECT COUNT(*) AS c FROM 网点源").fetchone()["c"] == 0:
        for row in [
            ("网点-东站", "东站分拨", "华东", "示范网点"),
            ("网点-西港", "西港分拨", "华东", "示范网点"),
            ("网点-北仓", "北仓分拨", "华北", "示范网点"),
        ]:
            conn.execute(
                "INSERT INTO 网点源(源键,名称,区域,说明) VALUES (?,?,?,?)", row
            )
    conn.commit()
    conn.close()
