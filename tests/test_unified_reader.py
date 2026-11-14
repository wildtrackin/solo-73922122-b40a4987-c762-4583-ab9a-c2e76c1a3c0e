"""统一 reader 生成 / 确认发布：store 与 API 行为测试。"""
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

W1 = {"type": "record", "name": "Evt", "namespace": "demo",
      "fields": [{"name": "id", "type": "long"}, {"name": "n", "type": "int"}]}
W2 = {"type": "record", "name": "Evt", "namespace": "demo",
      "fields": [{"name": "id", "type": "long"}, {"name": "n", "type": "long"},
                 {"name": "note", "type": ["null", "string"], "default": None}]}
R0 = {"type": "record", "name": "Evt", "namespace": "demo",
      "fields": [{"name": "id", "type": "long"}]}
# 不可合并链路：字段是嵌套 record
W_NESTED = {"type": "record", "name": "Evt", "fields":
            [{"name": "payload", "type": {"type": "record", "name": "P", "fields": []}}]}


def j(obj):
    return json.dumps(obj, separators=(",", ":"))


@pytest.fixture()
def st(tmp_path):
    return Store(str(tmp_path / "contracts.db"))


@pytest.fixture()
def client(st):
    return TestClient(create_app(st))


def make_link(st, name="a"):
    st.create_link(name, j(W1), j(R0), compat.check_compatibility)
    st.publish_writer(name, j(W2), compat.check_compatibility)  # 历史: [W1, W2]


# ---------------- Store：生成 + 确认 ----------------

def test_confirm_success_updates_reader_only(st):
    make_link(st)
    link = st.get_link("a")
    from app import merge
    cand = merge.build_unified_reader(link["history"])
    assert cand.ok
    res = st.confirm_reader("a", j(cand.reader), link["version"], cand.writer_seqs,
                            compat.check_compatibility)
    assert res.ok
    after = st.get_link("a")
    assert after["reader"] == cand.reader and after["version"] == 3
    assert after["writer"] == W2                       # 当前 writer 不动
    assert [h["schema"] for h in after["history"]] == [W1, W2]  # 历史不动
    # 统一 reader 能读全部历史
    for h in after["history"]:
        assert compat.check_compatibility(
            compat.parse_schema(h["schema"]), compat.parse_schema(after["reader"])).ok


def test_confirm_stale_after_other_publish(st):
    make_link(st)
    link = st.get_link("a")
    from app import merge
    cand = merge.build_unified_reader(link["history"])
    base_version = link["version"]
    # 另一进程在确认前发布了新 writer（链路版本变化）
    st.publish_writer("a", j(W1), compat.check_compatibility)
    res = st.confirm_reader("a", j(cand.reader), base_version, cand.writer_seqs,
                            compat.check_compatibility)
    assert not res.ok and res.error == "stale"
    after = st.get_link("a")
    assert after["reader"] == R0                       # 已提交 reader 未被覆盖
    assert len(after["history"]) == 3                  # writer 历史未被覆盖


def test_confirm_stale_after_reader_publish(st):
    make_link(st)
    link = st.get_link("a")
    from app import merge
    cand = merge.build_unified_reader(link["history"])
    st.publish_reader("a", j(W2), compat.check_compatibility)  # 另一端改过 reader
    res = st.confirm_reader("a", j(cand.reader), link["version"], cand.writer_seqs,
                            compat.check_compatibility)
    assert not res.ok and res.error == "stale"
    assert st.get_link("a")["reader"] == W2


def test_confirm_rechecks_history_compatibility(st):
    """候选本身读不了全部历史时（如被手工改过），即使版本未变也必须拒绝。"""
    make_link(st)
    link = st.get_link("a")
    bad = {"type": "record", "name": "Evt", "namespace": "demo",
           "fields": [{"name": "id", "type": "long"}, {"name": "n", "type": "string"}]}
    res = st.confirm_reader("a", j(bad), link["version"], [1, 2], compat.check_compatibility)
    assert not res.ok and res.error == "incompatible"
    assert any(c["writer_seq"] == 2 for c in res.conflicts)  # n: long 读不进 string
    assert st.get_link("a")["reader"] == R0


def test_concurrent_confirms_exactly_one_succeeds(st):
    make_link(st)
    link = st.get_link("a")
    from app import merge
    cand = merge.build_unified_reader(link["history"])
    args = ("a", j(cand.reader), link["version"], cand.writer_seqs, compat.check_compatibility)
    results = []

    def confirm():
        results.append(st.confirm_reader(*args))

    t1, t2 = threading.Thread(target=confirm), threading.Thread(target=confirm)
    t1.start(); t2.start(); t1.join(); t2.join()
    oks = [r for r in results if r.ok]
    assert len(oks) == 1
    loser = [r for r in results if not r.ok][0]
    assert loser.error == "stale" and loser.stage == "commit"
    assert st.get_link("a")["version"] == 3            # 只提交了一次


# ---------------- HTTP API ----------------

def test_api_generate_and_confirm(client):
    r = client.post("/api/links", json={"name": "orders", "writer": W1, "reader": R0})
    assert r.status_code == 201
    client.put("/api/links/orders/writer", json={"schema": W2})

    r = client.post("/api/links/orders/reader-candidates")
    assert r.status_code == 200, r.text
    cand = r.json()["candidate"]
    assert cand["writer_seqs"] == [1, 2]
    assert cand["base_version"] == 2
    fields = {f["name"]: f for f in cand["reader"]["fields"]}
    assert fields["n"]["type"] == "long"                       # int+long -> long
    assert fields["note"]["type"] == ["null", "string"]        # 缺席版本补 null
    assert fields["note"]["default"] is None

    r = client.post("/api/links/orders/reader-candidates/confirm",
                    json={"schema": cand["reader"], "base_version": cand["base_version"],
                          "writer_seqs": cand["writer_seqs"]})
    assert r.status_code == 200, r.text
    state = r.json()["state"]
    assert state["version"] == 3 and state["reader"] == cand["reader"]
    assert [h["schema"] for h in state["history"]] == [W1, W2]  # 历史未被覆盖


def test_api_generate_unmergeable_422_with_paths(client):
    client.post("/api/links", json={"name": "n", "writer": W_NESTED, "reader": W_NESTED})
    r = client.post("/api/links/n/reader-candidates")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "unmergeable"
    assert detail["conflicts"][0]["path"] == "$.payload"
    assert client.get("/api/links/n").json()["version"] == 1   # 未写入任何东西


def test_api_confirm_stale_409_keeps_committed_state(client):
    client.post("/api/links", json={"name": "orders", "writer": W1, "reader": R0})
    cand = client.post("/api/links/orders/reader-candidates").json()["candidate"]
    # 另一“进程”在确认前发布新 writer（同一个库，模拟并发修改）
    client.put("/api/links/orders/writer", json={"schema": W2})
    r = client.post("/api/links/orders/reader-candidates/confirm",
                    json={"schema": cand["reader"], "base_version": cand["base_version"],
                          "writer_seqs": cand["writer_seqs"]})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "stale"
    state = client.get("/api/links/orders").json()
    assert state["reader"] == R0                               # reader 未被覆盖
    assert len(state["history"]) == 2 and state["version"] == 2


def test_api_confirm_incompatible_candidate_409(client):
    client.post("/api/links", json={"name": "orders", "writer": W1, "reader": R0})
    cand = client.post("/api/links/orders/reader-candidates").json()["candidate"]
    bad = {"type": "record", "name": "Evt", "namespace": "demo",
           "fields": [{"name": "id", "type": "string"}]}       # id: long 读不进 string
    r = client.post("/api/links/orders/reader-candidates/confirm",
                    json={"schema": bad, "base_version": cand["base_version"],
                          "writer_seqs": cand["writer_seqs"]})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "incompatible"
    assert client.get("/api/links/orders").json()["reader"] == R0


def test_api_generate_and_confirm_missing_link_404(client):
    assert client.post("/api/links/ghost/reader-candidates").status_code == 404
    r = client.post("/api/links/ghost/reader-candidates/confirm",
                    json={"schema": R0, "base_version": 1, "writer_seqs": [1]})
    assert r.status_code == 404


def test_api_existing_publish_still_works(client):
    """原有发布功能不受新端点影响。"""
    client.post("/api/links", json={"name": "orders", "writer": W1, "reader": R0})
    r = client.put("/api/links/orders/reader", json={"schema": W2})
    assert r.status_code == 200 and r.json()["state"]["reader"] == W2
    r = client.put("/api/links/orders/writer", json={"schema": W1})
    assert r.status_code == 200 and r.json()["state"]["version"] == 3
