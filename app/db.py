"""SQLite 存储：账务数据与版本记录（实体版本 + 事件流水）。"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 人员：客户 / 项目经理（可更换，替换记录见 manager_history）
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('customer','manager'))
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    current_manager_id INTEGER REFERENCES users(id),
    -- 合同价以分为单位；调整通过 contract_adjustments 走版本
    contract_price_cents INTEGER NOT NULL,
    -- 质保金比例（基点，如 500 = 5%）
    retention_bp INTEGER NOT NULL,
    acceptance_date TEXT,           -- 竣工验收日期
    warranty_days INTEGER NOT NULL, -- 质保期天数
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manager_history (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    old_manager_id INTEGER REFERENCES users(id),
    new_manager_id INTEGER NOT NULL REFERENCES users(id),
    reason TEXT NOT NULL,
    basis TEXT,                     -- 依据：换岗通知/协议附件说明
    changed_at TEXT NOT NULL
);

-- 合同价调整（增项/减项），必须留依据
CREATE TABLE IF NOT EXISTS contract_adjustments (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    delta_cents INTEGER NOT NULL,
    reason TEXT NOT NULL,
    basis TEXT NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

-- 项目经理录入的工程完成量（客户不得代录）
CREATE TABLE IF NOT EXISTS work_completions (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    item_name TEXT NOT NULL,
    quantity REAL NOT NULL,
    unit TEXT NOT NULL,
    entered_by INTEGER NOT NULL REFERENCES users(id),
    entered_at TEXT NOT NULL
);

-- 整改项
CREATE TABLE IF NOT EXISTS rectifications (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open','submitted','passed','rework','cancelled')),
    submitted_at TEXT,              -- 经理提交复验
    signed_by INTEGER REFERENCES users(id),  -- 必须是客户
    signed_at TEXT,
    signature TEXT,                 -- 模拟签名适配器返回的签名令牌
    tail_cents INTEGER NOT NULL DEFAULT 0,   -- 该整改对应的可释放尾款
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    -- 部分整改通过后客户提出“新问题”：作为父子关系保留来源
    parent_id INTEGER REFERENCES rectifications(id)
);

CREATE TABLE IF NOT EXISTS rect_photos (
    id INTEGER PRIMARY KEY,
    rect_id INTEGER NOT NULL REFERENCES rectifications(id),
    file_path TEXT NOT NULL,        -- 本地文件（相对 PHOTOS_DIR）
    caption TEXT,
    -- repair=整改后复验照；issue=客户新提出问题的照片
    kind TEXT NOT NULL DEFAULT 'repair' CHECK(kind IN ('repair','issue','before')),
    uploaded_by INTEGER NOT NULL REFERENCES users(id),
    uploaded_at TEXT NOT NULL
);

-- 质保期内返修
CREATE TABLE IF NOT EXISTS warranty_repairs (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open','repaired','confirmed','claim')),
    cost_cents INTEGER NOT NULL DEFAULT 0, -- 发生 claim 时从质保金扣减
    basis TEXT,
    opened_at TEXT NOT NULL,
    repaired_at TEXT,
    confirmed_by INTEGER REFERENCES users(id),
    confirmed_at TEXT
);

-- 付款申请单（尾款申请：客户对已通过整改发起）
CREATE TABLE IF NOT EXISTS payment_requests (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    kind TEXT NOT NULL CHECK(kind IN ('tail','retention','progress')),
    amount_cents INTEGER NOT NULL,
    note TEXT,
    requested_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_request_items (
    request_id INTEGER NOT NULL REFERENCES payment_requests(id),
    rect_id INTEGER NOT NULL REFERENCES rectifications(id),
    amount_cents INTEGER NOT NULL,
    PRIMARY KEY(request_id, rect_id)
);

-- 一笔付款指令（模拟支付适配器）；receipt_no 全局唯一 => 回执重放幂等
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    request_id INTEGER NOT NULL UNIQUE REFERENCES payment_requests(id),
    kind TEXT NOT NULL CHECK(kind IN ('tail','retention','progress')),
    amount_cents INTEGER NOT NULL,
    receipt_no TEXT UNIQUE,         -- 适配器回执号
    status TEXT NOT NULL DEFAULT 'initiated'
        CHECK(status IN ('initiated','paid','failed')),
    paid_at TEXT,
    created_at TEXT NOT NULL
);

-- 争议记录（保留依据）
CREATE TABLE IF NOT EXISTS disputes (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    subject TEXT NOT NULL,
    detail TEXT,
    basis TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
    raised_by INTEGER NOT NULL REFERENCES users(id),
    raised_at TEXT NOT NULL,
    resolved_at TEXT
);

-- 版本/审计流水：所有关键变更追加写入，只增不改
CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    actor_id INTEGER,
    entity TEXT NOT NULL,
    entity_id INTEGER,
    action TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT UNIQUE     -- 幂等键：同一操作（含并发放款）只产生一笔指令
);

CREATE INDEX IF NOT EXISTS idx_event_entity ON event_log(entity, entity_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._tl = threading.local()
        self._lock = threading.Lock()
        self._keepalive = None
        if self.path == ":memory:":
            # 共享缓存内存库：多连接共享同一库，常驻连接保活
            self._uri = "file:settlement-mem?mode=memory&cache=shared"
            self._keepalive = self.connect()
        else:
            self._uri = self.path
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        target = getattr(self, "_uri", self.path)
        conn = sqlite3.connect(target, timeout=30, isolation_level=None,
                               check_same_thread=False, uri=self.path == ":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_schema(self):
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT)")
            row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_meta VALUES('version', ?)",
                             (str(SCHEMA_VERSION),))
        finally:
            conn.close()

    def tx(self):
        """事务上下文（调用线程内串行化，配合 BEGIN IMMEDIATE 防并发双花）。"""
        return _Tx(self)

    def log_event(self, conn, actor_id, entity, entity_id, action,
                  payload=None, idempotency_key=None):
        try:
            cur = conn.execute(
                "INSERT INTO event_log(ts,actor_id,entity,entity_id,action,payload,idempotency_key)"
                " VALUES(?,?,?,?,?,?,?)",
                (now_iso(), actor_id, entity, entity_id, action,
                 json.dumps(payload or {}, ensure_ascii=False), idempotency_key))
            return cur.lastrowid
        except sqlite3.IntegrityError as e:
            if idempotency_key is not None:
                from .errors import Conflict
                raise Conflict("重复操作（幂等键已存在）") from e
            raise


class _Tx:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self) -> sqlite3.Connection:
        self.conn = self.db.connect()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        self.conn.close()
        return False
