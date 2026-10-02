"""SQLite 持久层：账务数据 + 版本/事件留痕。

所有金额以「分」(INTEGER) 存储，避免浮点误差。
事件表 (events) 只追加，用于保留调价、换人、争议等业务依据。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 用户与角色：customer=客户，manager=项目经理
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('customer', 'manager'))
);

-- 合同（当前版本指针、当前项目经理、质保金比例对应的固定金额）
CREATE TABLE IF NOT EXISTS contracts (
    id                TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    customer_id       TEXT NOT NULL REFERENCES users(id),
    manager_id        TEXT NOT NULL REFERENCES users(id),
    current_version   INTEGER NOT NULL DEFAULT 1,
    retention_cents   INTEGER NOT NULL CHECK (retention_cents >= 0),
    warranty_end      TEXT NOT NULL,          -- ISO 日期，质保期满日
    created_at        TEXT NOT NULL
);

-- 合同版本：每次合同价调整新增一行，旧版本保留为依据
CREATE TABLE IF NOT EXISTS contract_versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id   TEXT NOT NULL REFERENCES contracts(id),
    version_no    INTEGER NOT NULL,
    amount_cents  INTEGER NOT NULL CHECK (amount_cents >= 0),
    change_reason TEXT,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (contract_id, version_no)
);

-- 项目经理任期：更换经理时结束旧任期、开启新任期
CREATE TABLE IF NOT EXISTS pm_tenures (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id  TEXT NOT NULL REFERENCES contracts(id),
    manager_id   TEXT NOT NULL REFERENCES users(id),
    seq          INTEGER NOT NULL,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    change_reason TEXT,
    UNIQUE (contract_id, seq)
);

-- 工程完成量：仅项目经理可录入（服务层强校验角色）
CREATE TABLE IF NOT EXISTS progress_entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id  TEXT NOT NULL REFERENCES contracts(id),
    item_name    TEXT NOT NULL,
    quantity     REAL NOT NULL CHECK (quantity >= 0),
    unit         TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    recorded_by  TEXT NOT NULL REFERENCES users(id),
    recorded_at  TEXT NOT NULL
);

-- 整改/返修项。kind: rectification=竣工验收整改, warranty=质保期返修
-- status: open 待整改 / submitted 待客户确认 / passed 已验收通过 / rejected 客户提出新问题
CREATE TABLE IF NOT EXISTS rectifications (
    id             TEXT PRIMARY KEY,
    contract_id    TEXT NOT NULL REFERENCES contracts(id),
    kind           TEXT NOT NULL DEFAULT 'rectification'
                   CHECK (kind IN ('rectification', 'warranty')),
    code           TEXT NOT NULL,
    title          TEXT NOT NULL,
    location       TEXT NOT NULL,
    description    TEXT NOT NULL,
    holdback_cents INTEGER NOT NULL CHECK (holdback_cents >= 0),  -- 对应的尾款留存
    status         TEXT NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open', 'submitted', 'passed', 'rejected')),
    created_at     TEXT NOT NULL,
    passed_at      TEXT,
    UNIQUE (contract_id, code)
);

-- 每次整改提交（打回后可重新提交，形成第 2、3 次提交）
CREATE TABLE IF NOT EXISTS rect_submissions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rect_id       TEXT NOT NULL REFERENCES rectifications(id),
    seq           INTEGER NOT NULL,
    note          TEXT NOT NULL,
    submitted_by  TEXT NOT NULL REFERENCES users(id),
    submitted_at  TEXT NOT NULL,
    decision      TEXT NOT NULL DEFAULT 'pending'
                  CHECK (decision IN ('pending', 'passed', 'rejected')),
    decided_by    TEXT REFERENCES users(id),
    decided_at    TEXT,
    decision_note TEXT,
    signature     TEXT,                          -- 模拟签名适配器签发的验收签名令牌
    UNIQUE (rect_id, seq)
);

-- 现场照片，本地文件存储（只保存相对路径），挂在某次提交下
CREATE TABLE IF NOT EXISTS rect_photos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rect_id       TEXT NOT NULL REFERENCES rectifications(id),
    submission_id INTEGER REFERENCES rect_submissions(id),
    file_path     TEXT NOT NULL,
    caption       TEXT NOT NULL,
    uploaded_at   TEXT NOT NULL
);

-- 付款指令：幂等键唯一，保证并发放款只产生一笔指令
-- kind: progress 进度款 / final 整改尾款 / retention 质保金
CREATE TABLE IF NOT EXISTS payment_instructions (
    id              TEXT PRIMARY KEY,
    contract_id     TEXT NOT NULL REFERENCES contracts(id),
    kind            TEXT NOT NULL CHECK (kind IN ('progress', 'final', 'retention')),
    amount_cents    INTEGER NOT NULL CHECK (amount_cents > 0),
    idempotency_key TEXT NOT NULL UNIQUE,
    basis           TEXT NOT NULL,               -- 放款依据（JSON）
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'executed', 'receipt_confirmed')),
    gateway_ref     TEXT,                        -- 模拟支付网关流水号
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    executed_at     TEXT,
    receipt_at      TEXT
);

-- 双人签放：客户与项目经理各自确认，二者齐备才出账
CREATE TABLE IF NOT EXISTS payment_approvals (
    payment_id TEXT NOT NULL REFERENCES payment_instructions(id),
    user_id    TEXT NOT NULL REFERENCES users(id),
    approved_at TEXT NOT NULL,
    PRIMARY KEY (payment_id, user_id)
);

-- 指令与整改项的对应：尾款按已通过项逐笔释放
CREATE TABLE IF NOT EXISTS payment_items (
    payment_id TEXT NOT NULL REFERENCES payment_instructions(id),
    rect_id    TEXT NOT NULL REFERENCES rectifications(id),
    amount_cents INTEGER NOT NULL,
    PRIMARY KEY (payment_id, rect_id)
);

-- 质保索赔（质保期内返修登记），存在未结索赔时质保金不得结清
CREATE TABLE IF NOT EXISTS warranty_claims (
    id          TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(id),
    rect_id     TEXT REFERENCES rectifications(id),
    title       TEXT NOT NULL,
    detail      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'resolved')),
    opened_by   TEXT NOT NULL,
    opened_at   TEXT NOT NULL,
    resolved_at TEXT
);

-- 争议记录：回执延迟、新问题、调价分歧等
CREATE TABLE IF NOT EXISTS disputes (
    id          TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(id),
    title       TEXT NOT NULL,
    detail      TEXT NOT NULL,
    ref_type    TEXT,
    ref_id      TEXT,
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'resolved')),
    raised_by   TEXT NOT NULL,
    raised_at   TEXT NOT NULL,
    resolved_at TEXT,
    resolution  TEXT
);

-- 只追加的事件流：所有关键动作的依据
CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    contract_id TEXT,
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_contract ON events(contract_id, seq);
CREATE INDEX IF NOT EXISTS idx_rect_contract ON rectifications(contract_id);
CREATE INDEX IF NOT EXISTS idx_pay_contract ON payment_instructions(contract_id);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SCHEMA)
    return con


def record_event(
    con: sqlite3.Connection,
    event_type: str,
    actor: str,
    payload: dict,
    contract_id: str | None = None,
    ts: datetime | None = None,
) -> None:
    con.execute(
        "INSERT INTO events(ts, actor, contract_id, type, payload) VALUES (?,?,?,?,?)",
        (
            (ts or datetime.now()).isoformat(timespec="seconds"),
            actor,
            contract_id,
            event_type,
            json.dumps(payload, ensure_ascii=False),
        ),
    )
