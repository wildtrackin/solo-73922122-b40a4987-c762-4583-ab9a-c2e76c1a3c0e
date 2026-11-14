"""FastAPI 入口：契约发布网页 + REST API。

两个进程可共用同一个 SQLite 库文件（DB_PATH 指向共享卷），
兼容检查不持写锁，发布按提交时状态裁决（见 store.py）。
"""
from __future__ import annotations

import os
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import compat, store

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
