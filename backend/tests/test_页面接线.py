"""页面接线测试：四处页面须读到关窗收口口径（四文件起）。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("数据目录", tempfile.mkdtemp(prefix="tide-page-"))
os.environ.setdefault("夹具目录", str(Path(__file__).resolve().parents[2] / "fixtures"))

from starlette.testclient import TestClient

from app.main import app

_页 = ("窗切片账.html", "入窗缓冲.html", "窗贯验.html", "窗规.html")


def test_四处页面均可打开():
    with TestClient(app) as c:
        for 名 in _页:
            r = c.get("/" + 名)
            assert r.status_code == 200, 名
            assert "关窗收口" in r.text, 名


def test_切片页与缓冲页挂收口():
    with TestClient(app) as c:
        共 = c.get("/common.js").text
        assert "现行片数" in 共 and "归档键" in 共 and "失败指纹" in 共

        切 = c.get("/窗切片账.html").text
        assert "/api/关窗/收口" in 切 or "/api/关窗/快照" in 切
        assert "现行片集" in 切

        缓 = c.get("/入窗缓冲.html").text
        assert "/api/关窗/收口" in 缓 or "/api/关窗/快照" in 缓
        assert "失败指纹" in 缓


def test_贯验页与窗规页读形态():
    with TestClient(app) as c:
        验 = c.get("/窗贯验.html").text
        assert "/api/关窗/收口" in 验
        assert "形态" in 验

        规 = c.get("/窗规.html").text
        assert "/api/关窗/收口" in 规
        assert "窗封印指纹回退" in 规


def test_页面渲染出的数据口径():
    with TestClient(app) as c:
        c.post("/api/观测点/夹具/关窗中断样例")
        c.post("/api/关窗/跑")
        收 = c.get("/api/关窗/收口").json()
        assert 收["形态"] == "回退"
        assert 收["现行片数"] == 0
        assert 收["归档键"] == ""
        assert 收["valid"] is False
        assert 收["趟号"]


def test_空桩列表已剔除已实现():
    with TestClient(app) as c:
        桩 = c.get("/api/空桩").json()
        assert "关窗半截回滚" not in 桩["空桩"]
        assert "关窗半截回滚" in 桩["已实现"]
        assert 桩["条数"] == len(桩["空桩"])
