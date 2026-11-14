"""FastAPI 入口：契约发布网页 + REST API。

两个进程可共用同一个 SQLite 库文件（DB_PATH 指向共享卷），
兼容检查不持写锁，发布按提交时状态裁决（见 store.py）。
"""
from __future__ import annotations

import os
from typing import Any, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import compat, merge, store

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "contracts.db"))
SERVER_ID = os.environ.get("SERVER_ID", "dev")
MAX_DEMO_DELAY_MS = 10_000
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

_NAME_PATTERN = r"^[A-Za-z0-9_.\-]{1,64}$"


class LinkCreate(BaseModel):
    name: str = Field(pattern=_NAME_PATTERN)
    writer: Any  # Avro 模式：JSON 对象或 JSON 字符串
    reader: Any


class PublishBody(BaseModel):
    schema_: Any = Field(alias="schema")
    demo_delay_ms: int = Field(default=0, ge=0, le=MAX_DEMO_DELAY_MS)

    model_config = {"populate_by_name": True}


class CheckBody(BaseModel):
    end: Literal["writer", "reader"]
    schema_: Any = Field(alias="schema")

    model_config = {"populate_by_name": True}


class CandidateConfirm(BaseModel):
    """确认发布统一 reader 候选：base_version / writer_seqs 来自生成响应，作为乐观并发令牌。"""
    schema_: Any = Field(alias="schema")
    base_version: int = Field(ge=1)
    writer_seqs: List[int] = Field(min_length=1)

    model_config = {"populate_by_name": True}


def _normalize_schema(raw: Any) -> str:
    """校验并规范化模式文本；非法 JSON / 非法 Avro 模式返回 400。"""
    try:
        text = compat.canonical(raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"模式不是合法 JSON: {exc}")
    try:
        compat.parse_schema(text)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Avro 模式解析失败: {exc}")
    return text


def _reject(result: store.PublishResult, what: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": "incompatible",
            "stage": result.stage,  # pre-check: 预检拒绝；commit: 提交时裁决拒绝
            "message": what,
            "conflicts": result.conflicts,
            "state": result.state,
        },
    )


def create_app(st: Optional[store.Store] = None) -> FastAPI:
    app = FastAPI(title="Avro 契约注册中心", version="1.0.0")
    st = st or store.Store(DB_PATH)
    app.state.store = st

    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def _server_header(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Server-Id"] = SERVER_ID
        return resp

    # --------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    @app.get("/api/info")
    def info():
        return {"server_id": SERVER_ID, "db_path": st.path, "links": len(st.list_links())}

    @app.get("/api/links")
    def list_links():
        return {"server_id": SERVER_ID, "links": st.list_links()}

    @app.post("/api/links", status_code=201)
    def create_link(body: LinkCreate):
        writer_json = _normalize_schema(body.writer)
        reader_json = _normalize_schema(body.reader)
        try:
            result = st.create_link(body.name, writer_json, reader_json, compat.check_compatibility)
        except store.LinkExists:
            raise HTTPException(status_code=409, detail={"error": "exists", "message": f"链路 {body.name!r} 已存在"})
        if not result.ok:
            raise _reject(result, "初建链路不兼容：当前 reader 无法读取初始 writer，链路未创建")
        return {"ok": True, "server_id": SERVER_ID, "state": result.state}

    @app.get("/api/links/{name}")
    def get_link(name: str):
        link = st.get_link(name)
        if link is None:
            raise HTTPException(status_code=404, detail=f"链路 {name!r} 不存在")
        return {"server_id": SERVER_ID, **link}

    @app.delete("/api/links/{name}")
    def delete_link(name: str):
        if not st.delete_link(name):
            raise HTTPException(status_code=404, detail=f"链路 {name!r} 不存在")
        return {"ok": True}

    @app.put("/api/links/{name}/writer")
    def publish_writer(name: str, body: PublishBody):
        writer_json = _normalize_schema(body.schema_)
        try:
            result = st.publish_writer(name, writer_json, compat.check_compatibility, body.demo_delay_ms)
        except store.LinkNotFound:
            raise HTTPException(status_code=404, detail=f"链路 {name!r} 不存在")
        if not result.ok:
            raise _reject(result, "新 writer 无法被当前 reader 读取，已保留旧契约")
        return {"ok": True, "server_id": SERVER_ID, "state": result.state}

    @app.put("/api/links/{name}/reader")
    def publish_reader(name: str, body: PublishBody):
        reader_json = _normalize_schema(body.schema_)
        try:
            result = st.publish_reader(name, reader_json, compat.check_compatibility, body.demo_delay_ms)
        except store.LinkNotFound:
            raise HTTPException(status_code=404, detail=f"链路 {name!r} 不存在")
        if not result.ok:
            raise _reject(result, "新 reader 无法读取全部历史 writer，已保留旧契约")
        return {"ok": True, "server_id": SERVER_ID, "state": result.state}

    @app.post("/api/links/{name}/reader-candidates")
    def generate_reader_candidate(name: str):
        """生成统一 reader 候选：读取当前已提交版本的全部 writer 历史并合并字段。

        只读不写库；返回候选模式、覆盖的 writer 序号与所基于的链路版本。
        """
        link = st.get_link(name)
        if link is None:
            raise HTTPException(status_code=404, detail=f"链路 {name!r} 不存在")
        result = merge.build_unified_reader(link["history"])
        if not result.ok:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "unmergeable",
                    "message": "历史 writer 无法合并为统一 reader"
                               "（仅支持：各版均为同名 record，顶层字段为原始类型或 null+原始类型 union）",
                    "conflicts": result.conflicts,
                    "state": link,
                },
            )
        return {
            "ok": True,
            "server_id": SERVER_ID,
            "candidate": {
                "reader": result.reader,
                "writer_seqs": result.writer_seqs,
                "base_version": link["version"],
            },
        }

    @app.post("/api/links/{name}/reader-candidates/confirm")
    def confirm_reader_candidate(name: str, body: CandidateConfirm):
        """确认发布候选：确认前链路被其他进程改过则拒绝旧候选（不覆盖已提交契约）；
        通过时仍须在提交事务内重新通过全部历史兼容检查。"""
        reader_json = _normalize_schema(body.schema_)
        try:
            result = st.confirm_reader(
                name, reader_json, body.base_version, body.writer_seqs, compat.check_compatibility
            )
        except store.LinkNotFound:
            raise HTTPException(status_code=404, detail=f"链路 {name!r} 不存在")
        if not result.ok:
            if result.error == "stale":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "stale",
                        "stage": result.stage,
                        "message": "确认前链路已被其他进程修改，旧候选已拒绝；"
                                   "已提交的 reader 与 writer 历史未被覆盖",
                        "state": result.state,
                    },
                )
            raise _reject(result, "统一 reader 未通过全部历史兼容检查，已保留旧契约")
        return {"ok": True, "server_id": SERVER_ID, "state": result.state}

    @app.post("/api/links/{name}/check")
    def dry_check(name: str, body: CheckBody):
        """试算：只检查不提交（不持写锁）。"""
        schema_json = _normalize_schema(body.schema_)
        link = st.get_link(name)
        if link is None:
            raise HTTPException(status_code=404, detail=f"链路 {name!r} 不存在")
        obj = compat.parse_schema(schema_json)
        if body.end == "writer":
            res = compat.check_compatibility(obj, compat.parse_schema(link["reader"]))
            conflicts = [{"writer_seq": None, **c} for c in res.to_list()]
        else:
            conflicts = []
            for h in link["history"]:
                res = compat.check_compatibility(compat.parse_schema(h["schema"]), obj)
                conflicts.extend({"writer_seq": h["seq"], **c} for c in res.to_list())
        return {"ok": not conflicts, "conflicts": conflicts, "server_id": SERVER_ID}

    return app


app = create_app()
