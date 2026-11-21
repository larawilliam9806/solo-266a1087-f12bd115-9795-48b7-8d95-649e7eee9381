"""Application services: table replacement, weighting jobs, result access.

Concurrency model
-----------------
A single process is assumed (docker compose runs one uvicorn worker).
``self._lock`` serialises table replacement and job bookkeeping; the heavy
LP work happens on a worker thread using its own SQLite connection.

Every source table is an immutable versioned snapshot.  Replacement only
flips a pointer row after the new snapshot is fully written, so a failed
upload cannot disturb the live table.  Results are always tagged with the
exact version pair they were computed from: as soon as either table is
replaced, the old pair becomes stale and its weights/differences stop being
served (the API reports "待配权").
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from . import database, weighting
from .errors import (
    JobRunningError,
    MissingTableError,
    MissingTargetsError,
    NoResultError,
    NotReadyError,
    ValidationError,
)

STATUS_LABELS = {
    "missing": "待配权（缺少数据表）",
    "pending": "待配权",
    "running": "配权计算中",
    "balanced": "已配平",
    "unbalanced": "无法配平",
    "failed": "计算失败",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _content_hash(records: list[dict]) -> str:
    payload = json.dumps(records, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class WeightingService:
    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or database.DEFAULT_DB_PATH
        self._lock = threading.RLock()
        with database.db_session(self._db_path) as conn:
            database.init_db(conn)
        # One background thread at a time; a new replacement supersedes it.
        self._worker: threading.Thread | None = None
        self._worker_token: uuid.UUID | None = None

    # ------------------------------------------------------------------
    # Snapshot reads
    # ------------------------------------------------------------------
    def _current_version(self, conn: sqlite3.Connection, table: str) -> int | None:
        row = conn.execute(
            "SELECT version FROM current_versions WHERE table_name = ?",
            (table,),
        ).fetchone()
        return None if row is None else row["version"]

    def _load_respondents(self, conn: sqlite3.Connection, version: int) -> list[dict]:
        rows = conn.execute(
            "SELECT household_id, district, size_band, init_weight "
            "FROM respondent_records WHERE version = ? ORDER BY household_id",
            (version,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _load_targets(self, conn: sqlite3.Connection, version: int) -> list[dict]:
        rows = conn.execute(
            "SELECT dimension, group_name, target_count "
            "FROM target_records WHERE version = ? "
            "ORDER BY dimension, group_name",
            (version,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Validation (runs before anything is written)
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_respondents(records: list[dict]) -> None:
        if not isinstance(records, list) or not records:
            raise ValidationError("应答名单不能为空")
        seen: set[str] = set()
        for idx, rec in enumerate(records):
            try:
                hid = str(rec["household_id"]).strip()
                district = str(rec["district"]).strip()
                band = str(rec["size_band"]).strip()
                weight = float(rec["init_weight"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValidationError(
                    f"第 {idx + 1} 行字段不完整或类型错误: {exc}",
                    {"index": idx},
                )
            if not hid or not district or not band:
                raise ValidationError(f"第 {idx + 1} 行存在空字段", {"index": idx})
            if weight != weight or weight in (float("inf"), float("-inf")):
                raise ValidationError(f"第 {idx + 1} 行初始权重不是有限数",
                                      {"index": idx})
            if weight <= 0:
                raise ValidationError(
                    f"第 {idx + 1} 行初始权重必须为正数", {"index": idx}
                )
            if hid in seen:
                raise ValidationError(
                    f"户号重复: {hid}", {"household_id": hid}
                )
            seen.add(hid)

    @staticmethod
    def _validate_targets(records: list[dict]) -> None:
        if not isinstance(records, list) or not records:
            raise ValidationError("总体目标表不能为空")
        seen: set[tuple[str, str]] = set()
        for idx, rec in enumerate(records):
            try:
                dimension = str(rec["dimension"]).strip()
                group = str(rec["group_name"]).strip()
                target = float(rec["target_count"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValidationError(
                    f"第 {idx + 1} 行字段不完整或类型错误: {exc}",
                    {"index": idx},
                )
            if dimension not in ("district", "size_band"):
                raise ValidationError(
                    f"第 {idx + 1} 行 dimension 必须为 district 或 size_band",
                    {"index": idx, "value": dimension},
                )
            if not group:
                raise ValidationError(f"第 {idx + 1} 行 group_name 为空",
                                      {"index": idx})
            if target != target or target in (float("inf"), float("-inf")):
                raise ValidationError(f"第 {idx + 1} 行目标户数不是有限数",
                                      {"index": idx})
            if target < 0:
                raise ValidationError(
                    f"第 {idx + 1} 行目标户数必须非负", {"index": idx}
                )
            key = (dimension, group)
            if key in seen:
                raise ValidationError(
                    f"分组目标重复: {dimension}/{group}",
                    {"dimension": dimension, "group_name": group},
                )
            seen.add(key)

    # ------------------------------------------------------------------
    # Whole-table replacement
    # ------------------------------------------------------------------
    def replace_respondents(self, records: list[dict]) -> dict:
        self._validate_respondents(records)
        normalized = [
            {
                "household_id": r["household_id"].strip(),
                "district": r["district"].strip(),
                "size_band": r["size_band"].strip(),
                "init_weight": float(r["init_weight"]),
            }
            for r in records
        ]
        digest = _content_hash(normalized)
        with self._lock, database.db_session(self._db_path) as conn:
            previous = self._current_version(conn, "respondents")
            version = self._insert_respondent_snapshot(conn, normalized, digest)
            conn.execute(
                "INSERT INTO current_versions(table_name, version) "
                "VALUES ('respondents', ?) "
                "ON CONFLICT(table_name) DO UPDATE SET version = excluded.version",
                (version,),
            )
            conn.commit()
        return {
            "table": "respondents",
            "version": version,
            "row_count": len(normalized),
            "content_hash": digest,
            "replaced_version": previous,
            "replaced_at": _utc_now(),
            "message": "应答名单换交成功，旧版权重与差额已停供，请重新发起配权",
        }

    def replace_targets(self, records: list[dict]) -> dict:
        self._validate_targets(records)
        normalized = [
            {
                "dimension": r["dimension"].strip(),
                "group_name": r["group_name"].strip(),
                "target_count": float(r["target_count"]),
            }
            for r in records
        ]
        digest = _content_hash(normalized)
        with self._lock, database.db_session(self._db_path) as conn:
            previous = self._current_version(conn, "targets")
            version = self._insert_target_snapshot(conn, normalized, digest)
            conn.execute(
                "INSERT INTO current_versions(table_name, version) "
                "VALUES ('targets', ?) "
                "ON CONFLICT(table_name) DO UPDATE SET version = excluded.version",
                (version,),
            )
            conn.commit()
        return {
            "table": "targets",
            "version": version,
            "row_count": len(normalized),
            "content_hash": digest,
            "replaced_version": previous,
            "replaced_at": _utc_now(),
            "message": "总体目标表换交成功，旧版权重与差额已停供，请重新发起配权",
        }

    def _insert_respondent_snapshot(
        self, conn: sqlite3.Connection, records: list[dict], digest: str
    ) -> int:
        cur = conn.execute(
            "INSERT INTO respondent_versions(created_at, row_count, content_hash) "
            "VALUES (?, ?, ?)",
            (_utc_now(), len(records), digest),
        )
        version = cur.lastrowid
        conn.executemany(
            "INSERT INTO respondent_records"
            "(version, household_id, district, size_band, init_weight) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    version,
                    r["household_id"],
                    r["district"],
                    r["size_band"],
                    r["init_weight"],
                )
                for r in records
            ],
        )
        return version

    def _insert_target_snapshot(
        self, conn: sqlite3.Connection, records: list[dict], digest: str
    ) -> int:
        cur = conn.execute(
            "INSERT INTO target_versions(created_at, row_count, content_hash) "
            "VALUES (?, ?, ?)",
            (_utc_now(), len(records), digest),
        )
        version = cur.lastrowid
        conn.executemany(
            "INSERT INTO target_records"
            "(version, dimension, group_name, target_count) "
            "VALUES (?, ?, ?, ?)",
            [
                (version, r["dimension"], r["group_name"], r["target_count"])
                for r in records
            ],
        )
        return version

    # ------------------------------------------------------------------
    # Status + job lifecycle
    # ------------------------------------------------------------------
    def get_status(self) -> dict:
        with self._lock, database.db_session(self._db_path) as conn:
            r_ver = self._current_version(conn, "respondents")
            t_ver = self._current_version(conn, "targets")
            missing = []
            if r_ver is None:
                missing.append("respondents")
            if t_ver is None:
                missing.append("targets")
            if missing:
                return {
                    "status": "missing",
                    "status_label": STATUS_LABELS["missing"],
                    "respondent_version": None,
                    "target_version": None,
                    "missing_tables": missing,
                    "message": "两表尚未齐备，无法配权",
                }

            job = self._latest_job(conn, r_ver, t_ver)
            if job is None:
                return {
                    "status": "pending",
                    "status_label": STATUS_LABELS["pending"],
                    "respondent_version": r_ver,
                    "target_version": t_ver,
                    "message": "当前两表版本尚无配权结果，请发起配权",
                }
            return self._status_from_job(job, r_ver, t_ver)

    def _status_from_job(
        self, job: sqlite3.Row, r_ver: int, t_ver: int
    ) -> dict:
        status = job["status"]
        body = {
            "status": status,
            "status_label": STATUS_LABELS[status],
            "respondent_version": r_ver,
            "target_version": t_ver,
            "job_id": job["id"],
        }
        if status == "running":
            body["message"] = "配权计算中，请稍后查询"
        elif status == "balanced":
            body["message"] = "已配平，可领取逐户权重"
            body["objective_gap"] = job["objective_gap"]
        elif status == "unbalanced":
            body["message"] = "无法配平，请领取各组差额排查缺样"
            body["objective_gap"] = job["objective_gap"]
        else:  # failed
            body["message"] = f"计算失败: {job['error_reason']}"
            body["error_reason"] = job["error_reason"]
        return body

    def _latest_job(
        self, conn: sqlite3.Connection, r_ver: int, t_ver: int
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM weighting_jobs "
            "WHERE respondent_version = ? AND target_version = ? "
            "ORDER BY id DESC LIMIT 1",
            (r_ver, t_ver),
        ).fetchone()

    def start_weighting(self) -> dict:
        with self._lock, database.db_session(self._db_path) as conn:
            r_ver = self._current_version(conn, "respondents")
            t_ver = self._current_version(conn, "targets")
            if r_ver is None or t_ver is None:
                missing = [
                    name
                    for name, ver in (
                        ("respondents", r_ver),
                        ("targets", t_ver),
                    )
                    if ver is None
                ]
                raise MissingTableError(
                    "缺少必需数据表，无法发起配权",
                    {"missing_tables": missing},
                )

            job = self._latest_job(conn, r_ver, t_ver)
            if job is not None and job["status"] == "running":
                raise JobRunningError(
                    "当前两表版本正在配权计算中，请勿重复发起",
                    {"job_id": job["id"]},
                )

            # Coverage check: every group occurring in the roster must have
            # a target, otherwise weighted counts for that group cannot be
            # evaluated.
            missing_groups = self._missing_groups(conn, r_ver, t_ver)
            if missing_groups:
                raise MissingTargetsError(
                    "总体目标表缺少名单中出现的分组目标",
                    {"missing_group_targets": missing_groups},
                )

            cur = conn.execute(
                "INSERT INTO weighting_jobs"
                "(respondent_version, target_version, status, created_at) "
                "VALUES (?, ?, 'running', ?)",
                (r_ver, t_ver, _utc_now()),
            )
            job_id = cur.lastrowid
            conn.commit()

        token = uuid.uuid4()
        self._worker_token = token
        thread = threading.Thread(
            target=self._run_job,
            args=(job_id, r_ver, t_ver, token),
            name=f"weighting-job-{job_id}",
            daemon=True,
        )
        self._worker = thread
        thread.start()
        return {
            "job_id": job_id,
            "status": "running",
            "status_label": STATUS_LABELS["running"],
            "respondent_version": r_ver,
            "target_version": t_ver,
            "message": "配权作业已开始",
        }

    def _missing_groups(
        self,
        conn: sqlite3.Connection,
        r_ver: int,
        t_ver: int,
    ) -> list[dict]:
        targets = self._load_targets(conn, t_ver)
        covered = {(t["dimension"], t["group_name"]) for t in targets}
        missing: list[dict] = []
        for dimension, column in (("district", "district"),
                                  ("size_band", "size_band")):
            rows = conn.execute(
                f"SELECT DISTINCT {column} AS g FROM respondent_records "
                "WHERE version = ? ORDER BY g",
                (r_ver,),
            ).fetchall()
            for row in rows:
                if (dimension, row["g"]) not in covered:
                    missing.append(
                        {"dimension": dimension, "group_name": row["g"]}
                    )
        return missing

    def _run_job(self, job_id: int, r_ver: int, t_ver: int, token: uuid.UUID) -> None:
        try:
            with database.db_session(self._db_path) as conn:
                # Immutable snapshots: safe to read without holding the lock.
                households = self._load_respondents(conn, r_ver)
                target_rows = self._load_targets(conn, t_ver)
                target_tuples = [
                    (t["dimension"], t["group_name"], t["target_count"])
                    for t in target_rows
                ]
            result = weighting.solve(households, target_tuples)

            with self._lock:
                # Superseded by a table replacement while we were computing.
                if self._worker_token != token:
                    return
                with database.db_session(self._db_path) as conn:
                    if not self._pair_is_current(conn, r_ver, t_ver):
                        return
                    if result.failure_reason is not None:
                        conn.execute(
                            "UPDATE weighting_jobs SET status='failed', "
                            "error_reason=?, finished_at=? WHERE id=?",
                            (result.failure_reason, _utc_now(), job_id),
                        )
                    elif result.balanced:
                        conn.execute(
                            "UPDATE weighting_jobs SET status='balanced', "
                            "is_balanced=1, objective_gap=?, "
                            "error_reason=NULL, finished_at=? WHERE id=?",
                            (result.gap, _utc_now(), job_id),
                        )
                        conn.executemany(
                            "INSERT INTO weight_results"
                            "(job_id, household_id, weight) VALUES (?, ?, ?)",
                            [
                                (job_id, hh["household_id"], w)
                                for hh, w in zip(households, result.weights)
                            ],
                        )
                        conn.executemany(
                            "INSERT INTO diff_results"
                            "(job_id, dimension, group_name, weighted, "
                            "target_count, difference) VALUES (?, ?, ?, ?, ?, ?)",
                            [
                                (
                                    job_id,
                                    d["dimension"],
                                    d["group_name"],
                                    d["weighted_count"],
                                    d["target_count"],
                                    d["difference"],
                                )
                                for d in result.group_diffs
                            ],
                        )
                    else:
                        conn.execute(
                            "UPDATE weighting_jobs SET status='unbalanced', "
                            "is_balanced=0, objective_gap=?, "
                            "error_reason=NULL, finished_at=? WHERE id=?",
                            (result.gap, _utc_now(), job_id),
                        )
                        conn.executemany(
                            "INSERT INTO diff_results"
                            "(job_id, dimension, group_name, weighted, "
                            "target_count, difference) VALUES (?, ?, ?, ?, ?, ?)",
                            [
                                (
                                    job_id,
                                    d["dimension"],
                                    d["group_name"],
                                    d["weighted_count"],
                                    d["target_count"],
                                    d["difference"],
                                )
                                for d in result.group_diffs
                            ],
                        )
                    conn.commit()
        except Exception as exc:  # pragma: no cover - defensive
            with self._lock:
                if self._worker_token != token:
                    return
                with database.db_session(self._db_path) as conn:
                    if not self._pair_is_current(conn, r_ver, t_ver):
                        return
                    conn.execute(
                        "UPDATE weighting_jobs SET status='failed', "
                        "error_reason=?, finished_at=? WHERE id=?",
                        (f"配权计算异常: {exc!r}", _utc_now(), job_id),
                    )
                    conn.commit()

    def _pair_is_current(
        self, conn: sqlite3.Connection, r_ver: int, t_ver: int
    ) -> bool:
        return (
            self._current_version(conn, "respondents") == r_ver
            and self._current_version(conn, "targets") == t_ver
        )

    # ------------------------------------------------------------------
    # Result access (always carries both table versions)
    # ------------------------------------------------------------------
    def _current_finished_job(self, conn: sqlite3.Connection):
        r_ver = self._current_version(conn, "respondents")
        t_ver = self._current_version(conn, "targets")
        if r_ver is None or t_ver is None:
            missing = [
                name
                for name, ver in (
                    ("respondents", r_ver),
                    ("targets", t_ver),
                )
                if ver is None
            ]
            raise MissingTableError(
                "缺少必需数据表", {"missing_tables": missing}
            )
        job = self._latest_job(conn, r_ver, t_ver)
        if job is None:
            raise NotReadyError(
                "当前两表版本尚未完成配权，结果待生成",
                {
                    "respondent_version": r_ver,
                    "target_version": t_ver,
                },
            )
        if job["status"] == "running":
            raise NotReadyError(
                "配权计算中，请稍后再查询",
                {"job_id": job["id"]},
            )
        if job["status"] == "failed":
            raise NoResultError(
                f"配权计算失败: {job['error_reason']}",
                {
                    "job_id": job["id"],
                    "error_reason": job["error_reason"],
                    "respondent_version": r_ver,
                    "target_version": t_ver,
                },
            )
        return job, r_ver, t_ver

    def get_weights(self, limit: int | None = None, offset: int = 0) -> dict:
        with self._lock, database.db_session(self._db_path) as conn:
            job, r_ver, t_ver = self._current_finished_job(conn)
            if job["status"] != "balanced":
                raise NoResultError(
                    "当前两表版本无法配平，逐户权重不可领取；"
                    "请调用差额接口查看各组加权户数与目标的差额",
                    {
                        "job_id": job["id"],
                        "objective_gap": job["objective_gap"],
                        "respondent_version": r_ver,
                        "target_version": t_ver,
                    },
                )
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM weight_results WHERE job_id = ?",
                (job["id"],),
            ).fetchone()["c"]
            sql = (
                "SELECT household_id, weight FROM weight_results "
                "WHERE job_id = ? ORDER BY household_id"
            )
            params: list = [job["id"]]
            if limit is not None:
                sql += " LIMIT ? OFFSET ?"
                params.extend([int(limit), int(offset)])
            rows = conn.execute(sql, params).fetchall()
            return {
                "status": "balanced",
                "job_id": job["id"],
                "respondent_version": r_ver,
                "target_version": t_ver,
                "objective_gap": job["objective_gap"],
                "total_count": total,
                "limit": limit,
                "offset": offset,
                "weights": [dict(r) for r in rows],
            }

    def get_differences(self) -> dict:
        with self._lock, database.db_session(self._db_path) as conn:
            job, r_ver, t_ver = self._current_finished_job(conn)
            rows = conn.execute(
                "SELECT dimension, group_name, weighted AS weighted_count, "
                "target_count, difference FROM diff_results "
                "WHERE job_id = ? ORDER BY dimension, group_name",
                (job["id"],),
            ).fetchall()
            return {
                "status": job["status"],
                "job_id": job["id"],
                "respondent_version": r_ver,
                "target_version": t_ver,
                "objective_gap": job["objective_gap"],
                "is_balanced": bool(job["is_balanced"]),
                "differences": [dict(r) for r in rows],
                "message": (
                    "各组加权户数减目标户数的差额（difference = weighted - target）"
                    if job["status"] == "unbalanced"
                    else "当前版本已配平，差额仅作核验参考"
                ),
            }

    def get_versions(self) -> dict:
        with self._lock, database.db_session(self._db_path) as conn:
            r_ver = self._current_version(conn, "respondents")
            t_ver = self._current_version(conn, "targets")
            r_meta = None
            t_meta = None
            if r_ver is not None:
                r_meta = dict(conn.execute(
                    "SELECT version, created_at, row_count, content_hash "
                    "FROM respondent_versions WHERE version = ?",
                    (r_ver,),
                ).fetchone())
            if t_ver is not None:
                t_meta = dict(conn.execute(
                    "SELECT version, created_at, row_count, content_hash "
                    "FROM target_versions WHERE version = ?",
                    (t_ver,),
                ).fetchone())
            return {
                "respondents": r_meta,
                "targets": t_meta,
            }
