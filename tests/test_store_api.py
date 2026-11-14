"""Store / API 测试：历史、失败回滚、重启保留、双端并发提交裁决。"""
import json
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import compat
from app.main import create_app
from app.store import Store

W_INT = {"type": "record", "name": "Demo", "fields": [{"name": "v", "type": "int"}]}
W_LONG = {"type": "record", "name": "Demo", "fields": [{"name": "v", "type": "long"}]}
R_LONG = {"type": "record", "name": "Demo", "fields": [{"name": "v", "type": "long"}]}
R_INT = {"type": "record", "name": "Demo", "fields": [{"name": "v", "type": "int"}]}


def j(obj):
    return json.dumps(obj, separators=(",", ":"))


@pytest.fixture()
def st(tmp_path):
    return Store(str(tmp_path / "contracts.db"))


@pytest.fixture()
def client(st):
    return TestClient(create_app(st))


# ---------------- Store 行为 ----------------

def test_create_requires_compatibility(st):
    res = st.create_link("bad", j(W_LONG), j(R_INT), compat.check_compatibility)
    assert not res.ok and res.stage == "pre-check"
    assert st.get_link("bad") is None  # 失败不留链路


def test_create_commits_current_and_history_together(st):
    res = st.create_link("a", j(W_INT), j(R_LONG), compat.check_compatibility)
    assert res.ok
    link = st.get_link("a")
    assert link["version"] == 1
    assert len(link["history"]) == 1 and link["history"][0]["schema"] == W_INT


def test_publish_writer_appends_history(st):
    st.create_link("a", j(W_INT), j(R_LONG), compat.check_compatibility)
    res = st.publish_writer("a", j(W_LONG), compat.check_compatibility)
    assert res.ok
    link = st.get_link("a")
    assert link["writer"] == W_LONG and link["version"] == 2
    assert [h["seq"] for h in link["history"]] == [1, 2]


def test_rejected_writer_keeps_old_contract(st):
    st.create_link("a", j(W_INT), j(R_INT), compat.check_compatibility)
    res = st.publish_writer("a", j(W_LONG), compat.check_compatibility)  # long 写不进 int 读端
    assert not res.ok and res.stage == "pre-check"
    link = st.get_link("a")
    assert link["writer"] == W_INT and len(link["history"]) == 1
    assert any(c["path"] == "$.v" for c in res.conflicts)


def test_reader_must_read_all_history(st):
    st.create_link("a", j(W_INT), j(R_LONG), compat.check_compatibility)
    st.publish_writer("a", j(W_LONG), compat.check_compatibility)  # 历史: [int, long]
    res = st.publish_reader("a", j(R_INT), compat.check_compatibility)
    assert not res.ok
    seqs = {c["writer_seq"] for c in res.conflicts}
    assert 2 in seqs  # 失败来自历史 #2 的 long writer
    assert st.get_link("a")["reader"] == R_LONG  # 旧契约保留


def test_state_survives_reopen(tmp_path):
    db = str(tmp_path / "contracts.db")
    s1 = Store(db)
    s1.create_link("a", j(W_INT), j(R_LONG), compat.check_compatibility)
    s1.publish_writer("a", j(W_LONG), compat.check_compatibility)
    s2 = Store(db)  # 模拟进程重启
    link = s2.get_link("a")
    assert link["writer"] == W_LONG and len(link["history"]) == 2


def test_concurrent_cross_end_publish_never_incompatible(st):
    """int 写端 / long 读端下，同时 writer->long、reader->int（带演示延迟）：
    两端预检都能通过，但提交时按当时状态裁决，必须恰好一个成功。"""
    st.create_link("race", j(W_INT), j(R_LONG), compat.check_compatibility)
    results = {}

    def do_writer():
        results["writer"] = st.publish_writer("race", j(W_LONG), compat.check_compatibility, demo_delay_ms=400)

    def do_reader():
        results["reader"] = st.publish_reader("race", j(R_INT), compat.check_compatibility, demo_delay_ms=400)

    t1, t2 = threading.Thread(target=do_writer), threading.Thread(target=do_reader)
    t1.start(); t2.start(); t1.join(); t2.join()

    oks = [k for k, r in results.items() if r.ok]
    assert len(oks) == 1, f"必须恰好一个成功: {oks}"
    loser = results["writer" if oks[0] == "reader" else "reader"]
    assert not loser.ok and loser.stage == "commit"          # 在提交时裁决阶段被拒
    assert loser.conflicts and loser.conflicts[0]["path"] == "$.v"

    final = st.get_link("race")
    if oks[0] == "writer":   # writer=long 成功 -> reader 仍是 long，组合兼容
        assert final["writer"] == W_LONG and final["reader"] == R_LONG
        assert len(final["history"]) == 2
    else:                    # reader=int 成功 -> writer 仍是 int，组合兼容
        assert final["writer"] == W_INT and final["reader"] == R_INT
        assert len(final["history"]) == 1
    # 最终状态必然兼容：reader 能读当前 writer 及全部历史
    for h in final["history"]:
        assert compat.check_compatibility(
            compat.parse_schema(h["schema"]), compat.parse_schema(final["reader"])).ok


# ---------------- HTTP API ----------------

def test_api_create_publish_query(client):
    r = client.post("/api/links", json={"name": "orders", "writer": W_INT, "reader": R_LONG})
    assert r.status_code == 201, r.text
    assert r.headers["x-server-id"]

    r = client.put("/api/links/orders/writer", json={"schema": W_LONG})
    assert r.status_code == 200 and r.json()["state"]["version"] == 2

    r = client.get("/api/links/orders")
    assert r.status_code == 200
    body = r.json()
    assert len(body["history"]) == 2 and body["writer"]["fields"][0]["type"] == "long"

    r = client.get("/api/links")
    assert any(l["name"] == "orders" for l in r.json()["links"])


def test_api_reject_shows_conflict_paths(client):
    client.post("/api/links", json={"name": "orders", "writer": W_INT, "reader": R_INT})
    r = client.put("/api/links/orders/writer", json={"schema": W_LONG})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["stage"] == "pre-check"
    assert detail["conflicts"][0]["path"] == "$.v"
    # 旧契约保留
    assert client.get("/api/links/orders").json()["writer"] == W_INT


def test_api_invalid_schema_400(client):
    r = client.post("/api/links", json={"name": "x", "writer": {"type": "nope"}, "reader": W_INT})
    assert r.status_code == 400
    r = client.put("/api/links/x/writer", json={"schema": "{not json"})
    assert r.status_code in (400, 404)  # 400: JSON/模式错误先于 404


def test_api_dry_check_does_not_commit(client):
    client.post("/api/links", json={"name": "orders", "writer": W_INT, "reader": R_LONG})
    r = client.post("/api/links/orders/check", json={"end": "reader", "schema": R_INT})
    assert r.status_code == 200 and r.json()["ok"] is True  # 历史只有 int，int 读端可行
    r = client.post("/api/links/orders/check", json={"end": "writer", "schema": {"type": "record", "name": "Demo", "fields": [{"name": "v", "type": "string"}]}})
    assert r.json()["ok"] is False
    assert client.get("/api/links/orders").json()["version"] == 1  # 未提交任何变更
