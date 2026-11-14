"""把同一链路的全部历史 writer 合并为一个统一 reader 候选。

适用范围（不满足则拒绝合并并给出字段路径）：
  * 每版 writer 都是 record，且全名（namespace + name）一致；
  * 顶层字段类型只能是：原始类型，或 null 与一种原始类型组成的二分支 union。

合并规则：
  * 同一字段在各版本类型一致 -> 沿用该类型；
  * 各版本都是数值类型（int/long/float/double）-> 取提升链上的共同读端类型
    （链上最大者，如 int+long -> long，int+double -> double）；
  * 其余混合 -> 用合法 union 承接（分支按固定顺序去重，null 永远排第一）；
  * 字段在某些版本缺席、或在某些版本可空 -> 并入 null 分支并补 null 默认值，
    使统一 reader 能从缺该字段的旧版本数据中读出 null。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

PRIMITIVES = {"null", "boolean", "int", "long", "float", "double", "bytes", "string"}
# Avro 数值提升链：int -> long -> float -> double；共同读端类型取链上最大者
NUMERIC_CHAIN = ["int", "long", "float", "double"]
# union 非 null 分支的固定输出顺序（保证候选模式确定可复现）
_BRANCH_ORDER = ["boolean", "int", "long", "float", "double", "bytes", "string"]


@dataclass
class MergeResult:
    ok: bool
    reader: Optional[dict] = None                 # 候选统一 reader 模式
    writer_seqs: List[int] = field(default_factory=list)  # 覆盖的 writer 序号
    conflicts: List[dict] = field(default_factory=list)   # {writer_seq, path, message}


class _Unsupported(Exception):
    """字段类型超出可承接范围。"""


def _fullname(rec: dict) -> str:
    """record 全名：name 含点即全名，否则按 namespace 补全。"""
    name = str(rec.get("name", ""))
    if "." in name:
        return name
    ns = rec.get("namespace")
    return f"{ns}.{name}" if ns else name


def _field_base(ftype: Any) -> Tuple[bool, Optional[str]]:
    """把字段类型归一为 (是否可空, 非 null 原始类型)；无法承接时抛 _Unsupported。"""
    if isinstance(ftype, str):
        if ftype == "null":
            return True, None
        if ftype in PRIMITIVES:
            return False, ftype
        raise _Unsupported(f'命名类型引用 "{ftype}" 不是原始类型')
    if isinstance(ftype, list):
        non_null = [b for b in ftype if b != "null"]
        if (len(ftype) == 2 and len(non_null) == 1
                and isinstance(non_null[0], str) and non_null[0] in PRIMITIVES):
            return True, non_null[0]
        raise _Unsupported("union 必须是 null 与一种原始类型组成的二分支 union")
    raise _Unsupported("字段类型不是原始类型，也不是 null+原始类型 union")


def build_unified_reader(history: List[dict]) -> MergeResult:
    """history: [{"seq": int, "schema": <已解码 JSON>}]（同一已提交版本的全部 writer 历史）。"""
    seqs = [h["seq"] for h in history]
    conflicts: List[dict] = []
    if not history:
        return MergeResult(ok=False, conflicts=[
            {"writer_seq": None, "path": "$", "message": "链路没有 writer 历史，无法生成"}])

    # 1) 每版都必须是 record，且全名一致
    records: List[dict] = []
    fullname: Optional[str] = None
    for h in history:
        seq, schema = h["seq"], h["schema"]
        if not isinstance(schema, dict) or schema.get("type") != "record":
            conflicts.append({"writer_seq": seq, "path": "$",
                              "message": f"writer #{seq} 不是 record 模式，不在统一 reader 生成范围内"})
            continue
        fn = _fullname(schema)
        if fullname is None:
            fullname = fn
        elif fn != fullname:
            conflicts.append({"writer_seq": seq, "path": "$",
                              "message": f'record 全名不一致: writer #{seq} 为 "{fn}"，此前版本为 "{fullname}"'})
        if not isinstance(schema.get("fields"), list):
            conflicts.append({"writer_seq": seq, "path": "$",
                              "message": f"writer #{seq} 缺少 fields 列表"})
            continue
        records.append(schema)
    if conflicts:
        return MergeResult(ok=False, conflicts=conflicts)

    # 2) 逐字段归一并校验形态；字段顺序取首次出现顺序（保留历史字段）
    order: List[str] = []
    slots: Dict[str, dict] = {}
    for h, rec in zip(history, records):
        seq = h["seq"]
        for f in rec["fields"]:
            fname, ftype = f.get("name"), f.get("type")
            try:
                nullable, base = _field_base(ftype)
            except _Unsupported as exc:
                conflicts.append({"writer_seq": seq, "path": f"$.{fname}",
                                  "message": f"字段 {fname!r} 无法承接: {exc}"})
                continue
            slot = slots.get(fname)
            if slot is None:
                slot = slots[fname] = {"nullable": False, "bases": set(), "present": 0}
                order.append(fname)
            slot["nullable"] = slot["nullable"] or nullable
            if base is not None:
                slot["bases"].add(base)
            slot["present"] += 1
    if conflicts:
        return MergeResult(ok=False, conflicts=conflicts)

    # 3) 生成合并字段
    out_fields: List[dict] = []
    for fname in order:
        slot = slots[fname]
        bases = slot["bases"]
        nullable = slot["nullable"] or slot["present"] < len(records)  # 缺席版本以 null 补齐
        if not bases:
            # 所有版本都是纯 null
            out_fields.append({"name": fname, "type": "null", "default": None})
            continue
        if len(bases) == 1:
            core: Any = next(iter(bases))
        elif all(b in NUMERIC_CHAIN for b in bases):
            core = max(bases, key=NUMERIC_CHAIN.index)  # 数值提升：共同读端类型
        else:
            core = [b for b in _BRANCH_ORDER if b in bases]  # 其余：合法 union 承接
        if nullable:
            branches = ["null"] + (core if isinstance(core, list) else [core])
            out_fields.append({"name": fname, "type": branches, "default": None})
        else:
            out_fields.append({"name": fname, "type": core})

    # 4) 候选 record 沿用首版 writer 的 name/namespace（各版全名已校验一致）
    first = records[0]
    reader: Dict[str, Any] = {"type": "record", "name": first.get("name")}
    if first.get("namespace") and "." not in str(first.get("name")):
        reader["namespace"] = first["namespace"]
    reader["fields"] = out_fields
    return MergeResult(ok=True, reader=reader, writer_seqs=seqs)
