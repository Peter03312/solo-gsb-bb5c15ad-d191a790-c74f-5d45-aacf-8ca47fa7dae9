"""HTTP API 与页面入口。"""
from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import rules as engine
from .storage import Store

app = FastAPI(title="回收分拣来料污染判定", version="1.0.0")

_store = Store()


def get_store() -> Store:
    return _store


# ------------------------------------------------------------------ 请求模型
class MaterialItem(BaseModel):
    category: str = Field(min_length=1, max_length=32)
    contamination_kg: float


class AssessmentIn(BaseModel):
    batch_no: str = Field(min_length=1, max_length=64)
    region: str = Field(min_length=1, max_length=32)
    weight_kg: float
    materials: list[MaterialItem] = Field(min_length=1)
    rule_version: str | None = Field(default=None, max_length=32)
    operator: str | None = Field(default=None, max_length=32)


class RuleIn(BaseModel):
    region: str = Field(min_length=1, max_length=32)
    version: str = Field(min_length=1, max_length=32)
    currency: str = Field(default="CNY", max_length=8)
    categories: dict[str, dict[str, Any]]
    levels: list[dict[str, Any]]
    activate: bool = False
    actor: str | None = Field(default=None, max_length=32)


class ActivateIn(BaseModel):
    version: str = Field(min_length=1, max_length=32)
    actor: str | None = Field(default=None, max_length=32)


# ------------------------------------------------------------------ 错误处理
def _err(errors: list[dict], status: int = 422) -> JSONResponse:
    return JSONResponse(status_code=status, content={"errors": errors})


@app.exception_handler(RequestValidationError)
async def _validation_handler(_: Request, exc: RequestValidationError):
    errors = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e["loc"] if p != "body")
        errors.append({"field": loc or "body", "message": e["msg"]})
    return _err(errors)


# ------------------------------------------------------------------ 规则接口
@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/rules")
def list_rules(region: str | None = None, store: Store = Depends(get_store)):
    return {"rules": store.list_rules(region)}


@app.get("/api/rules/{region}/active")
def active_rule(region: str, store: Store = Depends(get_store)):
    rule = store.get_active_rule(region)
    if not rule:
        return _err([{"field": "region",
                      "message": f"区域 {region} 没有启用中的规则版本"}], 404)
    return rule


@app.post("/api/rules", status_code=201)
def create_rule(payload: RuleIn, store: Store = Depends(get_store)):
    body = payload.model_dump()
    errors = engine.validate_rule(body)
    if errors:
        return _err(errors)
    try:
        store.create_rule(body)
    except ValueError as exc:
        return _err([{"field": "version", "message": str(exc)}], 409)
    store.add_audit(payload.actor, "rule.created", "rule",
                    f"{payload.region}/{payload.version}",
                    {"region": payload.region, "version": payload.version,
                     "activate": payload.activate})
    return {"region": payload.region, "version": payload.version,
            "status": "active" if payload.activate else "inactive"}


@app.post("/api/rules/{region}/activate")
def activate_rule(region: str, payload: ActivateIn,
                  store: Store = Depends(get_store)):
    if not store.activate_rule(region, payload.version):
        return _err([{"field": "version",
                      "message": f"区域 {region} 不存在规则版本 {payload.version}"}],
                    404)
    store.add_audit(payload.actor, "rule.activated", "rule",
                    f"{region}/{payload.version}",
                    {"region": region, "version": payload.version})
    return {"region": region, "active_version": payload.version}


# ---------------------------------------------------------------- 判定单接口
def _resolve_rule(store: Store, region: str, version: str | None):
    """解析本次判定使用的规则版本；失败时返回字段错误。"""
    if version:
        rule = store.get_rule(region, version)
        if not rule:
            return None, {"field": "rule_version",
                          "message": f"区域 {region} 不存在规则版本 {version}"}
        return rule, None
    rule = store.get_active_rule(region)
    if not rule:
        if store.list_rules(region):
            return None, {"field": "region",
                          "message": f"区域 {region} 没有启用中的规则版本"}
        return None, {"field": "region", "message": f"未知来源区域：{region}"}
    return rule, None


@app.post("/api/assessments", status_code=201)
def create_assessment(payload: AssessmentIn, store: Store = Depends(get_store)):
    rule, err = _resolve_rule(store, payload.region, payload.rule_version)
    if err:
        return _err([err])
    materials = [m.model_dump() for m in payload.materials]
    errors = engine.validate_submission(rule, payload.weight_kg, materials)
    if errors:
        return _err(errors)
    result = engine.evaluate(rule, payload.weight_kg, materials)
    rec = store.create_assessment(payload.model_dump(), rule, result)
    store.add_audit(payload.operator, "assessment.created", "assessment", rec["id"],
                    {"batch_no": payload.batch_no, "region": payload.region,
                     "rule_version": rule["version"],
                     "level": result["level"]["code"],
                     "conclusion": result["conclusion"],
                     "total_fee": result["total_fee"]})
    return rec


@app.get("/api/assessments")
def list_assessments(region: str | None = None, level: str | None = None,
                     batch_no: str | None = None, limit: int = 50, offset: int = 0,
                     store: Store = Depends(get_store)):
    limit = max(1, min(limit, 500))
    return {"items": store.list_assessments(region, level, batch_no, limit, offset)}


@app.get("/api/assessments/{aid}")
def get_assessment(aid: str, store: Store = Depends(get_store)):
    rec = store.get_assessment(aid)
    if not rec:
        return _err([{"field": "id", "message": f"判定单不存在：{aid}"}], 404)
    return rec


# ---------------------------------------------------------------- 审计与导出
@app.get("/api/audit")
def list_audit(entity: str | None = None, limit: int = 100, offset: int = 0,
               store: Store = Depends(get_store)):
    limit = max(1, min(limit, 1000))
    return {"items": store.list_audit(entity, limit, offset)}


def _csv_response(rows: list[dict], columns: list[str], filename: str):
    buf = io.StringIO()
    buf.write("﻿")  # BOM，便于 Excel 识别中文
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/export/assessments.csv")
def export_assessments(region: str | None = None, level: str | None = None,
                       batch_no: str | None = None,
                       store: Store = Depends(get_store)):
    rows = store.list_assessments(region, level, batch_no, limit=100000)
    return _csv_response(rows, [
        "id", "batch_no", "region", "weight_kg", "total_contamination_kg",
        "contamination_ratio", "level_code", "level_name", "conclusion",
        "total_fee", "currency", "rule_version", "operator", "created_at",
    ], "assessments.csv")


@app.get("/api/export/audit.csv")
def export_audit(store: Store = Depends(get_store)):
    rows = store.list_audit(limit=100000)
    return _csv_response(rows, ["id", "ts", "actor", "action", "entity",
                                "entity_id", "detail"], "audit.csv")


# ------------------------------------------------------------------ 页面
INDEX = Path(__file__).parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(INDEX)
