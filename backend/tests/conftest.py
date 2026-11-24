"""测试公共夹具：隔离数据目录，逐测清库。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="tide-test-")
os.environ.setdefault("数据目录", _tmp)
os.environ.setdefault("夹具目录", str(Path(__file__).resolve().parents[2] / "fixtures"))

import pytest


_清表 = (
    "窗规",
    "网点源",
    "观测点",
    "入窗缓冲",
    "窗切片账",
    "切片事件",
    "迟到匣",
    "窗贯验",
    "装载批次",
    "关窗批次",
    "关窗收口",
    "关窗封印",
)


@pytest.fixture(autouse=True)
def _清库():
    from app import seed
    from app.db import 连接

    seed.初始化库()
    conn = 连接()
    try:
        for 表 in _清表:
            conn.execute(f"DELETE FROM {表}")
        conn.commit()
    finally:
        conn.close()
    seed.初始化库()
    yield
