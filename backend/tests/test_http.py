"""HTTP 层：登录、设置、批次裁决、409 结构、重复列表、时刻详情。"""
import os

from fastapi.testclient import TestClient

from support import check, finish, reset_db
import db
from app import app

failures = []
reset_db()


def run(client):
    r = client.post("/api/login", json={"username": "admin", "password": "123456"})
    check("登录", r.status_code == 200, failures)
    H = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = client.post("/api/login", json={"username": "archive", "password": "arch123456"})
    RH = {"Authorization": f"Bearer {r.json()['access_token']}"}

    check("未登录 401", client.get("/api/batches").status_code == 401, failures)
    check("档案员改设置 403",
          client.put("/api/dedup-setting", headers=RH, json={"policy": "overwrite"}).status_code == 403,
          failures)
    check("档案员归集 403",
          client.post("/api/batches", headers=RH,
                      json={"title": "x", "raw_text": "AA01|202611140900|1|A",
                            "duplicate_policy": "reject"}).status_code == 403, failures)

    r = client.get("/api/dedup-setting", headers=H)
    check("设置初始为空且未锁", r.json()["policy"] is None and r.json()["locked"] is False, failures)
    check("设置带两个选项",
          {o["value"] for o in r.json()["options"]} == {"reject", "overwrite"}, failures)

    check("非法策略 400",
          client.post("/api/batches", headers=H,
                      json={"title": "坏", "raw_text": "AA01|202611140900|1|A",
                            "duplicate_policy": "nope"}).status_code == 400, failures)

    r = client.post("/api/batches", headers=H,
                    json={"title": "首批", "raw_text": "AA01|202611140900|100|A",
                          "duplicate_policy": "overwrite"})
    check("首批 200", r.status_code == 200, failures)
    check("首批落入1", r.json()["batch"]["accepted_count"] == 1, failures)
    check("设置已锁为覆盖",
          client.get("/api/dedup-setting", headers=H).json()["policy"] == "overwrite", failures)
    check("锁定日换策略 409",
          client.put("/api/dedup-setting", headers=H, json={"policy": "reject"}).status_code == 409,
          failures)

    r = client.post("/api/batches", headers=H,
                    json={"title": "覆盖批", "raw_text": "AA01|202611140900|128|M",
                          "duplicate_policy": "overwrite"})
    body = r.json()
    check("覆盖批 overwrite_count=1", body["batch"]["overwrite_count"] == 1, failures)
    check("对照卡片带覆盖留痕",
          body["overwrites"][0]["old_tide_cm"] == 100 and body["overwrites"][0]["new_tide_cm"] == 128,
          failures)
    check("落库厘米更新", body["landed"][0]["tide_cm"] == 128, failures)

    r = client.post("/api/batches", headers=H,
                    json={"title": "不符批",
                          "raw_text": "AA01|202611140900|130|A\nZZ99|202611140900|1|A",
                          "duplicate_policy": "reject"})
    check("不符 409", r.status_code == 409, failures)
    payload = r.json()["detail"]
    check("409 带消息和批次",
          bool(payload["message"]) and payload["batch"]["batch"]["status"] == "未接纳", failures)
    check("不符批次含一条 mismatch",
          payload["batch"]["rejected"][0]["kind"] == "mismatch"
          and payload["batch"]["rejected"][0]["kind_label"] == "处理不符", failures)
    check("停下的后续行未另开批次",
          all(b["title"] != "ZZ99" for b in client.get("/api/batches", headers=H).json()), failures)

    dupes = client.get("/api/duplicates", headers=H).json()
    aa = [d for d in dupes if d["station_code"] == "AA01"][0]
    check("重复列表字段齐全",
          aa["overwrite_count"] == 1 and aa["handling"] == "覆盖厘米" and aa["section"] == "overwrite",
          failures)
    wg = [d for d in dupes if d["station_code"] == "WG01"]
    check("种子拒绝重复分拒绝区",
          bool(wg) and wg[0]["section"] == "reject" and wg[0]["handling"] == "拒绝重复", failures)

    d = client.get("/api/duplicates/AA01/202611140900", headers=H).json()
    check("详情当前厘米", d["landed"]["tide_cm"] == 128, failures)
    check("详情覆盖链新旧对照",
          [(h["old_tide_cm"], h["new_tide_cm"]) for h in d["history"]] == [(100, 128)], failures)
    check("坏时刻 400",
          client.get("/api/duplicates/AA01/9999", headers=H).status_code == 400, failures)

    rj = client.post("/api/batches", headers=H,
                     json={"title": "空时刻批", "raw_text": "BB02||55|A",
                           "duplicate_policy": "overwrite"}).json()["rejected"]
    check("空时刻未接纳",
          rj[0]["reason"] == "观测时刻空着，不参与去重" and rj[0]["observed_at"] is None, failures)

    bid = client.get("/api/batches", headers=H).json()[0]["id"]
    check("批次详情四段齐全",
          set(client.get(f"/api/batches/{bid}", headers=H).json().keys())
          == {"batch", "landed", "rejected", "overwrites"}, failures)


with TestClient(app) as client:
    run(client)

finish(failures)
