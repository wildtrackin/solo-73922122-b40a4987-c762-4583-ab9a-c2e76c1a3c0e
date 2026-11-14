"""compat.check_compatibility 的规则覆盖测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.compat import check_compatibility, parse_schema


def ok(writer, reader):
    res = check_compatibility(parse_schema(writer), parse_schema(reader))
    assert res.ok, [f"{c.path}: {c.message}" for c in res.conflicts]


def bad(writer, reader, *path_fragments):
    res = check_compatibility(parse_schema(writer), parse_schema(reader))
    assert not res.ok, "预期不兼容但通过了"
    paths = [c.path for c in res.conflicts]
    for frag in path_fragments:
        assert any(frag in p for p in paths), f"未在冲突路径 {paths} 中找到片段 {frag!r}"
    return res


# ---------------- 原始类型与提升 ----------------

def test_primitive_same():
    ok('"int"', '"int"')
    ok('"string"', '"string"')


def test_promotions():
    ok('"int"', '"long"')
    ok('"int"', '"double"')
    ok('"long"', '"float"')
    ok('"float"', '"double"')
    ok('"string"', '"bytes"')
    ok('"bytes"', '"string"')


def test_no_demotion():
    bad('"long"', '"int"')
    bad('"double"', '"float"')
    bad('"string"', '"int"')


# ---------------- record：字段增删、默认值、别名 ----------------

W_USER = {
    "type": "record", "name": "User", "namespace": "com.acme",
    "fields": [{"name": "id", "type": "int"}, {"name": "mail", "type": "string"}],
}


def test_reader_adds_field_with_default():
    r = {"type": "record", "name": "User", "namespace": "com.acme",
         "fields": [{"name": "id", "type": "long"},
                    {"name": "mail", "type": "string"},
                    {"name": "age", "type": "int", "default": 0}]}
    ok(W_USER, r)


def test_reader_adds_field_without_default():
    r = {"type": "record", "name": "User", "namespace": "com.acme",
         "fields": [{"name": "id", "type": "int"}, {"name": "age", "type": "int"}]}
    res = bad(W_USER, r, "$.age")
    assert any("默认值" in c.message for c in res.conflicts)


def test_reader_drops_field_is_fine():
    r = {"type": "record", "name": "User", "namespace": "com.acme",
         "fields": [{"name": "id", "type": "int"}]}
    ok(W_USER, r)


def test_field_type_mismatch_path():
    r = {"type": "record", "name": "User", "namespace": "com.acme",
         "fields": [{"name": "id", "type": "int"}, {"name": "mail", "type": "int"}]}
    bad(W_USER, r, "$.mail")


def test_field_alias_on_reader():
    # reader 字段 email 通过别名匹配 writer 字段 mail
    r = {"type": "record", "name": "User", "namespace": "com.acme",
         "fields": [{"name": "id", "type": "int"},
                    {"name": "email", "type": "string", "aliases": ["mail"]}]}
    ok(W_USER, r)


def test_record_name_alias():
    r = {"type": "record", "name": "Customer", "namespace": "com.acme",
         "aliases": ["User"],  # 相对别名按 reader 命名空间补全
         "fields": [{"name": "id", "type": "int"}]}
    ok(W_USER, r)


def test_record_name_mismatch():
    r = {"type": "record", "name": "Other", "namespace": "com.acme",
         "fields": [{"name": "id", "type": "int"}]}
    bad(W_USER, r, "$")


# ---------------- union ----------------

def test_writer_union_every_branch_must_match():
    bad('["null", "string"]', '"string"')       # null 分支读不了
    ok('["null", "int"]', '["null", "long"]')   # 每个分支都有去处


def test_reader_union_one_branch_is_enough():
    ok('"int"', '["string", "long"]')


def test_reader_union_reports_closest_branch():
    res = bad('{"type":"record","name":"A","fields":[{"name":"x","type":"string"}]}',
              '["int", {"type":"record","name":"A","fields":[{"name":"x","type":"int"}]}]')
    assert any("reader#1" in c.path for c in res.conflicts)


# ---------------- 递归 record / 嵌套容器 ----------------

NODE_W = {
    "type": "record", "name": "Node",
    "fields": [{"name": "v", "type": "int"},
               {"name": "next", "type": ["null", "Node"], "default": None},
               {"name": "kids", "type": {"type": "array", "items": "Node"}},
               {"name": "attrs", "type": {"type": "map", "values": "int"}}],
}


def test_recursive_record_compatible():
    r = {
        "type": "record", "name": "Node",
        "fields": [{"name": "v", "type": "long"},
                   {"name": "next", "type": ["null", "Node"], "default": None},
                   {"name": "kids", "type": {"type": "array", "items": "Node"}},
                   {"name": "attrs", "type": {"type": "map", "values": "double"}}],
    }
    ok(NODE_W, r)


def test_recursive_record_deep_conflict_path():
    r = {
        "type": "record", "name": "Node",
        "fields": [{"name": "v", "type": "string"},   # int -> string 不行
                   {"name": "next", "type": ["null", "Node"], "default": None},
                   {"name": "kids", "type": {"type": "array", "items": "Node"}},
                   {"name": "attrs", "type": {"type": "map", "values": "int"}}],
    }
    bad(NODE_W, r, "$.v")


def test_array_item_conflict_path():
    bad('{"type":"array","items":"long"}', '{"type":"array","items":"int"}', "$[]")


def test_map_value_conflict_path():
    bad('{"type":"map","values":"string"}', '{"type":"map","values":"int"}', "${}")


# ---------------- enum / fixed ----------------

def test_enum_symbol_removed_needs_default():
    w = {"type": "enum", "name": "Color", "symbols": ["R", "G", "B"]}
    r_no_default = {"type": "enum", "name": "Color", "symbols": ["R", "G"]}
    r_with_default = {"type": "enum", "name": "Color", "symbols": ["R", "G"], "default": "R"}
    bad(w, r_no_default)
    ok(w, r_with_default)


def test_enum_alias():
    w = {"type": "enum", "name": "Color", "symbols": ["R"]}
    r = {"type": "enum", "name": "Shade", "aliases": ["Color"], "symbols": ["R", "G"]}
    ok(w, r)


def test_fixed_size_and_alias():
    w = {"type": "fixed", "name": "Hash", "size": 16}
    ok(w, {"type": "fixed", "name": "Hash", "size": 16})
    ok(w, {"type": "fixed", "name": "Digest", "size": 16, "aliases": ["Hash"]})
    bad(w, {"type": "fixed", "name": "Hash", "size": 8})


# ---------------- 跨类型 ----------------

def test_cross_type_mismatch():
    bad('"int"', '{"type":"array","items":"int"}')
    bad('{"type":"record","name":"A","fields":[]}', '{"type":"map","values":"int"}')
