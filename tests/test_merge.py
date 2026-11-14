"""merge.build_unified_reader 的合并规则测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import compat, merge


def rec(name="Evt", namespace="demo", fields=()):
    s = {"type": "record", "name": name, "fields": list(fields)}
    if namespace:
        s["namespace"] = namespace
    return s


def hist(*schemas):
    return [{"seq": i + 1, "schema": s} for i, s in enumerate(schemas)]


def field_map(reader):
    return {f["name"]: f for f in reader["fields"]}


def assert_reads_all(result, history):
    """生成的候选必须真能读取全部历史 writer。"""
    assert result.ok, [c["message"] for c in result.conflicts]
    reader = compat.parse_schema(result.reader)
    for h in history:
        res = compat.check_compatibility(compat.parse_schema(h["schema"]), reader)
        assert res.ok, f"writer #{h['seq']} 不可读: " + str([c.__dict__ for c in res.conflicts])


# ---------------- 数值提升：共同读端类型 ----------------

def test_numeric_promotion_picks_common_type():
    h = hist(rec(fields=[{"name": "v", "type": "int"}]),
             rec(fields=[{"name": "v", "type": "long"}]),
             rec(fields=[{"name": "v", "type": "int"}]))
    r = merge.build_unified_reader(h)
    assert r.ok and r.writer_seqs == [1, 2, 3]
    assert field_map(r.reader)["v"]["type"] == "long"
    assert_reads_all(r, h)


def test_numeric_chain_to_double():
    h = hist(rec(fields=[{"name": "v", "type": "int"}]),
             rec(fields=[{"name": "v", "type": "double"}]))
    r = merge.build_unified_reader(h)
    assert field_map(r.reader)["v"]["type"] == "double"
    assert_reads_all(r, h)


def test_long_float_merge_to_float():
    h = hist(rec(fields=[{"name": "v", "type": "long"}]),
             rec(fields=[{"name": "v", "type": "float"}]))
    r = merge.build_unified_reader(h)
    assert field_map(r.reader)["v"]["type"] == "float"
    assert_reads_all(r, h)


def test_same_type_kept():
    h = hist(rec(fields=[{"name": "v", "type": "string"}]),
             rec(fields=[{"name": "v", "type": "string"}]))
    r = merge.build_unified_reader(h)
    assert field_map(r.reader)["v"]["type"] == "string"
    assert "default" not in field_map(r.reader)["v"]
    assert_reads_all(r, h)


# ---------------- 其余混合：合法 union 承接 ----------------

def test_mixed_types_become_union():
    h = hist(rec(fields=[{"name": "v", "type": "int"}]),
             rec(fields=[{"name": "v", "type": "string"}]))
    r = merge.build_unified_reader(h)
    assert field_map(r.reader)["v"]["type"] == ["int", "string"]
    assert_reads_all(r, h)


def test_string_bytes_become_union():
    h = hist(rec(fields=[{"name": "v", "type": "string"}]),
             rec(fields=[{"name": "v", "type": "bytes"}]))
    r = merge.build_unified_reader(h)
    assert field_map(r.reader)["v"]["type"] == ["bytes", "string"]
    assert_reads_all(r, h)


def test_numeric_mixed_with_string_full_union():
    h = hist(rec(fields=[{"name": "v", "type": "int"}]),
             rec(fields=[{"name": "v", "type": "long"}]),
             rec(fields=[{"name": "v", "type": "string"}]))
    r = merge.build_unified_reader(h)
    assert field_map(r.reader)["v"]["type"] == ["int", "long", "string"]
    assert_reads_all(r, h)


# ---------------- 可空 / 缺席：null 默认值补齐 ----------------

def test_nullable_union_kept_with_default():
    h = hist(rec(fields=[{"name": "v", "type": ["null", "string"]}]),
             rec(fields=[{"name": "v", "type": "string"}]))
    r = merge.build_unified_reader(h)
    f = field_map(r.reader)["v"]
    assert f["type"] == ["null", "string"] and f["default"] is None
    assert_reads_all(r, h)


def test_missing_field_gets_null_default():
    h = hist(rec(fields=[{"name": "id", "type": "long"}]),
             rec(fields=[{"name": "id", "type": "long"}, {"name": "note", "type": "string"}]))
    r = merge.build_unified_reader(h)
    f = field_map(r.reader)["note"]
    assert f["type"] == ["null", "string"] and f["default"] is None
    assert_reads_all(r, h)


def test_missing_and_nullable_and_promoted():
    h = hist(rec(fields=[{"name": "id", "type": "long"}]),
             rec(fields=[{"name": "id", "type": "long"}, {"name": "n", "type": "int"}]),
             rec(fields=[{"name": "id", "type": "long"}, {"name": "n", "type": ["null", "long"]}]))
    r = merge.build_unified_reader(h)
    f = field_map(r.reader)["n"]
    assert f["type"] == ["null", "long"] and f["default"] is None
    assert_reads_all(r, h)


def test_pure_null_field():
    h = hist(rec(fields=[{"name": "v", "type": "null"}]),
             rec(fields=[{"name": "v", "type": "null"}]))
    r = merge.build_unified_reader(h)
    f = field_map(r.reader)["v"]
    assert f["type"] == "null" and f["default"] is None
    assert_reads_all(r, h)


def test_field_order_is_first_appearance():
    h = hist(rec(fields=[{"name": "b", "type": "int"}]),
             rec(fields=[{"name": "a", "type": "int"}, {"name": "c", "type": "string"}]))
    r = merge.build_unified_reader(h)
    assert [f["name"] for f in r.reader["fields"]] == ["b", "a", "c"]


# ---------------- 范围之外：拒绝并给出字段路径 ----------------

def test_non_record_writer_rejected():
    h = hist("int", rec(fields=[{"name": "v", "type": "int"}]))
    r = merge.build_unified_reader(h)
    assert not r.ok
    assert any(c["writer_seq"] == 1 and c["path"] == "$" for c in r.conflicts)


def test_record_name_mismatch_rejected():
    h = hist(rec(name="A", fields=[{"name": "v", "type": "int"}]),
             rec(name="B", fields=[{"name": "v", "type": "int"}]))
    r = merge.build_unified_reader(h)
    assert not r.ok
    assert any(c["writer_seq"] == 2 and c["path"] == "$" for c in r.conflicts)


def test_same_fullname_different_notation_ok():
    h = hist(rec(name="Evt", namespace="demo", fields=[{"name": "v", "type": "int"}]),
             rec(name="demo.Evt", namespace=None, fields=[{"name": "v", "type": "long"}]))
    r = merge.build_unified_reader(h)
    assert r.ok
    assert field_map(r.reader)["v"]["type"] == "long"


def test_nested_record_field_rejected_with_path():
    h = hist(rec(fields=[{"name": "payload", "type": {"type": "record", "name": "P", "fields": []}}]))
    r = merge.build_unified_reader(h)
    assert not r.ok
    assert any(c["path"] == "$.payload" and c["writer_seq"] == 1 for c in r.conflicts)


def test_array_field_rejected_with_path():
    h = hist(rec(fields=[{"name": "items", "type": {"type": "array", "items": "int"}}]))
    r = merge.build_unified_reader(h)
    assert not r.ok and r.conflicts[0]["path"] == "$.items"


def test_two_branch_non_null_union_rejected():
    h = hist(rec(fields=[{"name": "v", "type": ["int", "string"]}]))
    r = merge.build_unified_reader(h)
    assert not r.ok and r.conflicts[0]["path"] == "$.v"


def test_named_type_reference_rejected():
    h = hist(rec(fields=[{"name": "v", "type": "SomeOther"}]))
    r = merge.build_unified_reader(h)
    assert not r.ok and r.conflicts[0]["path"] == "$.v"


def test_empty_history_rejected():
    r = merge.build_unified_reader([])
    assert not r.ok
