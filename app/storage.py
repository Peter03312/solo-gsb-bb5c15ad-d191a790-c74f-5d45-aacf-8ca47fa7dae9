"""SQLite 持久化：判定单（含修订链）、规则版本与审计日志。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from .rules import SEED_RULES

SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    region       TEXT NOT NULL,
    version      TEXT NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'inactive',
    created_at   TEXT NOT NULL,
    activated_at TEXT,
    UNIQUE(region, version)
);
CREATE TABLE IF NOT EXISTS assessments (
    id                     TEXT PRIMARY KEY,
    batch_no               TEXT NOT NULL,
    region                 TEXT NOT NULL,
    weight_kg              REAL NOT NULL,
    total_contamination_kg REAL NOT NULL,
    contamination_ratio    REAL NOT NULL,
    level_code             TEXT NOT NULL,
    level_name             TEXT NOT NULL,
    conclusion             TEXT NOT NULL,
    total_fee              REAL NOT NULL,
    currency               TEXT NOT NULL,
    rule_version           TEXT NOT NULL,
    rule_snapshot          TEXT NOT NULL,
    request_json           TEXT NOT NULL,
    result_json            TEXT NOT NULL,
    operator               TEXT,
    created_at             TEXT NOT NULL,
    root_id                TEXT,
    prev_id                TEXT,
    revision_no            INTEGER NOT NULL DEFAULT 1,
    revision_reason        TEXT,
    superseded             INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_assessments_batch  ON assessments(batch_no);
CREATE INDEX IF NOT EXISTS idx_assessments_region ON assessments(region);
CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    actor     TEXT,
    action    TEXT NOT NULL,
    entity    TEXT NOT NULL,
    entity_id TEXT,
    detail    TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """对 SQLite 的轻量封装；每次操作独立连接，线程安全。"""

    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("APP_DB", "data/app.db")
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        with self._locked_conn() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
        self._seed_rules()

    # ------------------------------------------------------------------ 迁移
    def _migrate(self, conn: sqlite3.Connection) -> None:
        """为既有数据库补充修订链相关列（新库已由建表语句包含）。"""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(assessments)")}
        for col, ddl in (
            ("root_id", "ALTER TABLE assessments ADD COLUMN root_id TEXT"),
            ("prev_id", "ALTER TABLE assessments ADD COLUMN prev_id TEXT"),
            ("revision_no", "ALTER TABLE assessments ADD COLUMN revision_no"
                            " INTEGER NOT NULL DEFAULT 1"),
            ("revision_reason", "ALTER TABLE assessments ADD COLUMN"
                                " revision_reason TEXT"),
            ("superseded", "ALTER TABLE assessments ADD COLUMN superseded"
                           " INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in cols:
                conn.execute(ddl)
        # 历史单据各自成链：root_id 指向自身
        conn.execute("UPDATE assessments SET root_id=id WHERE root_id IS NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_assessments_root"
                     " ON assessments(root_id)")

    # ------------------------------------------------------------------ 基础
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _locked_conn(self):
        """上下文管理器：持锁打开连接，退出时提交并关闭。"""
        store = self

        class _Ctx:
            def __enter__(self) -> sqlite3.Connection:
                store._lock.acquire()
                self.conn = store._conn()
                return self.conn

            def __exit__(self, exc_type, exc, tb):
                try:
                    if exc_type is None:
                        self.conn.commit()
                    else:
                        self.conn.rollback()
                finally:
                    self.conn.close()
                    store._lock.release()
                return False

        return _Ctx()

    # ------------------------------------------------------------------ 规则
    def _seed_rules(self) -> None:
        with self._locked_conn() as conn:
            for rule in SEED_RULES:
                payload = {k: v for k, v in rule.items() if k != "status"}
                conn.execute(
                    "INSERT OR IGNORE INTO rule_versions"
                    " (region, version, payload, status, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (rule["region"], rule["version"],
                     json.dumps(payload, ensure_ascii=False), "inactive", _now()),
                )
            # 保证每个区域恰好一个启用版本；已被用户切换过的区域不受影响。
            regions = [r[0] for r in conn.execute(
                "SELECT DISTINCT region FROM rule_versions")]
            for region in regions:
                active = conn.execute(
                    "SELECT COUNT(*) FROM rule_versions"
                    " WHERE region=? AND status='active'", (region,),
                ).fetchone()[0]
                if active:
                    continue
                preferred = next(
                    (r["version"] for r in SEED_RULES
                     if r["region"] == region and r.get("status") == "active"),
                    None,
                )
                if preferred is None:
                    preferred = conn.execute(
                        "SELECT version FROM rule_versions"
                        " WHERE region=? ORDER BY id LIMIT 1", (region,),
                    ).fetchone()[0]
                conn.execute(
                    "UPDATE rule_versions SET status='active', activated_at=?"
                    " WHERE region=? AND version=?",
                    (_now(), region, preferred),
                )

    def list_rules(self, region: str | None = None) -> list[dict]:
        sql = ("SELECT region, version, payload, status, created_at, activated_at"
               " FROM rule_versions")
        params: tuple = ()
        if region:
            sql += " WHERE region=?"
            params = (region,)
        sql += " ORDER BY region, id"
        with self._locked_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{
            "region": r["region"],
            "version": r["version"],
            "status": r["status"],
            "created_at": r["created_at"],
            "activated_at": r["activated_at"],
            "rule": json.loads(r["payload"]),
        } for r in rows]

    def get_rule(self, region: str, version: str) -> dict | None:
        with self._locked_conn() as conn:
            row = conn.execute(
                "SELECT payload FROM rule_versions WHERE region=? AND version=?",
                (region, version),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def get_active_rule(self, region: str) -> dict | None:
        with self._locked_conn() as conn:
            row = conn.execute(
                "SELECT payload FROM rule_versions"
                " WHERE region=? AND status='active'", (region,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def create_rule(self, body: dict) -> dict:
        payload = {k: body[k] for k in
                   ("region", "version", "currency", "categories", "levels")}
        with self._locked_conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO rule_versions (region, version, payload, status,"
                    " created_at) VALUES (?,?,?,?,?)",
                    (body["region"], body["version"],
                     json.dumps(payload, ensure_ascii=False), "inactive", _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"区域 {body['region']} 已存在规则版本 {body['version']}"
                ) from exc
            if body.get("activate"):
                self._activate(conn, body["region"], body["version"])
        return payload

    def _activate(self, conn: sqlite3.Connection, region: str, version: str) -> None:
        conn.execute("UPDATE rule_versions SET status='inactive' WHERE region=?",
                     (region,))
        conn.execute(
            "UPDATE rule_versions SET status='active', activated_at=?"
            " WHERE region=? AND version=?",
            (_now(), region, version),
        )

    def activate_rule(self, region: str, version: str) -> bool:
        with self._locked_conn() as conn:
            exists = conn.execute(
                "SELECT 1 FROM rule_versions WHERE region=? AND version=?",
                (region, version),
            ).fetchone()
            if not exists:
                return False
            self._activate(conn, region, version)
        return True

    # ---------------------------------------------------------------- 判定单
    def _insert_assessment(self, conn: sqlite3.Connection, aid: str,
                           batch_no: str, region: str, request: dict,
                           rule: dict, result: dict, root_id: str,
                           prev_id: str | None, revision_no: int,
                           reason: str | None) -> None:
        conn.execute(
            "INSERT INTO assessments (id, batch_no, region, weight_kg,"
            " total_contamination_kg, contamination_ratio, level_code,"
            " level_name, conclusion, total_fee, currency, rule_version,"
            " rule_snapshot, request_json, result_json, operator, created_at,"
            " root_id, prev_id, revision_no, revision_reason, superseded)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                aid, batch_no, region, request["weight_kg"],
                result["total_contamination_kg"], result["contamination_ratio"],
                result["level"]["code"], result["level"]["name"],
                result["conclusion"], result["total_fee"], result["currency"],
                rule["version"], json.dumps(rule, ensure_ascii=False),
                json.dumps(request, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                request.get("operator"), _now(),
                root_id, prev_id, revision_no, reason, 0,
            ),
        )

    def create_assessment(self, payload: dict, rule: dict, result: dict) -> dict:
        aid = "A" + uuid.uuid4().hex[:10].upper()
        with self._locked_conn() as conn:
            self._insert_assessment(conn, aid, payload["batch_no"],
                                    payload["region"], payload, rule, result,
                                    root_id=aid, prev_id=None, revision_no=1,
                                    reason=None)
        return self.get_assessment(aid)

    def create_revision(self, prev_id: str, payload: dict, rule: dict,
                        result: dict) -> tuple[str, dict | None]:
        """基于链尾单号创建修订版；链尾校验与插入在同一事务内原子完成，
        并发争用同一尾版时只有一个成功，其余得到 "conflict"。

        返回 (status, record)：status ∈ {"ok", "not_found", "conflict"}；
        仅 "ok" 时产生新单据（前版同时被标记为已被取代）。
        """
        aid = "A" + uuid.uuid4().hex[:10].upper()
        with self._locked_conn() as conn:
            row = conn.execute(
                "SELECT batch_no, region, root_id, revision_no, superseded"
                " FROM assessments WHERE id=?", (prev_id,),
            ).fetchone()
            if not row:
                return "not_found", None
            if row["superseded"]:
                return "conflict", None
            root_id = row["root_id"] or prev_id
            revision_no = (row["revision_no"] or 1) + 1
            # 批次与区域沿用原单；复检重量、物料、原因、操作者来自修订请求
            request = {
                "batch_no": row["batch_no"],
                "region": row["region"],
                "weight_kg": payload["weight_kg"],
                "materials": payload["materials"],
                "reason": payload["reason"],
                "operator": payload.get("operator"),
            }
            self._insert_assessment(conn, aid, row["batch_no"], row["region"],
                                    request, rule, result, root_id, prev_id,
                                    revision_no, payload["reason"])
            conn.execute("UPDATE assessments SET superseded=1 WHERE id=?",
                         (prev_id,))
        return "ok", self.get_assessment(aid)

    def get_assessment(self, aid: str) -> dict | None:
        with self._locked_conn() as conn:
            row = conn.execute("SELECT * FROM assessments WHERE id=?", (aid,)).fetchone()
        if not row:
            return None
        request = json.loads(row["request_json"])
        result = json.loads(row["result_json"])
        return {
            "id": row["id"],
            "batch_no": row["batch_no"],
            "region": row["region"],
            "weight_kg": row["weight_kg"],
            "materials": request["materials"],
            "operator": row["operator"],
            "created_at": row["created_at"],
            "rule_version": row["rule_version"],
            "total_contamination_kg": row["total_contamination_kg"],
            "contamination_ratio": row["contamination_ratio"],
            "level": {"code": row["level_code"], "name": row["level_name"]},
            "conclusion": row["conclusion"],
            "fee_lines": result["fee_lines"],
            "total_fee": row["total_fee"],
            "currency": row["currency"],
            "evidence": result["evidence"],
            "root_id": row["root_id"] or row["id"],
            "prev_id": row["prev_id"],
            "revision_no": row["revision_no"] or 1,
            "revision_reason": row["revision_reason"],
            "superseded": bool(row["superseded"]),
        }

    def get_rule_snapshot(self, aid: str) -> dict | None:
        """读取判定单出具时保存的规则快照；单号不存在时返回 None。"""
        with self._locked_conn() as conn:
            row = conn.execute(
                "SELECT rule_snapshot FROM assessments WHERE id=?", (aid,),
            ).fetchone()
        return json.loads(row["rule_snapshot"]) if row else None

    def get_revision_chain(self, aid: str) -> list[dict] | None:
        """返回 aid 所在修订链的全部版本（按版本号升序）；单号不存在返回 None。"""
        with self._locked_conn() as conn:
            row = conn.execute(
                "SELECT root_id FROM assessments WHERE id=?", (aid,),
            ).fetchone()
            if not row:
                return None
            root_id = row["root_id"] or aid
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM assessments WHERE root_id=?"
                " ORDER BY revision_no, created_at", (root_id,),
            )]
        return [self.get_assessment(i) for i in ids]

    def list_assessments(self, region: str | None = None, level: str | None = None,
                         batch_no: str | None = None, limit: int = 50,
                         offset: int = 0) -> list[dict]:
        sql = ("SELECT id, batch_no, region, weight_kg, total_contamination_kg,"
               " contamination_ratio, level_code, level_name, conclusion, total_fee,"
               " currency, rule_version, operator, created_at, root_id, prev_id,"
               " revision_no, superseded FROM assessments")
        conds, params = [], []
        if region:
            conds.append("region=?")
            params.append(region)
        if level:
            conds.append("level_code=?")
            params.append(level)
        if batch_no:
            conds.append("batch_no LIKE ?")
            params.append(f"%{batch_no}%")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._locked_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{**dict(r), "superseded": bool(r["superseded"])} for r in rows]

    # ---------------------------------------------------------------- 审计
    def add_audit(self, actor: str | None, action: str, entity: str,
                  entity_id: str | None, detail: dict | None = None) -> None:
        with self._locked_conn() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, actor, action, entity, entity_id, detail)"
                " VALUES (?,?,?,?,?,?)",
                (_now(), actor, action, entity, entity_id,
                 json.dumps(detail or {}, ensure_ascii=False)),
            )

    def list_audit(self, entity: str | None = None, limit: int = 100,
                   offset: int = 0) -> list[dict]:
        sql = "SELECT * FROM audit_log"
        params: list[Any] = []
        if entity:
            sql += " WHERE entity=?"
            params.append(entity)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._locked_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
