"""SQLite 契约存储：当前 writer/reader + 全部已发布 writer 历史。

并发模型
--------
* 兼容性检查在普通只读连接上执行（自动提交的 SELECT），全程不持有
  SQLite 写锁，任意多个检查/查询可并发进行（WAL 模式下读写互不阻塞）。
* 发布分两个阶段：
    1. 预检：读取当前快照做兼容性检查，失败直接拒绝（不碰写锁）；
    2. 提交：开启 `BEGIN IMMEDIATE` 短事务（此时才取写锁），在事务内
       重新读取已提交状态并**按提交时状态重新裁决**。裁决通过才写入，
       否则 ROLLBACK，旧契约原样保留。
  因此两个并发发布即使各自预检都通过，也不可能都提交成功并留下不兼容
  组合——后提交者会基于先提交者已落库的状态重新检查并被拒绝。
* 当前 writer 与其历史条目在同一事务内一起提交；SQLite 文件持久化，
  进程重启后数据保留。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

from . import compat

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS links (
    name       TEXT PRIMARY KEY,
    writer     TEXT NOT NULL,          -- 当前 writer 模式（紧凑 JSON）
    reader     TEXT NOT NULL,          -- 当前 reader 模式（紧凑 JSON）
    version    INTEGER NOT NULL,       -- 每次成功发布 +1
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS writer_history (
    link         TEXT NOT NULL REFERENCES links(name) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,     -- 从 1 开始单调递增；最大 seq 即当前 writer
    schema       TEXT NOT NULL,
    published_at TEXT NOT NULL,
    PRIMARY KEY (link, seq)
);
"""

# check_fn(writer_schema, reader_schema) -> compat.CheckResult
CheckFn = Callable[[Any, Any], "compat.CheckResult"]


class LinkNotFound(Exception):
    pass


class LinkExists(Exception):
    pass


@dataclass
class PublishResult:
    ok: bool
    stage: str = ""                 # "pre-check" | "commit"（拒绝时所处的裁决阶段）
    conflicts: List[dict] = field(default_factory=list)
    state: Optional[dict] = None    # 成功后的新状态 / 拒绝时库中的当前状态


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _tag(conflicts: List[dict], seq: Optional[int]) -> List[dict]:
    return [{"writer_seq": seq, **c} for c in conflicts]


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")  # 持久化在库文件中，两个进程共享
            conn.executescript(_SCHEMA_SQL)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _link_dict(row: sqlite3.Row, hist: List[sqlite3.Row]) -> dict:
        return {
            "name": row["name"],
            "version": row["version"],
            "writer": json.loads(row["writer"]),
            "reader": json.loads(row["reader"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "history": [
                {"seq": h["seq"], "schema": json.loads(h["schema"]), "published_at": h["published_at"]}
                for h in hist
            ],
        }

    # ------------------------------------------------------------------
    # 只读路径：自动提交的 SELECT，不持有写锁
    # ------------------------------------------------------------------
    def list_links(self) -> List[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT name, version, updated_at FROM links ORDER BY name"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_link(self, name: str) -> Optional[dict]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM links WHERE name = ?", (name,)).fetchone()
            if row is None:
                return None
            hist = conn.execute(
                "SELECT seq, schema, published_at FROM writer_history WHERE link = ? ORDER BY seq",
                (name,),
            ).fetchall()
            return self._link_dict(row, hist)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 创建：初建也须兼容（reader 能读 writer）
    # ------------------------------------------------------------------
    def create_link(self, name: str, writer_json: str, reader_json: str, check_fn: CheckFn) -> PublishResult:
        verdict = check_fn(compat.parse_schema(writer_json), compat.parse_schema(reader_json))
        if not verdict.ok:
            return PublishResult(ok=False, stage="pre-check", conflicts=_tag(verdict.to_list(), 1))

        now = _now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO links(name, writer, reader, version, created_at, updated_at)"
                    " VALUES (?,?,?,1,?,?)",
                    (name, writer_json, reader_json, now, now),
                )
                # 初始 writer 同时作为历史 #1，与当前模式在同一事务提交
                conn.execute(
                    "INSERT INTO writer_history(link, seq, schema, published_at) VALUES (?,?,?,?)",
                    (name, 1, writer_json, now),
                )
                conn.execute("COMMIT")
            except sqlite3.IntegrityError:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise LinkExists(name)
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return PublishResult(ok=True, state=self.get_link(name))

    # ------------------------------------------------------------------
    # 发布新 writer：须能被当前 reader 读取
    # ------------------------------------------------------------------
    def publish_writer(self, name: str, writer_json: str, check_fn: CheckFn, demo_delay_ms: int = 0) -> PublishResult:
        writer_obj = compat.parse_schema(writer_json)

        # 阶段 1：快照预检（无锁）
        snap = self.get_link(name)
        if snap is None:
            raise LinkNotFound(name)
        pre = check_fn(writer_obj, compat.parse_schema(snap["reader"]))
        if not pre.ok:
            return PublishResult(ok=False, stage="pre-check", conflicts=_tag(pre.to_list(), None), state=snap)

        if demo_delay_ms > 0:
            time.sleep(demo_delay_ms / 1000.0)  # 可控演示：放大并发窗口（不持锁）

        # 阶段 2：提交时按库中最新状态重新裁决（持写锁，事务尽量短）
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM links WHERE name = ?", (name,)).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    raise LinkNotFound(name)
                verdict = check_fn(writer_obj, compat.parse_schema(row["reader"]))
                if not verdict.ok:
                    conn.execute("ROLLBACK")
                    return PublishResult(
                        ok=False, stage="commit", conflicts=_tag(verdict.to_list(), None), state=self.get_link(name)
                    )
                seq = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM writer_history WHERE link = ?", (name,)
                ).fetchone()[0]
                now = _now()
                # 当前模式与历史条目在同一事务一起提交
                conn.execute(
                    "INSERT INTO writer_history(link, seq, schema, published_at) VALUES (?,?,?,?)",
                    (name, seq, writer_json, now),
                )
                conn.execute(
                    "UPDATE links SET writer = ?, version = version + 1, updated_at = ? WHERE name = ?",
                    (writer_json, now, name),
                )
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return PublishResult(ok=True, state=self.get_link(name))

    # ------------------------------------------------------------------
    # 发布新 reader：须能读取全部历史 writer（含当前 writer）
    # ------------------------------------------------------------------
    def publish_reader(self, name: str, reader_json: str, check_fn: CheckFn, demo_delay_ms: int = 0) -> PublishResult:
        reader_obj = compat.parse_schema(reader_json)

        def check_all(history: List[dict]) -> List[dict]:
            conflicts: List[dict] = []
            for h in history:
                res = check_fn(compat.parse_schema(h["schema"]), reader_obj)
                conflicts.extend(_tag(res.to_list(), h["seq"]))
            return conflicts

        # 阶段 1：快照预检（无锁）
        snap = self.get_link(name)
        if snap is None:
            raise LinkNotFound(name)
        pre_conflicts = check_all(snap["history"])
        if pre_conflicts:
            return PublishResult(ok=False, stage="pre-check", conflicts=pre_conflicts, state=snap)

        if demo_delay_ms > 0:
            time.sleep(demo_delay_ms / 1000.0)  # 可控演示：放大并发窗口（不持锁）

        # 阶段 2：提交时按库中最新历史重新裁决（持写锁，事务尽量短）
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT 1 FROM links WHERE name = ?", (name,)).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    raise LinkNotFound(name)
                hist = conn.execute(
                    "SELECT seq, schema FROM writer_history WHERE link = ? ORDER BY seq", (name,)
                ).fetchall()
                conflicts = check_all([{"seq": h["seq"], "schema": h["schema"]} for h in hist])
                if conflicts:
                    conn.execute("ROLLBACK")
                    return PublishResult(ok=False, stage="commit", conflicts=conflicts, state=self.get_link(name))
                conn.execute(
                    "UPDATE links SET reader = ?, version = version + 1, updated_at = ? WHERE name = ?",
                    (reader_json, _now(), name),
                )
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return PublishResult(ok=True, state=self.get_link(name))

    # ------------------------------------------------------------------
    def delete_link(self, name: str) -> bool:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("DELETE FROM links WHERE name = ?", (name,))
            conn.execute("COMMIT")
            return cur.rowcount > 0
        finally:
            conn.close()
