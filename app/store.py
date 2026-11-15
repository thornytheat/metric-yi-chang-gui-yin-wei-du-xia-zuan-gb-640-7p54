"""SQLite 持久化：运行清单、指标点、隔离行。

每次操作单独开连接，避免 TestClient 多线程共享连接；写入按 run_id 覆盖，
同源重跑命中同一 run_id，先删后写保证行数不变。
"""
from __future__ import annotations

import json
import os
import sqlite3
from decimal import Decimal

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    run_key       TEXT UNIQUE NOT NULL,
    input_name    TEXT NOT NULL,
    input_sha256  TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    duration_ms   INTEGER NOT NULL,
    empty_state   TEXT NOT NULL DEFAULT '',
    manifest      TEXT NOT NULL,
    summary       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS points (
    run_id          TEXT NOT NULL,
    hotel_code      TEXT NOT NULL,
    hotel_name      TEXT NOT NULL,
    channel         TEXT NOT NULL,
    biz_date        TEXT NOT NULL,
    stratum         TEXT NOT NULL,
    rooms_available INTEGER NOT NULL,
    rooms_sold      INTEGER NOT NULL,
    adr             TEXT NOT NULL,
    revenue_net     TEXT NOT NULL,
    late_days       INTEGER NOT NULL,
    late_flag       INTEGER NOT NULL,
    out_of_window   INTEGER NOT NULL,
    source_line     INTEGER NOT NULL,
    updated_utc     TEXT NOT NULL,
    PRIMARY KEY (run_id, hotel_code, channel, biz_date)
);
CREATE TABLE IF NOT EXISTS quarantine (
    run_id     TEXT NOT NULL,
    line_no    INTEGER NOT NULL,
    code       TEXT NOT NULL,
    payload    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_points_run ON points(run_id);
CREATE INDEX IF NOT EXISTS idx_quarantine_run ON quarantine(run_id);

-- 版本链：普通批次 parent_run_id 为空；修订版本指向其直接父版本
CREATE TABLE IF NOT EXISTS revisions (
    revision_id    TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    parent_run_id  TEXT NOT NULL,
    root_run_id    TEXT NOT NULL,
    revision_no    INTEGER NOT NULL,
    input_name     TEXT NOT NULL,
    input_sha256   TEXT NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    mode           TEXT NOT NULL,
    status         TEXT NOT NULL,
    switched       INTEGER NOT NULL,
    affected       TEXT NOT NULL,
    verification   TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    fail_reason    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_revisions_root ON revisions(root_run_id);
CREATE INDEX IF NOT EXISTS idx_revisions_run ON revisions(run_id);

-- 当前生效版本指针：单行，切换在同一事务里完成；失败不改动
CREATE TABLE IF NOT EXISTS current_version (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    run_id     TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 已复核结论：钉在具体版本与事件上，新版本不覆盖旧结论
CREATE TABLE IF NOT EXISTS review_marks (
    run_id     TEXT NOT NULL,
    event_id   TEXT NOT NULL,
    series     TEXT NOT NULL,
    verdict    TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    reviewer   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, event_id)
);
"""

POINT_FIELDS = (
    "hotel_code", "hotel_name", "channel", "biz_date", "stratum",
    "rooms_available", "rooms_sold", "adr", "revenue_net",
    "late_days", "late_flag", "out_of_window", "source_line", "updated_utc",
)


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- runs -------------------------------------------------------
    def save_run(self, manifest: dict, run_key: str, summary: dict) -> None:
        with self._conn() as conn:
            self._save_run(conn, manifest, run_key, summary)

    @staticmethod
    def _save_run(conn: sqlite3.Connection, manifest: dict, run_key: str,
                  summary: dict) -> None:
        conn.execute(
            "INSERT INTO runs (run_id, run_key, input_name, input_sha256, rules_version,"
            " created_at, duration_ms, empty_state, manifest, summary)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(run_id) DO UPDATE SET duration_ms=excluded.duration_ms,"
            " manifest=excluded.manifest, summary=excluded.summary",
            (
                manifest["run_id"], run_key, manifest["input_name"],
                manifest["input_sha256"], manifest["rules_version"],
                manifest["created_at"], int(manifest["duration_ms"]),
                manifest.get("empty_state", ""),
                json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str),
            ),
        )

    def run_id_for_key(self, run_key: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT run_id FROM runs WHERE run_key=?", (run_key,)).fetchone()
        return row["run_id"] if row else None

    def get_run(self, run_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return None
        return {
            "run_id": row["run_id"],
            "run_key": row["run_key"],
            "input_name": row["input_name"],
            "input_sha256": row["input_sha256"],
            "rules_version": row["rules_version"],
            "created_at": row["created_at"],
            "duration_ms": row["duration_ms"],
            "empty_state": row["empty_state"],
            "manifest": json.loads(row["manifest"]),
            "summary": json.loads(row["summary"]),
        }

    def latest_run(self) -> dict | None:
        # 当前生效版本以指针为准；指针不存在时回退到最新批次（首次导入/种子）
        current_id = self.current_run_id()
        if current_id:
            run = self.get_run(current_id)
            if run is not None:
                return run
        with self._conn() as conn:
            row = conn.execute(
                "SELECT run_id FROM runs WHERE empty_state='' ORDER BY created_at DESC, rowid DESC"
                " LIMIT 1").fetchone()
        return self.get_run(row["run_id"]) if row else None

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT run_id FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,)).fetchall()
        return [self.get_run(row["run_id"]) for row in rows]

    # ---- points -----------------------------------------------------
    def replace_points(self, run_id: str, points: list[dict]) -> None:
        with self._conn() as conn:
            self._replace_points(conn, run_id, points)

    @staticmethod
    def _replace_points(conn: sqlite3.Connection, run_id: str, points: list[dict]) -> None:
        conn.execute("DELETE FROM points WHERE run_id=?", (run_id,))
        conn.executemany(
            "INSERT INTO points (run_id, " + ", ".join(POINT_FIELDS) + ")"
            " VALUES (" + ",".join(["?"] * (len(POINT_FIELDS) + 1)) + ")",
            [tuple([run_id] + [p[f] for f in POINT_FIELDS]) for p in points],
        )

    def load_points(self, run_id: str) -> list[dict]:
        with self._conn() as conn:
            return self._load_points(conn, run_id)

    def _load_points(self, conn: sqlite3.Connection, run_id: str) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM points WHERE run_id=? ORDER BY hotel_code, channel, biz_date",
            (run_id,)).fetchall()
        out = []
        for row in rows:
            item = {field: row[field] for field in POINT_FIELDS}
            item["adr"] = Decimal(item["adr"])
            item["revenue_net"] = Decimal(item["revenue_net"])
            item["late_flag"] = bool(item["late_flag"])
            item["out_of_window"] = bool(item["out_of_window"])
            out.append(item)
        return out

    # ---- quarantine -------------------------------------------------
    def replace_quarantine(self, run_id: str, entries: list[dict]) -> None:
        with self._conn() as conn:
            self._replace_quarantine(conn, run_id, entries)

    @staticmethod
    def _replace_quarantine(conn: sqlite3.Connection, run_id: str,
                            entries: list[dict]) -> None:
        conn.execute("DELETE FROM quarantine WHERE run_id=?", (run_id,))
        conn.executemany(
            "INSERT INTO quarantine (run_id, line_no, code, payload) VALUES (?,?,?,?)",
            [(run_id, int(e.get("line_no", 0)), e["code"],
              json.dumps(e, ensure_ascii=False, sort_keys=True, default=str))
             for e in entries],
        )

    def load_quarantine(self, run_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT payload FROM quarantine WHERE run_id=? ORDER BY line_no, code",
                (run_id,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def point_count(self, run_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM points WHERE run_id=?",
                               (run_id,)).fetchone()
        return int(row["n"])

    def quarantine_count(self, run_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM quarantine WHERE run_id=?",
                               (run_id,)).fetchone()
        return int(row["n"])

    # ---- 当前版本指针 ------------------------------------------------
    def current_run_id(self) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT run_id FROM current_version WHERE id=1").fetchone()
        return row["run_id"] if row else None

    def set_current_version(self, run_id: str, stamp: str) -> None:
        with self._conn() as conn:
            self._set_current_version(conn, run_id, stamp)

    @staticmethod
    def _set_current_version(conn: sqlite3.Connection, run_id: str, stamp: str) -> None:
        conn.execute(
            "INSERT INTO current_version (id, run_id, updated_at) VALUES (1, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET run_id=excluded.run_id,"
            " updated_at=excluded.updated_at",
            (run_id, stamp),
        )

    # ---- 修订版本链 --------------------------------------------------
    def save_revision(self, record: dict) -> None:
        with self._conn() as conn:
            self._save_revision(conn, record)

    @staticmethod
    def _save_revision(conn: sqlite3.Connection, record: dict) -> None:
        conn.execute(
            "INSERT INTO revisions (revision_id, run_id, parent_run_id, root_run_id,"
            " revision_no, input_name, input_sha256, idempotency_key, mode, status,"
            " switched, affected, verification, created_at, fail_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record["revision_id"], record["run_id"], record["parent_run_id"],
                record["root_run_id"], int(record["revision_no"]), record["input_name"],
                record["input_sha256"], record["idempotency_key"], record["mode"],
                record["status"], int(record["switched"]),
                json.dumps(record["affected"], ensure_ascii=False, sort_keys=True),
                json.dumps(record["verification"], ensure_ascii=False, sort_keys=True,
                           default=str),
                record["created_at"], record.get("fail_reason", ""),
            ),
        )

    def revision_for_key(self, idempotency_key: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM revisions WHERE idempotency_key=?",
                               (idempotency_key,)).fetchone()
        if not row:
            return None
        return self._revision_row(row)

    def list_revisions(self, root_run_id: str | None = None) -> list[dict]:
        with self._conn() as conn:
            if root_run_id:
                rows = conn.execute(
                    "SELECT * FROM revisions WHERE root_run_id=?"
                    " ORDER BY revision_no, rowid", (root_run_id,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM revisions ORDER BY root_run_id, revision_no, rowid"
                ).fetchall()
        return [self._revision_row(row) for row in rows]

    def lineage_for_run(self, run_id: str) -> list[dict]:
        """沿 parent 链回溯该 run 所属版本链（从根到当前）。"""
        revisions = {item["run_id"]: item for item in self.list_revisions()}
        current = run_id
        chain: list[dict] = []
        seen: set[str] = set()
        while current in revisions and current not in seen:
            seen.add(current)
            item = revisions[current]
            chain.append(item)
            current = item["parent_run_id"]
        chain.reverse()
        return chain

    @staticmethod
    def _revision_row(row: sqlite3.Row) -> dict:
        return {
            "revision_id": row["revision_id"],
            "run_id": row["run_id"],
            "parent_run_id": row["parent_run_id"],
            "root_run_id": row["root_run_id"],
            "revision_no": row["revision_no"],
            "input_name": row["input_name"],
            "input_sha256": row["input_sha256"],
            "idempotency_key": row["idempotency_key"],
            "mode": row["mode"],
            "status": row["status"],
            "switched": bool(row["switched"]),
            "affected": json.loads(row["affected"]),
            "verification": json.loads(row["verification"]),
            "created_at": row["created_at"],
            "fail_reason": row["fail_reason"],
        }

    # ---- 已复核结论 --------------------------------------------------
    def save_review_mark(self, run_id: str, event_id: str, series: str,
                         verdict: str, note: str, reviewer: str, stamp: str) -> dict:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO review_marks (run_id, event_id, series, verdict, note,"
                " reviewer, created_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(run_id, event_id) DO UPDATE SET verdict=excluded.verdict,"
                " note=excluded.note, reviewer=excluded.reviewer, created_at=excluded.created_at",
                (run_id, event_id, series, verdict, note, reviewer, stamp),
            )
        return {"run_id": run_id, "event_id": event_id, "series": series,
                "verdict": verdict, "note": note, "reviewer": reviewer,
                "created_at": stamp}

    def load_review_marks(self, run_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM review_marks WHERE run_id=? ORDER BY event_id",
                (run_id,)).fetchall()
        return [dict(row) for row in rows]

    # ---- 事务：一次性切换整版结果 ------------------------------------
    def transaction(self):
        return self._conn()
