"""收口只读接口与路由测试。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("数据目录", tempfile.mkdtemp(prefix="tide-route-"))
os.environ.setdefault("夹具目录", str(Path(__file__).resolve().parents[2] / "fixtures"))

from starlette.testclient import TestClient

from app.main import app


def _客户端() -> TestClient:
    return TestClient(app)


def test_未关窗时收口只读口径():
    with _客户端() as c:
        收 = c.get("/api/关窗/收口").json()
        assert 收["形态"] == "未关窗"
        assert 收["现行片数"] == 0
        assert 收["归档键"] == ""
        assert 收["valid"] is False
        assert "趟号" in 收
        assert "形态一" in 收["策略"]


def test_中断后收口接口可区分回退():
    with _客户端() as c:
        assert c.post("/api/观测点/夹具/关窗中断样例").status_code == 200
        关 = c.post("/api/关窗/跑").json()
        assert 关["形态"] == "回退"

        收 = c.get("/api/关窗/收口").json()
        assert 收["趟号"] == 关["趟号"]
        assert 收["现行片数"] == 0
        assert 收["归档键"] == ""
        assert 收["形态"] == "回退"
        assert 收["valid"] is False
        assert 收["失败指纹"] == 关["指纹"]

        assert c.get("/api/窗切片账").json() == []
        缓冲 = c.get("/api/入窗缓冲").json()
        assert len(缓冲) == 3 and {r["命运"] for r in 缓冲} == {"在缓冲"}

        验 = c.post("/api/窗贯验/跑").json()
        assert 验["形态"] == "回退"
        assert 验["现行片数"] == 0
        assert 验["归档键"] == ""
        assert 验["是否对齐"] is True
        assert "回退" in 验["说明"]


def test_成功收口与页面集合同票():
    with _客户端() as c:
        assert c.post("/api/观测点/夹具/成功成片对照").status_code == 200
        关 = c.post("/api/关窗/跑").json()
        assert 关["形态"] == "全量现行"

        收 = c.get("/api/关窗/收口").json()
        片 = c.get("/api/窗切片账").json()
        assert 收["形态"] == "全量现行"
        assert 收["valid"] is True
        assert 收["归档键"] == ""
        assert 收["现行片数"] == len(片) == 2
        assert 收["现行片数"] == 关["现行片数"]

        验 = c.post("/api/窗贯验/跑").json()
        assert 验["形态"] == "全量现行"
        assert 验["现行片数"] == 2
        assert 验["归档键"] == ""
        assert 验["是否对齐"] is True
        assert 验["切片笔数"] == 3

        历史 = c.get("/api/窗贯验").json()
        assert 历史 and 历史[0]["形态"] == "全量现行"
        assert 历史[0]["现行片数"] == 2


def test_封印账保留失败与成功两枚指纹():
    with _客户端() as c:
        c.post("/api/观测点/夹具/关窗中断样例")
        回 = c.post("/api/关窗/跑").json()
        c.post("/api/窗规", json={"关窗故障": ""})
        成 = c.post("/api/关窗/跑").json()
        assert 回["指纹"] and 成["指纹"]

        收 = c.get("/api/关窗/收口").json()
        封 = 收["封印"]
        assert len(封) >= 2
        assert 封[0]["有效"] == 1 and 封[0]["指纹"] == 成["指纹"]
        assert any(x["有效"] == 0 and x["指纹"] == 回["指纹"] for x in 封)
        assert 收["失败指纹"] == 回["指纹"]
