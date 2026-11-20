"""跨夜召回保障的持久层。

设计原则：
- recall_events / recall_punches / recall_retry_log 只追加，原始打卡永不删除；
  补卡通过新 punch_id 版本（seq 递增）实现替换，采信时取同 punch_id 最大 seq。
- recall_vouchers 一事件多版本，status=CURRENT/SUPERSEDED/FAILED；
  替换失败时旧 CURRENT 不受影响（事务回滚），不存在“无有效凭证”窗口。
- 费率表按版本快照，凭证落库时冻结当版倍率，费率改动只影响之后的新凭证。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.errors import DomainError


def connection() -> sqlite3.Connection:
    path = Path(os.environ.get('DB_PATH', 'data/records.db'))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(
        '''
        CREATE TABLE IF NOT EXISTS recall_events(
            event_id TEXT PRIMARY KEY,
            employee TEXT NOT NULL,
            hourly_rate TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS recall_punches(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL REFERENCES recall_events(event_id),
            punch_id TEXT NOT NULL,
            direction TEXT NOT NULL CHECK(direction IN ('IN','OUT')),
            at TEXT NOT NULL,
            source TEXT NOT NULL CHECK(source IN ('ORIGINAL','MAKEUP')),
            seq INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(event_id,punch_id,seq)
        );
        CREATE TABLE IF NOT EXISTS recall_breaks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL REFERENCES recall_events(event_id),
            break_start TEXT NOT NULL,
            break_end TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS recall_rate_versions(
            version INTEGER PRIMARY KEY AUTOINCREMENT,
            calendar TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS recall_vouchers(
            voucher_no TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('CURRENT','SUPERSEDED','FAILED')),
            rate_version INTEGER NOT NULL,
            body TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(event_id,version)
        );
        CREATE TABLE IF NOT EXISTS recall_retry_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN ('PROCESSED','DEDUPED','REJECTED')),
            voucher_no TEXT,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        '''
    )
    return db


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def voucher_number(event_id: str, version: int) -> str:
    """统一凭证号：RC-<事件ID>-V<两位版本>，事件自带 RC- 前缀时不重复拼接。"""
    stem = event_id[3:] if event_id.startswith('RC-') else event_id
    return f'RC-{stem}-V{version:02d}'


# ---------- 事件与打卡（只追加） ----------

def upsert_event(event_id: str, employee: str, hourly_rate: str) -> None:
    with connection() as db:
        db.execute(
            '''INSERT INTO recall_events(event_id,employee,hourly_rate,created_at,updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(event_id) DO UPDATE SET
                 employee=excluded.employee,hourly_rate=excluded.hourly_rate,updated_at=excluded.updated_at''',
            (event_id, employee, str(hourly_rate), now_iso(), now_iso()),
        )


def add_punch(event_id: str, punch_id: str, direction: str, at: str, source: str) -> int:
    """追加一条打卡；同 (event,punch_id) 的 seq 自动递增，旧行保留。"""
    with connection() as db:
        row = db.execute(
            'SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM recall_punches WHERE event_id=? AND punch_id=?',
            (event_id, punch_id),
        ).fetchone()
        seq = row['next_seq']
        db.execute(
            '''INSERT INTO recall_punches(event_id,punch_id,direction,at,source,seq,created_at)
               VALUES(?,?,?,?,?,?,?)''',
            (event_id, punch_id, direction, at, source, seq, now_iso()),
        )
        return seq


def add_break(event_id: str, break_start: str, break_end: str) -> None:
    with connection() as db:
        db.execute(
            'INSERT INTO recall_breaks(event_id,break_start,break_end) VALUES(?,?,?)',
            (event_id, break_start, break_end),
        )


def list_punches(event_id: str) -> list[dict]:
    with connection() as db:
        rows = db.execute(
            'SELECT punch_id,direction,at,source,seq FROM recall_punches WHERE event_id=? ORDER BY seq,id',
            (event_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_breaks(event_id: str) -> list[dict]:
    with connection() as db:
        rows = db.execute(
            'SELECT break_start,break_end FROM recall_breaks WHERE event_id=? ORDER BY id',
            (event_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_event(event_id: str) -> dict:
    with connection() as db:
        row = db.execute('SELECT * FROM recall_events WHERE event_id=?', (event_id,)).fetchone()
    if not row:
        raise DomainError('NOT_FOUND', f'召回事件 {event_id} 不存在', 404)
    return dict(row)


def list_events() -> list[dict]:
    with connection() as db:
        rows = db.execute('SELECT event_id,employee,hourly_rate,created_at FROM recall_events ORDER BY event_id').fetchall()
    return [dict(r) for r in rows]


# ---------- 重试幂等 ----------

def recent_request_hashes(event_id: str, limit: int = 100) -> list[str]:
    with connection() as db:
        rows = db.execute(
            '''SELECT request_hash FROM recall_retry_log WHERE event_id=?
               ORDER BY id DESC LIMIT ?''', (event_id, limit),
        ).fetchall()
    return [r['request_hash'] for r in rows]


def log_retry(event_id: str, request_hash: str, outcome: str, detail: str, voucher_no: str | None = None) -> None:
    with connection() as db:
        db.execute(
            '''INSERT INTO recall_retry_log(event_id,request_hash,outcome,voucher_no,detail,created_at)
               VALUES(?,?,?,?,?,?)''',
            (event_id, request_hash, outcome, voucher_no, detail, now_iso()),
        )


def list_retry_log(event_id: str) -> list[dict]:
    with connection() as db:
        rows = db.execute(
            'SELECT request_hash,outcome,voucher_no,detail,created_at FROM recall_retry_log WHERE event_id=? ORDER BY id',
            (event_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- 费率版本 ----------

def save_rate_calendar(calendar: dict) -> int:
    with connection() as db:
        cur = db.execute(
            'INSERT INTO recall_rate_versions(calendar,created_at) VALUES(?,?)',
            (json.dumps(calendar, ensure_ascii=False, sort_keys=True), now_iso()),
        )
        return cur.lastrowid


def get_rate_calendar(version: int) -> dict:
    with connection() as db:
        row = db.execute('SELECT calendar FROM recall_rate_versions WHERE version=?', (version,)).fetchone()
    if not row:
        raise DomainError('RATE', f'费率版本 {version} 不存在')
    return json.loads(row['calendar'])


def latest_rate_version() -> int | None:
    with connection() as db:
        row = db.execute('SELECT MAX(version) AS v FROM recall_rate_versions').fetchone()
    return row['v']


# ---------- 凭证版本化（原子替换、失败回滚） ----------

def _next_voucher_version(db: sqlite3.Connection, event_id: str) -> int:
    row = db.execute('SELECT COALESCE(MAX(version),0)+1 AS v FROM recall_vouchers WHERE event_id=?',
                     (event_id,)).fetchone()
    return row['v']


def issue_voucher(event_id: str, body: dict, rate_version: int, reason: str) -> dict:
    """原子地先作废旧 CURRENT 再插入新凭证；任一步失败整体回滚。

    body 已先由规则层完整算好，落库失败（如约束冲突）时旧版 CURRENT 凭证保持有效。
    先作废后插入，保证任何时刻至多一个 CURRENT，恢复时也只会回到最近一版。
    """
    ts = now_iso()
    with connection() as db:
        try:
            version = _next_voucher_version(db, event_id)
            voucher_no = voucher_number(event_id, version)
            db.execute(
                '''UPDATE recall_vouchers SET status='SUPERSEDED'
                   WHERE event_id=? AND status='CURRENT' ''',
                (event_id,),
            )
            db.execute(
                '''INSERT INTO recall_vouchers(voucher_no,event_id,version,status,rate_version,body,reason,created_at)
                   VALUES(?,?,?,'CURRENT',?,?,?,?)''',
                (voucher_no, event_id, version, rate_version,
                 json.dumps(body, ensure_ascii=False), reason, ts),
            )
        except sqlite3.Error as exc:
            db.rollback()
            raise DomainError('VOUCHER', f'凭证生成失败，已保留旧版凭证：{exc}')
    return get_voucher(voucher_no)


def fail_and_restore(event_id: str, body: dict, rate_version: int, detail: str) -> str:
    """替换失败的原子事务：写入 FAILED 新版本，并把最近一版 SUPERSEDED 恢复为 CURRENT。

    与正常替换相反（正常是旧版先作废再插新版）；任一步失败整体回滚，
    保证失败后恰好一个 CURRENT（即替换前的现行凭证），且失败版本留痕。
    返回失败凭证号。
    """
    ts = now_iso()
    with connection() as db:
        try:
            nxt = db.execute(
                'SELECT COALESCE(MAX(version),0)+1 AS v FROM recall_vouchers WHERE event_id=?',
                (event_id,)).fetchone()['v']
            failed_no = voucher_number(event_id, nxt)
            db.execute(
                '''INSERT INTO recall_vouchers(voucher_no,event_id,version,status,rate_version,body,reason,created_at)
                   VALUES(?,?,?,'FAILED',?,?,?,?)''',
                (failed_no, event_id, nxt, rate_version,
                 json.dumps(body, ensure_ascii=False), f'替换失败：{detail}', ts),
            )
            row = db.execute(
                '''SELECT voucher_no FROM recall_vouchers
                   WHERE event_id=? AND status='SUPERSEDED'
                   ORDER BY version DESC LIMIT 1''',
                (event_id,),
            ).fetchone()
            if row:
                # 先把当前现行凭证降为 SUPERSEDED，再恢复最近一版 SUPERSEDED 为 CURRENT，
                # 保证失败后仍至多一个 CURRENT（即被替换前的上一版）。
                db.execute(
                    "UPDATE recall_vouchers SET status='SUPERSEDED' WHERE event_id=? AND status='CURRENT'",
                    (event_id,),
                )
                db.execute(
                    "UPDATE recall_vouchers SET status='CURRENT' WHERE voucher_no=?",
                    (row['voucher_no'],),
                )
        except sqlite3.Error as exc:
            db.rollback()
            raise DomainError('VOUCHER', f'失败回滚事务异常：{exc}')
    return failed_no


def get_voucher(voucher_no: str) -> dict:
    with connection() as db:
        row = db.execute('SELECT * FROM recall_vouchers WHERE voucher_no=?', (voucher_no,)).fetchone()
    if not row:
        raise DomainError('NOT_FOUND', f'凭证 {voucher_no} 不存在', 404)
    out = dict(row)
    out['body'] = json.loads(out['body'])
    return out


def current_voucher(event_id: str) -> dict | None:
    with connection() as db:
        row = db.execute(
            "SELECT * FROM recall_vouchers WHERE event_id=? AND status='CURRENT'",
            (event_id,),
        ).fetchone()
    if not row:
        return None
    out = dict(row)
    out['body'] = json.loads(out['body'])
    return out


def voucher_history(event_id: str) -> list[dict]:
    with connection() as db:
        rows = db.execute(
            'SELECT voucher_no,version,status,rate_version,reason,created_at FROM recall_vouchers WHERE event_id=? ORDER BY version',
            (event_id,),
        ).fetchall()
    return [dict(r) for r in rows]
