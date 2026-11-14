"""Avro writer->reader 兼容性检查（Schema Resolution），拒绝时给出冲突路径。

依据 Avro 规范 "Schema Resolution" 一节实现：
  * 原始类型提升: int->long/float/double, long->float/double, float->double,
    string<->bytes；
  * record: 按 fullname 或 reader 端 aliases 匹配；reader 字段可按名称或
    字段 aliases 找到 writer 字段，找不到时 reader 字段必须有默认值；
  * union: writer union 的每个分支都必须能被 reader 读取；reader union
    只要有一个分支能读 writer 即可（拒绝时报告最接近分支的冲突）；
  * enum: 名称匹配，reader 必须覆盖全部 writer 符号，否则需声明 default；
  * fixed: 名称匹配且 size 相等；
  * array/map: 递归检查元素/值类型；
  * 递归 record 通过 (writer, reader) 对象对守卫终止，逻辑类型按底层类型比较。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, List, Optional, Set, Tuple

import avro.schema
from avro.schema import (
    ArraySchema,
    EnumSchema,
    FixedSchema,
    MapSchema,
    NamedSchema,
    RecordSchema,
    Schema,
    UnionSchema,
)

# Avro 规范允许的原始类型提升: writer 类型 -> 可读取它的 reader 类型集合
_PROMOTIONS = {
    "int": {"long", "float", "double"},
    "long": {"float", "double"},
    "float": {"double"},
    "string": {"bytes"},
    "bytes": {"string"},
}

_PRIMITIVES = {"null", "boolean", "int", "long", "float", "double", "bytes", "string"}


@dataclass
class Conflict:
    path: str
    message: str


@dataclass
class CheckResult:
    ok: bool
    conflicts: List[Conflict] = field(default_factory=list)

    def to_list(self) -> List[dict]:
        return [{"path": c.path, "message": c.message} for c in self.conflicts]


def parse_schema(raw: Any) -> Schema:
    """把 JSON 字符串或已解码对象解析为 avro Schema（解析失败抛 avro 异常）。"""
    if not isinstance(raw, str):
        raw = json.dumps(raw)
    return avro.schema.parse(raw)


def canonical(raw: Any) -> str:
    """规范化存储形式：紧凑 JSON 文本。"""
    obj = json.loads(raw) if isinstance(raw, str) else raw
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def check_compatibility(writer: Schema, reader: Schema) -> CheckResult:
    """reader 能否读取 writer 产出的数据。冲突按路径去重后返回。"""
    checker = _Checker()
    checker.check(writer, reader, "$")
    seen: Set[Tuple[str, str]] = set()
    uniq: List[Conflict] = []
    for c in checker.conflicts:
        key = (c.path, c.message)
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return CheckResult(ok=not uniq, conflicts=uniq)


def _label(s: Schema) -> str:
    return s.fullname if isinstance(s, NamedSchema) else s.type


def _aliases(s: Schema) -> List[str]:
    return s.get_prop("aliases") or []


def _qualify(alias: str, namespace: Optional[str]) -> str:
    """相对别名按 reader 命名空间补全为全名。"""
    if "." in alias or not namespace:
        return alias
    return f"{namespace}.{alias}"


def _named_match(writer: NamedSchema, reader: NamedSchema) -> bool:
    """reader 的全名或其别名（在 reader 命名空间下）命中 writer 全名。"""
    if writer.fullname == reader.fullname:
        return True
    ns = getattr(reader, "namespace", None)
    return any(_qualify(a, ns) == writer.fullname for a in _aliases(reader))


class _Checker:
    def __init__(self) -> None:
        self.conflicts: List[Conflict] = []
        self._active: Set[Tuple[int, int]] = set()  # 递归守卫：当前栈上的 (writer, reader) 对
        self._ok: Set[Tuple[int, int]] = set()      # 已通过的无冲突对（菱形结构去重）

    def check(self, writer: Schema, reader: Schema, path: str) -> None:
        key = (id(writer), id(reader))
        if key in self._active or key in self._ok:
            return
        self._active.add(key)
        before = len(self.conflicts)
        try:
            self._dispatch(writer, reader, path)
        finally:
            self._active.discard(key)
        if len(self.conflicts) == before:
            self._ok.add(key)

    def _fail(self, path: str, message: str) -> None:
        self.conflicts.append(Conflict(path, message))

    # ------------------------------------------------------------------
    def _dispatch(self, w: Schema, r: Schema, path: str) -> None:
        # writer 是 union：每个分支都必须能被 reader 读取
        if isinstance(w, UnionSchema):
            for i, branch in enumerate(w.schemas):
                self.check(branch, r, f"{path}|writer#{i}({_label(branch)})")
            return
        # reader 是 union：至少一个分支能读 writer；全失败时报告最接近分支的冲突
        if isinstance(r, UnionSchema):
            trials: List[Tuple[List[Conflict], int]] = []
            for i, branch in enumerate(r.schemas):
                sub = _Checker()
                sub._active = set(self._active)
                sub._ok = self._ok  # 无冲突结论与路径无关，可共享
                sub.check(w, branch, f"{path}|reader#{i}({_label(branch)})")
                if not sub.conflicts:
                    return
                # 打分：冲突越少越好；并列时优先与 writer 同类型的分支
                penalty = 0 if branch.type == w.type else 1
                trials.append((sub.conflicts, penalty))
            best, _ = min(trials, key=lambda t: (len(t[0]), t[1]))
            self._fail(path, f"writer 不匹配 reader union 的任何一个分支（共 {len(trials)} 个），以下为最接近分支的冲突")
            self.conflicts.extend(best)
            return

        wt, rt = w.type, r.type
        # 原始类型：相同或允许的提升
        if wt in _PRIMITIVES or rt in _PRIMITIVES:
            if wt == rt and wt in _PRIMITIVES:
                return
            if rt in _PROMOTIONS.get(wt, set()):
                return
            self._fail(path, f'writer 类型 "{wt}" 无法被 reader 类型 "{rt}" 读取')
            return
        # record
        if isinstance(w, RecordSchema) and isinstance(r, RecordSchema):
            self._check_record(w, r, path)
            return
        # array / map
        if isinstance(w, ArraySchema) and isinstance(r, ArraySchema):
            self.check(w.items, r.items, path + "[]")
            return
        if isinstance(w, MapSchema) and isinstance(r, MapSchema):
            self.check(w.values, r.values, path + "{}")
            return
        # enum
        if isinstance(w, EnumSchema) and isinstance(r, EnumSchema):
            self._check_enum(w, r, path)
            return
        # fixed
        if isinstance(w, FixedSchema) and isinstance(r, FixedSchema):
            self._check_fixed(w, r, path)
            return
        self._fail(path, f'writer 类型 "{wt}" 与 reader 类型 "{rt}" 不匹配')

    # ------------------------------------------------------------------
    def _check_record(self, w: RecordSchema, r: RecordSchema, path: str) -> None:
        if not _named_match(w, r):
            self._fail(
                path,
                f'record 名称不匹配: writer "{w.fullname}" vs reader "{r.fullname}"（reader 别名也未命中）',
            )
            return
        wfields = {f.name: f for f in w.fields}
        for rf in r.fields:
            wf = wfields.get(rf.name)
            via = ""
            if wf is None:
                for alias in rf.get_prop("aliases") or []:
                    if alias in wfields:
                        wf = wfields[alias]
                        via = f'（经别名 "{alias}" 匹配）'
                        break
            if wf is None:
                if rf.has_default:
                    continue
                self._fail(
                    f"{path}.{rf.name}",
                    f'reader 字段 "{rf.name}" 在 writer 中无对应字段且未声明默认值',
                )
                continue
            self.check(wf.type, rf.type, f"{path}.{rf.name}{via}")

    def _check_enum(self, w: EnumSchema, r: EnumSchema, path: str) -> None:
        if not _named_match(w, r):
            self._fail(
                path,
                f'enum 名称不匹配: writer "{w.fullname}" vs reader "{r.fullname}"（reader 别名也未命中）',
            )
            return
        missing = [s for s in w.symbols if s not in r.symbols]
        if missing and r.get_prop("default") is None:
            self._fail(
                path,
                f"reader enum 无法解析 writer 符号 {missing} 且未声明 default",
            )

    def _check_fixed(self, w: FixedSchema, r: FixedSchema, path: str) -> None:
        if not _named_match(w, r):
            self._fail(
                path,
                f'fixed 名称不匹配: writer "{w.fullname}" vs reader "{r.fullname}"（reader 别名也未命中）',
            )
            return
        if w.size != r.size:
            self._fail(path, f"fixed 长度不匹配: writer {w.size} 字节 vs reader {r.size} 字节")
