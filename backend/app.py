from __future__ import annotations

import asyncio
import json
import io
import math
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("WORKBENCH_DB", ROOT / "data" / "workbench.sqlite3"))
KINDS = {
    "connectors": "connector",
    "assets": "asset",
    "intents": "intent",
    "expected_tasks": "expected_task",
    "task_attempts": "task_attempt",
    "collection_records": "collection_record",
    "analysis_results": "analysis_result",
    "metric_snapshots": "metric_snapshot",
    "recovery_fixtures": "recovery_fixture",
}
KEY_FIELDS = {
    "connector": ("connector_id", "id", "name"),
    "asset": ("asset_id", "id"),
    "intent": ("intent_id", "id"),
    "expected_task": ("task_key", "id"),
    "task_attempt": ("attempt_id", "id"),
    "collection_record": ("capture_id", "collection_id", "record_id", "id"),
    "analysis_result": ("analysis_id", "result_id", "id"),
    "metric_snapshot": ("snapshot_id", "id"),
    "recovery_fixture": ("fixture_id", "id"),
}
ANALYSIS_FIELDS = ("summary", "brand_mentioned", "recommended", "evidence_text", "citation_urls", "risk_flags")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db() -> None:
    with closing(connect()) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS records (
            kind TEXT NOT NULL, record_key TEXT NOT NULL, task_key TEXT, payload TEXT NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY(kind, record_key)
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_records_task ON records(task_key, kind)")
        db.execute("""CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY, task_key TEXT NOT NULL, kind TEXT NOT NULL,
            status TEXT NOT NULL, mode TEXT, error TEXT, result TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
        if "job_payload" not in columns:
            db.execute("ALTER TABLE runs ADD COLUMN job_payload TEXT")
        if "dataset_generation" not in columns:
            db.execute("ALTER TABLE runs ADD COLUMN dataset_generation INTEGER NOT NULL DEFAULT 1")
        if "attempt_count" not in columns:
            db.execute("ALTER TABLE runs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
        if "max_attempts" not in columns:
            db.execute("ALTER TABLE runs ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3")
        if "next_run_at" not in columns:
            db.execute("ALTER TABLE runs ADD COLUMN next_run_at TEXT")
        db.execute("CREATE INDEX IF NOT EXISTS idx_runs_queue ON runs(status, created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_runs_due_queue ON runs(status, next_run_at, created_at)")
        db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_one_active_task
            ON runs(kind, task_key) WHERE status IN ('queued', 'running')""")
        db.execute("""CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        )""")
        db.execute("INSERT OR IGNORE INTO app_meta(key, value) VALUES('dataset_generation', '1')")
        db.execute("""CREATE TABLE IF NOT EXISTS import_batches (
            batch_id TEXT PRIMARY KEY, mode TEXT NOT NULL, dataset_generation INTEGER NOT NULL,
            imported INTEGER NOT NULL, sections TEXT NOT NULL, created_at TEXT NOT NULL
        )""")
        record_count = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        batch_count = db.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0]
        if record_count and not batch_count:
            generation = int(db.execute("SELECT value FROM app_meta WHERE key='dataset_generation'").fetchone()[0])
            db.execute("INSERT INTO import_batches(batch_id,mode,dataset_generation,imported,sections,created_at) VALUES(?,?,?,?,?,?)",
                       ("pre-upgrade-dataset", "existing", generation, record_count, "{}", now_iso()))
        db.commit()


def task_key_of(row: dict[str, Any]) -> str | None:
    direct = row.get("task_key") or row.get("business_key")
    if direct:
        return str(direct)
    asset = row.get("asset_id")
    platform = row.get("platform")
    day = row.get("business_date") or row.get("date")
    if asset is not None and platform is not None and day is not None:
        return f"{asset}|{platform}|{day}"
    return None


def record_key(kind: str, row: dict[str, Any], index: int) -> str:
    if kind == "expected_task":
        expected_key = task_key_of(row)
        if expected_key:
            return expected_key
    for field in KEY_FIELDS[kind]:
        value = row.get(field)
        if value is not None and str(value):
            return str(value)
    task_key = task_key_of(row)
    if kind == "metric_snapshot":
        brand = row.get("brand_id", "")
        platform = row.get("platform", "")
        day = row.get("business_date", "")
        if brand and platform and day:
            return f"{brand}|{platform}|{day}"
    if kind == "recovery_fixture" and task_key:
        return task_key
    if kind == "expected_task" and task_key:
        return task_key
    if kind == "analysis_result" and task_key:
        return f"{task_key}:{row.get('created_at') or row.get('updated_at') or index}"
    if kind == "collection_record" and task_key:
        return f"{task_key}:{row.get('attempt_id') or index}"
    raise ValueError(f"记录缺少可识别主键：{kind} 第 {index + 1} 条")


def validate_seed(data: Any, manifest: dict[str, Any] | None = None) -> dict[str, int]:
    if not isinstance(data, dict):
        raise ValueError("数据包顶层必须是 JSON 对象")
    rows_by_section: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    for section, kind in KINDS.items():
        rows = data.get(section, [])
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise ValueError(f"{section} 必须是数组")
        keys: set[str] = set()
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"{section}[{index}] 必须是对象")
            key = record_key(kind, row, index)
            if key in keys:
                raise ValueError(f"{section}[{index}] 主键重复：{key}")
            keys.add(key)
        rows_by_section[section] = rows
        counts[section] = len(rows)

    assets = rows_by_section["assets"]
    intents = rows_by_section["intents"]
    expected = rows_by_section["expected_tasks"]
    attempts = rows_by_section["task_attempts"]
    captures = rows_by_section["collection_records"]
    analyses = rows_by_section["analysis_results"]
    fixtures = rows_by_section["recovery_fixtures"]
    asset_ids = {str(first_value(row, ("asset_id", "id"))) for row in assets}
    intent_ids = {str(first_value(row, ("intent_id", "id"))) for row in intents}
    task_keys = {task_key_of(row) for row in expected}
    attempt_ids = {str(first_value(row, ("attempt_id", "id"))) for row in attempts}
    capture_ids = {str(first_value(row, ("capture_id", "collection_id", "id"))) for row in captures}
    platforms = {str(row.get("platform")) for row in rows_by_section["connectors"] if row.get("platform")}

    def require_ref(section: str, index: int, field: str, value: Any, valid: set[str]) -> None:
        if value is not None and str(value) and str(value) not in valid:
            raise ValueError(f"{section}[{index}].{field} 引用不存在：{value}")

    for index, row in enumerate(expected):
        asset_id = row.get("asset_id")
        platform = row.get("platform")
        business_date = row.get("business_date")
        if not asset_id or not platform or not business_date:
            raise ValueError(f"expected_tasks[{index}] 缺少 asset_id、platform 或 business_date")
        business_key = f"{asset_id}|{platform}|{business_date}"
        if task_key_of(row) != business_key:
            raise ValueError(f"expected_tasks[{index}].task_key 与业务键字段不一致")
        if assets:
            require_ref("expected_tasks", index, "asset_id", asset_id, asset_ids)
        if intents:
            require_ref("expected_tasks", index, "intent_id", row.get("intent_id"), intent_ids)
        if platforms:
            require_ref("expected_tasks", index, "platform", platform, platforms)

    if assets:
        for index, row in enumerate(intents):
            require_ref("intents", index, "asset_id", row.get("asset_id"), asset_ids)
    for section, rows in (("task_attempts", attempts), ("collection_records", captures),
                          ("analysis_results", analyses), ("recovery_fixtures", fixtures)):
        for index, row in enumerate(rows):
            key = task_key_of(row)
            if expected and key not in task_keys:
                raise ValueError(f"{section}[{index}].task_key 不在 expected_tasks 中：{key}")
            if section == "collection_records" and attempts:
                require_ref(section, index, "attempt_id", row.get("attempt_id"), attempt_ids)
            if section == "analysis_results":
                if captures:
                    require_ref(section, index, "capture_id", row.get("capture_id"), capture_ids)
                if row.get("capture_id"):
                    capture = next((item for item in captures if str(first_value(item, ("capture_id", "collection_id", "id"))) == str(row["capture_id"])), None)
                    if capture and task_key_of(capture) != key:
                        raise ValueError(f"analysis_results[{index}].capture_id 属于另一个业务任务")

    if manifest is not None:
        manifest_fields = {
            "asset_count": "assets", "intent_count": "intents", "expected_tasks": "expected_tasks",
            "task_attempt_rows": "task_attempts", "capture_rows": "collection_records",
            "analysis_result_rows": "analysis_results", "metric_snapshot_rows": "metric_snapshots",
            "recovery_rows": "recovery_fixtures",
        }
        for manifest_field, section in manifest_fields.items():
            declared = manifest.get(manifest_field)
            if declared is not None and int(declared) != counts[section]:
                raise ValueError(f"数据清单 {manifest_field}={declared}，但 {section} 实际为 {counts[section]}")
        brand_count = sum(str(row.get("asset_type", "")).lower() == "brand" for row in assets)
        if manifest.get("brand_count") is not None and int(manifest["brand_count"]) != brand_count:
            raise ValueError(f"数据清单 brand_count={manifest['brand_count']}，但品牌资产实际为 {brand_count}")
    return counts


def all_records(db: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    return [json.loads(row["payload"]) for row in db.execute(
        "SELECT payload FROM records WHERE kind=? ORDER BY record_key", (kind,)
    )]


def save_record(db: sqlite3.Connection, kind: str, row: dict[str, Any], key: str | None = None) -> None:
    normalized_key = key or record_key(kind, row, 0)
    task_key = task_key_of(row)
    db.execute("""INSERT INTO records(kind, record_key, task_key, payload, updated_at)
        VALUES(?, ?, ?, ?, ?) ON CONFLICT(kind, record_key) DO UPDATE SET
        task_key=excluded.task_key, payload=excluded.payload, updated_at=excluded.updated_at""",
        (kind, normalized_key, task_key, json.dumps(row, ensure_ascii=False), now_iso()))


def resolved_rows(db: sqlite3.Connection) -> list[dict[str, Any]]:
    expected = all_records(db, "expected_task")
    intents = all_records(db, "intent")
    intent_map = {str(first_value(intent, ("intent_id", "id"))): intent for intent in intents}
    attempts = all_records(db, "task_attempt")
    collections = all_records(db, "collection_record")
    analyses = all_records(db, "analysis_result")
    attempts.sort(key=lambda row: str(first_value(row, ("started_at", "created_at", "updated_at"), "")))
    attempt_task = {str(a.get("attempt_id") or a.get("id")): task_key_of(a) for a in attempts}
    for kind_rows in (collections, analyses):
        for row in kind_rows:
            if not task_key_of(row) and row.get("attempt_id"):
                row["task_key"] = attempt_task.get(str(row["attempt_id"]))
        kind_rows.sort(key=lambda row: str(first_value(row, ("created_at", "captured_at", "collected_at", "analyzed_at", "updated_at", "timestamp"), "")))
    output = []
    for task in expected:
        key = task_key_of(task)
        task_attempts = [r for r in attempts if task_key_of(r) == key]
        task_collections = [r for r in collections if task_key_of(r) == key]
        task_analyses = [r for r in analyses if task_key_of(r) == key]
        intent = intent_map.get(str(task.get("intent_id")), {})
        latest_attempt = task_attempts[-1] if task_attempts else None
        analysis = task_analyses[-1] if task_analyses else None
        collection = task_collections[-1] if task_collections else None
        analysis_capture_id = first_value(analysis, ("capture_id", "collection_id"))
        if analysis_capture_id:
            collection = next((item for item in task_collections
                               if str(first_value(item, ("capture_id", "collection_id", "id"))) == str(analysis_capture_id)), None)
        valid, reason = validate_analysis(collection, analysis)
        output.append({
            "task_key": key,
            "asset_id": task.get("asset_id"), "platform": task.get("platform"),
            "business_date": task.get("business_date"),
            "analysis_due_at": task.get("analysis_due_at"),
            "intent_id": task.get("intent_id"), "user_query": task.get("user_query") or task.get("question") or first_value(intent, ("user_query", "query", "question", "text"), ""),
            "attempt_count": len(task_attempts), "latest_attempt": latest_attempt,
            "collection": collection, "analysis": analysis,
            "valid": valid, "validation_error": reason,
        })
    return output


def first_value(row: dict[str, Any] | None, names: tuple[str, ...], default: Any = None) -> Any:
    if not row:
        return default
    for name in names:
        value = row.get(name)
        if value is not None:
            return value
    return default


def raw_answer(collection: dict[str, Any] | None) -> str:
    value = first_value(collection, ("raw_answer", "answer", "content", "response"), "")
    return value if isinstance(value, str) else ""


def source_urls(collection: dict[str, Any] | None) -> set[str]:
    if not collection:
        return set()
    values = first_value(collection, ("citation_urls", "source_urls", "urls"), [])
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        values = []
    direct = first_value(collection, ("source_url", "url"))
    if direct:
        values.append(direct)
    return {str(value) for value in values if value}


def validate_analysis(collection: dict[str, Any] | None, analysis: dict[str, Any] | None) -> tuple[bool, str | None]:
    if not collection:
        return False, "尚无采集回答"
    if not raw_answer(collection) or collection.get("payload_valid") is False:
        return False, "原始采集记录为空或无效"
    if not analysis:
        return False, "尚无分析结果"
    missing = [field for field in ANALYSIS_FIELDS if field not in analysis]
    if missing:
        return False, "分析字段缺失：" + ", ".join(missing)
    if not isinstance(analysis.get("summary"), str) or not isinstance(analysis.get("brand_mentioned"), bool) or not isinstance(analysis.get("recommended"), bool):
        return False, "summary、brand_mentioned 或 recommended 类型错误"
    answer = raw_answer(collection)
    evidence = analysis.get("evidence_text")
    if not isinstance(evidence, str) or not evidence or evidence not in answer:
        return False, "证据无法在原始回答中定位"
    citations = analysis.get("citation_urls")
    if not isinstance(citations, list) or any(not isinstance(value, str) for value in citations) or not set(citations).issubset(source_urls(collection)):
        return False, "引用链接与采集来源不一致"
    if not isinstance(analysis.get("risk_flags"), list):
        return False, "risk_flags 格式错误"
    return True, None


def filters_match(row: dict[str, Any], brand_id: str | None, asset_id: str | None,
                  platform: str | None, business_date: str | None, assets: dict[str, dict[str, Any]]) -> bool:
    if asset_id and str(row.get("asset_id")) != asset_id:
        return False
    asset = assets.get(str(row.get("asset_id")), {})
    if brand_id and str(asset.get("brand_id")) != brand_id and str(row.get("brand_id")) != brand_id:
        return False
    if platform and row.get("platform") != platform:
        return False
    if business_date and str(row.get("business_date")) != business_date:
        return False
    return True


def public_task(row: dict[str, Any]) -> dict[str, Any]:
    attempt = row["latest_attempt"] or {}
    analysis = row["analysis"] or {}
    collection = row["collection"] or {}
    overdue = not row["valid"] and is_overdue(row.get("analysis_due_at"))
    state = "有效完成" if row["valid"] else "逾期未完成" if overdue else state_label(attempt, collection, analysis)
    return {
        "task_key": row["task_key"], "asset_id": row["asset_id"], "platform": row["platform"],
        "business_date": row["business_date"], "intent_id": row["intent_id"], "user_query": row["user_query"],
        "analysis_due_at": row["analysis_due_at"], "overdue": overdue,
        "status": state, "attempt_count": row["attempt_count"],
        "failure_reason": first_value(attempt, ("error", "error_code", "failure_reason", "message")),
        "summary": analysis.get("summary"), "brand_mentioned": analysis.get("brand_mentioned"),
        "recommended": analysis.get("recommended"), "valid": row["valid"], "validation_error": row["validation_error"],
    }


def state_label(attempt: dict[str, Any], collection: dict[str, Any], analysis: dict[str, Any]) -> str:
    status = str(first_value(attempt, ("status", "state"), "")).lower()
    if status in {"running", "processing", "queued", "pending", "卡住"}:
        return "处理中"
    if status in {"analysis_failed"}:
        return "分析失败"
    if status in {"collection_failed", "failed", "error", "失败"}:
        return "采集失败" if not collection else "分析失败"
    if not collection:
        return "待采集" if not attempt else "无采集结果"
    if not analysis:
        return "待分析"
    return "结果无效"


def issue_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues = []
    for row in rows:
        attempt = row["latest_attempt"] or {}
        status = str(first_value(attempt, ("status", "state"), "")).lower()
        started = first_value(attempt, ("started_at", "queued_at", "created_at", "updated_at"))
        due_at = row.get("analysis_due_at")
        stuck = status in {"running", "processing", "queued", "pending"} and is_stale(started)
        issue_set = []
        if row["attempt_count"] == 0:
            issue_set.append(("missing", "任务缺失", "无执行记录"))
        if status in {"failed", "error", "失败", "collection_failed", "analysis_failed"}:
            code, title = ("analysis_failed", "分析失败") if row["collection"] else ("collection_failed", "采集失败")
            issue_set.append((code, title, first_value(attempt, ("error", "error_code", "failure_reason"), "执行失败")))
        elif row["attempt_count"] and not row["collection"] and status in {"completed", "succeeded", "success"}:
            issue_set.append(("completed_without_result", "执行完成但无采集结果", "执行记录已完成，但找不到关联的原始回答"))
        if stuck:
            issue_set.append(("stuck", "执行超时/卡住", f"任务连续运行超过 5 分钟，开始时间 {started or '未知'}"))
        if not row["valid"] and is_overdue(due_at):
            issue_set.append(("delivery_overdue", "超过交付截止时间", f"截止时间 {due_at}，当前任务仍未有效完成"))
        if row["collection"] and not row["analysis"]:
            issue_set.append(("analysis_missing", "分析缺失", "已采集但尚无分析结果"))
        elif row["analysis"] and not row["collection"]:
            issue_set.append(("analysis_capture_missing", "分析缺少对应采集记录", "当前分析结果引用的 capture_id 不存在"))
        elif row["collection"] and row["analysis"] and not row["valid"]:
            reason = row["validation_error"] or "分析结果无效"
            if "证据" in reason:
                code, title = "evidence_mismatch", "证据与原文不一致"
            elif "引用" in reason:
                code, title = "citation_mismatch", "引用来源不一致"
            else:
                code, title = "analysis_invalid", "分析结果无效"
            issue_set.append((code, title, reason))
        if row["attempt_count"] > 1:
            issue_set.append(("duplicate", "重复执行", f"共 {row['attempt_count']} 次执行尝试，按业务键只计一条任务"))
        for code, title, detail in issue_set:
            issues.append({"task_key": row["task_key"], "asset_id": row["asset_id"], "platform": row["platform"],
                           "business_date": row["business_date"], "code": code, "title": title, "detail": detail})
    return issues


def snapshot_issues(db: sqlite3.Connection, brand_id: str | None = None, asset_id: str | None = None,
                    platform: str | None = None, business_date: str | None = None) -> list[dict[str, Any]]:
    if asset_id:
        return []
    rows = resolved_rows(db)
    assets = all_records(db, "asset")
    asset_map = {str(first_value(asset, ("asset_id", "id"))): asset for asset in assets}
    output = []
    for snapshot in all_records(db, "metric_snapshot"):
        snap_brand = str(snapshot.get("brand_id") or "")
        snap_platform = str(snapshot.get("platform") or "")
        snap_date = str(snapshot.get("business_date") or "")
        if brand_id and snap_brand != brand_id or platform and snap_platform != platform or business_date and snap_date != business_date:
            continue
        group = [row for row in rows if str(row.get("platform") or "") == snap_platform
                 and str(row.get("business_date") or "") == snap_date
                 and str(row.get("brand_id") or asset_map.get(str(row.get("asset_id")), {}).get("brand_id") or "") == snap_brand]
        if not group:
            continue
        valid = [row for row in group if row["valid"]]
        mentions = sum(bool((row["analysis"] or {}).get("brand_mentioned")) for row in valid)
        actual_rate = mentions / len(valid) if valid else 0.0
        differences = []
        expected_snapshot = first_value(snapshot, ("expected_tasks", "expected_count", "total_expected"))
        if expected_snapshot is not None and int(expected_snapshot) != len(group):
            differences.append(f"应有任务数：快照 {expected_snapshot}，重算 {len(group)}")
        completed_snapshot = first_value(snapshot, ("reported_completed", "completed_count", "valid_complete"))
        if completed_snapshot is not None and int(completed_snapshot) != len(valid):
            differences.append(f"有效完成数：快照 {completed_snapshot}，重算 {len(valid)}")
        rate_snapshot = first_value(snapshot, ("reported_mention_rate", "mention_rate"))
        if rate_snapshot not in (None, ""):
            try:
                if not math.isclose(float(rate_snapshot), actual_rate, rel_tol=0, abs_tol=0.0002):
                    differences.append(f"提及率：快照 {float(rate_snapshot):.4f}，重算 {actual_rate:.4f}")
            except (TypeError, ValueError):
                differences.append("提及率字段无法解析")
        if differences:
            output.append({"task_key": f"snapshot|{snap_brand}|{snap_platform}|{snap_date}", "asset_id": None,
                           "platform": snap_platform, "business_date": snap_date, "code": "metric_mismatch",
                           "title": "统计快照不一致", "detail": "；".join(differences)})
    return output


def is_stale(value: Any) -> bool:
    if not value:
        return False
    try:
        start = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return (datetime.now(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() > 300
    except (ValueError, TypeError):
        return False


def is_overdue(value: Any) -> bool:
    if not value:
        return False
    try:
        due = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if due.tzinfo is None:
            due = due.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return datetime.now(timezone.utc) > due.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return False


def analysis_scope(asset_id: str | None, assets: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    asset_map = {str(first_value(asset, ("asset_id", "id"))): asset for asset in assets}
    asset = asset_map.get(str(asset_id), {})
    target_names = [str(first_value(asset, ("asset_name", "name", "product_name", "brand_name"), ""))]
    brand_id = first_value(asset, ("brand_id",))
    brand = asset_map.get(str(brand_id), {}) if brand_id else asset
    brand_names = [str(first_value(brand, ("brand_name", "asset_name", "name"), ""))]
    return [name for name in target_names if name], [name for name in brand_names if name]


def mock_analysis(collection: dict[str, Any], assets: list[dict[str, Any]], asset_id: str | None) -> dict[str, Any]:
    answer = raw_answer(collection)
    target_names, brand_names = analysis_scope(asset_id or collection.get("asset_id"), assets)
    mentioned = any(name in answer for name in target_names + brand_names)
    evidence = next((sentence.strip() for sentence in re.split(r"(?<=[。！？.!?])\s*", answer) if sentence.strip()), "")
    return {
        "summary": answer[:160], "brand_mentioned": mentioned, "recommended": mentioned,
        "evidence_text": evidence, "citation_urls": sorted(source_urls(collection)), "risk_flags": [],
    }


def live_analysis(collection: dict[str, Any], assets: list[dict[str, Any]], asset_id: str | None) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("未配置 OPENAI_API_KEY")
    base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    prompt = {
        "task": "Extract a JSON object with summary, brand_mentioned (boolean), recommended (boolean), evidence_text (verbatim substring), citation_urls (array), risk_flags (array). Use only the supplied answer and source URLs.",
        "answer": raw_answer(collection), "source_urls": sorted(source_urls(collection)),
        "target_asset_names": analysis_scope(asset_id or collection.get("asset_id"), assets)[0],
        "target_brand_names": analysis_scope(asset_id or collection.get("asset_id"), assets)[1],
    }
    request = urllib.request.Request(
        f"{base}/chat/completions", data=json.dumps({"model": model, "messages": [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}], "response_format": {"type": "json_object"}}).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read())
    return json.loads(payload["choices"][0]["message"]["content"])


class InspectionBody(BaseModel):
    brand_id: str | None = None
    asset_id: str | None = None
    platform: str | None = None
    business_date: str | None = None


class RunBody(BaseModel):
    mode: str = "mock"


app = FastAPI(title="声量观测台 API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["*"], allow_headers=["*"])
init_db()


@app.on_event("startup")
async def resume_persisted_jobs() -> None:
    with closing(connect()) as db:
        db.execute("UPDATE runs SET status='queued', error=NULL, updated_at=? WHERE status='running'", (now_iso(),))
        db.commit()
        pending = [dict(row) for row in db.execute("SELECT * FROM runs WHERE status='queued' ORDER BY created_at")]
    for run in pending:
        asyncio.create_task(asyncio.to_thread(dispatch_job, run["run_id"]))


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/dataset")
def dataset_status() -> dict[str, Any]:
    with closing(connect()) as db:
        row = db.execute("SELECT * FROM import_batches ORDER BY created_at DESC LIMIT 1").fetchone()
        generation = db.execute("SELECT value FROM app_meta WHERE key='dataset_generation'").fetchone()[0]
        return {"generation": int(generation), "latest_import": dict(row) if row else None,
                "record_count": db.execute("SELECT COUNT(*) FROM records").fetchone()[0]}


@app.post("/api/v1/seed/import")
def import_seed(data: dict[str, Any], mode: str = "merge") -> dict[str, Any]:
    if mode not in {"merge", "replace"}:
        raise HTTPException(status_code=422, detail="mode 只能是 merge 或 replace")
    try:
        sections = validate_seed(data)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    total = 0
    with closing(connect()) as db:
        try:
            db.execute("BEGIN")
            current_generation = int(db.execute("SELECT value FROM app_meta WHERE key='dataset_generation'").fetchone()[0])
            generation = current_generation + 1 if mode == "replace" else current_generation
            if mode == "replace":
                db.execute("DELETE FROM records")
                db.execute("UPDATE runs SET status='cancelled', error='数据集被替换', updated_at=? WHERE status IN ('queued','running')", (now_iso(),))
            for section, kind in KINDS.items():
                values = data.get(section, [])
                if values is None:
                    continue
                for index, row in enumerate(values):
                    key = record_key(kind, row, index)
                    save_record(db, kind, row, key)
                    total += 1
            if total == 0:
                raise ValueError("未识别到可导入的数据段")
            if mode == "replace":
                db.execute("UPDATE app_meta SET value=? WHERE key='dataset_generation'", (str(generation),))
            batch_id = str(uuid.uuid4())
            db.execute("INSERT INTO import_batches(batch_id, mode, dataset_generation, imported, sections, created_at) VALUES(?,?,?,?,?,?)",
                       (batch_id, mode, generation, total, json.dumps(sections, ensure_ascii=False), now_iso()))
            db.commit()
        except (ValueError, sqlite3.Error) as exc:
            db.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"batch_id": batch_id, "mode": mode, "generation": generation, "imported": total, "sections": sections}


@app.post("/api/v1/seed/import-archive")
async def import_archive(request: Request, mode: str = "merge") -> dict[str, Any]:
    archive = await request.body()
    if len(archive) > 20_000_000:
        raise HTTPException(status_code=413, detail="压缩包超过 20 MB 限制")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as package:
            candidates = [entry for entry in package.infolist() if Path(entry.filename).name.lower() == "seed_all.json"]
            if len(candidates) != 1:
                raise ValueError("压缩包内必须且只能包含一个 seed_all.json")
            entry = candidates[0]
            if entry.file_size > 5_000_000:
                raise ValueError("seed_all.json 超过 5 MB 限制")
            data = json.loads(package.read(entry).decode("utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("seed_all.json 顶层必须是 JSON 对象")
            manifest_entries = [item for item in package.infolist() if Path(item.filename).name.lower() == "dataset_manifest.json"]
            manifest = None
            if manifest_entries:
                if len(manifest_entries) != 1:
                    raise ValueError("压缩包内 dataset_manifest.json 不能重复")
                if manifest_entries[0].file_size > 100_000:
                    raise ValueError("dataset_manifest.json 超过 100 KB 限制")
                manifest = json.loads(package.read(manifest_entries[0]).decode("utf-8-sig"))
                if not isinstance(manifest, dict):
                    raise ValueError("dataset_manifest.json 顶层必须是 JSON 对象")
                validate_seed(data, manifest)
    except (zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"无法读取数据包：{exc}") from exc
    return import_seed(data, mode)


def query_rows(brand_id: str | None, asset_id: str | None, platform: str | None, business_date: str | None) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    with closing(connect()) as db:
        assets = all_records(db, "asset")
        asset_map = {str(first_value(a, ("asset_id", "id"))): a for a in assets}
        rows = [r for r in resolved_rows(db) if filters_match(r, brand_id, asset_id, platform, business_date, asset_map)]
    return rows, asset_map


@app.get("/api/v1/overview")
def overview(brand_id: str | None = None, asset_id: str | None = None, platform: str | None = None,
             business_date: str | None = None) -> dict[str, Any]:
    rows, asset_map = query_rows(brand_id, asset_id, platform, business_date)
    issues = issue_rows(rows) if rows else []
    with closing(connect()) as db:
        metric_issues = snapshot_issues(db, brand_id, asset_id, platform, business_date)
    completed = sum(r["valid"] for r in rows)
    mention_rows = [r for r in rows if r["valid"]]
    mentions = sum(bool((r["analysis"] or {}).get("brand_mentioned")) for r in mention_rows)
    return {"expected": len(rows), "valid_complete": completed, "exceptions": len({issue["task_key"] for issue in issues}),
            "statistic_diffs": len(metric_issues),
            "completion_rate": round(completed / len(rows) * 100, 1) if rows else 0,
            "mention_rate": round(mentions / len(mention_rows) * 100, 1) if mention_rows else 0,
            "platforms": sorted({str(r["platform"]) for r in rows if r["platform"]}),
            "dates": sorted({str(r["business_date"]) for r in rows if r["business_date"]}),
            "brands": sorted({(str(a.get("brand_id")), str(first_value(a, ("brand_name", "brand"), a.get("brand_id"))))
                              for a in asset_map.values() if a.get("brand_id")}),
            "assets": [{"asset_id": k, "name": first_value(a, ("name", "asset_name", "product_name", "brand_name"), k), "brand_id": a.get("brand_id")}
                       for k, a in asset_map.items()]}


@app.get("/api/v1/tasks")
def tasks(brand_id: str | None = None, asset_id: str | None = None, platform: str | None = None,
          business_date: str | None = None, status: str | None = None,
          q: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    rows, _ = query_rows(brand_id, asset_id, platform, business_date)
    public = [public_task(row) for row in rows]
    if status:
        public = [r for r in public if r["status"] == status]
    if q:
        public = [r for r in public if q.lower() in (str(r["task_key"]) + str(r["user_query"])).lower()]
    public.sort(key=lambda row: (str(row["business_date"] or ""), str(row["task_key"] or "")), reverse=True)
    total = len(public)
    start = (page - 1) * page_size
    return {"items": public[start:start + page_size], "total": total, "page": page, "page_size": page_size,
            "page_count": max(1, (total + page_size - 1) // page_size)}


@app.get("/api/v1/tasks/{key:path}")
def task_detail(key: str) -> dict[str, Any]:
    with closing(connect()) as db:
        for row in resolved_rows(db):
            if row["task_key"] == key:
                attempts = [a for a in all_records(db, "task_attempt") if task_key_of(a) == key]
                attempts.sort(key=lambda row: str(first_value(row, ("started_at", "queued_at", "created_at", "updated_at"), "")))
                collection = row["collection"] or {}
                analysis = row["analysis"] or {}
                return {**public_task(row), "task": {"asset_id": row["asset_id"], "platform": row["platform"],
                        "business_date": row["business_date"], "analysis_due_at": row["analysis_due_at"],
                        "intent_id": row["intent_id"], "user_query": row["user_query"]},
                        "raw_answer": raw_answer(collection), "source_urls": sorted(source_urls(collection)),
                        "analysis": analysis, "attempts": attempts, "valid": row["valid"],
                        "validation_error": row["validation_error"]}
    raise HTTPException(status_code=404, detail="未找到任务")


@app.post("/api/v1/inspections")
def inspect(body: InspectionBody) -> dict[str, Any]:
    rows, _ = query_rows(body.brand_id, body.asset_id, body.platform, body.business_date)
    issues = issue_rows(rows)
    with closing(connect()) as db:
        issues.extend(snapshot_issues(db, body.brand_id, body.asset_id, body.platform, body.business_date))
    return {"run_id": str(uuid.uuid4()), "created_at": now_iso(), "checked": len(rows), "issue_count": len(issues), "issues": issues}


def run_analysis(run_id: str, key: str, mode: str) -> None:
    with closing(connect()) as db:
        claimed = db.execute("UPDATE runs SET status='running',attempt_count=attempt_count+1,next_run_at=NULL,updated_at=? WHERE run_id=? AND status='queued'", (now_iso(), run_id))
        if claimed.rowcount != 1:
            db.commit()
            return
        run = db.execute("SELECT dataset_generation,attempt_count,max_attempts FROM runs WHERE run_id=? AND status='running'", (run_id,)).fetchone()
        if not run:
            db.commit()
            return
        generation = run["dataset_generation"]
        db.commit()
        row = next((r for r in resolved_rows(db) if r["task_key"] == key), None)
        if not row or not row["collection"]:
            db.execute("UPDATE runs SET status='failed', error=?, updated_at=? WHERE run_id=? AND status='running'", ("任务没有可解析的采集回答", now_iso(), run_id))
            db.commit()
            return
        try:
            assets = all_records(db, "asset")
            result = live_analysis(row["collection"], assets, row["asset_id"]) if mode == "live" else mock_analysis(row["collection"], assets, row["asset_id"])
            valid, reason = validate_analysis(row["collection"], result)
            if not valid:
                raise ValueError(reason or "分析结果校验失败")
            result.update({"task_key": key, "capture_id": first_value(row["collection"], ("capture_id", "collection_id", "id")),
                           "mode": mode, "created_at": now_iso()})
            db.execute("BEGIN IMMEDIATE")
            current_generation = int(db.execute("SELECT value FROM app_meta WHERE key='dataset_generation'").fetchone()[0])
            current_status = db.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if current_generation != generation or not current_status or current_status["status"] != "running":
                db.rollback()
                return
            save_record(db, "analysis_result", result, f"{key}:{uuid.uuid4()}")
            db.execute("UPDATE runs SET status='succeeded', mode=?, result=?, error=NULL,next_run_at=NULL,updated_at=? WHERE run_id=?",
                       (mode, json.dumps(result, ensure_ascii=False), now_iso(), run_id))
            db.commit()
        except Exception as exc:
            current_generation = int(db.execute("SELECT value FROM app_meta WHERE key='dataset_generation'").fetchone()[0])
            if current_generation == generation:
                retryable = mode == "live" and is_retryable_error(exc) and run["attempt_count"] < run["max_attempts"]
                if retryable:
                    delay = min(30, 2 ** (run["attempt_count"] - 1))
                    next_run = datetime.now(timezone.utc).timestamp() + delay
                    next_run_at = datetime.fromtimestamp(next_run, timezone.utc).isoformat(timespec="seconds")
                    db.execute("UPDATE runs SET status='queued',mode=?,error=?,next_run_at=?,updated_at=? WHERE run_id=? AND status='running'",
                               (mode, str(exc), next_run_at, now_iso(), run_id))
                else:
                    db.execute("UPDATE runs SET status='failed',mode=?,error=?,next_run_at=NULL,updated_at=? WHERE run_id=? AND status='running'",
                               (mode, str(exc), now_iso(), run_id))
            db.commit()


def is_retryable_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {408, 425, 429} or exc.code >= 500
    if isinstance(exc, urllib.error.URLError):
        return True
    return isinstance(exc, (TimeoutError, ConnectionError))


def dispatch_job(run_id: str) -> None:
    while True:
        with closing(connect()) as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row or row["status"] != "queued":
                return
            run = dict(row)
        next_run_at = run.get("next_run_at")
        if next_run_at:
            try:
                due = datetime.fromisoformat(next_run_at.replace("Z", "+00:00"))
                if due.tzinfo is None:
                    due = due.replace(tzinfo=timezone.utc)
                delay = (due.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
                if delay > 0:
                    time.sleep(min(delay, 1.0))
                    continue
            except ValueError:
                pass
        if run["kind"] == "reanalyze":
            run_analysis(run_id, run["task_key"], run.get("mode") or "mock")
        elif run["kind"] == "retry":
            payload = json.loads(run.get("job_payload") or "{}")
            run_retry(run_id, run["task_key"], payload["attempt_id"], payload["fixture"])
        else:
            with closing(connect()) as db:
                db.execute("UPDATE runs SET status='failed',error='未知作业类型',updated_at=? WHERE run_id=? AND status='queued'", (now_iso(), run_id))
                db.commit()
            return


@app.post("/api/v1/reanalyze", status_code=202)
def reanalyze(task_key: str, body: RunBody, background: BackgroundTasks) -> dict[str, Any]:
    if body.mode not in {"mock", "live"}:
        raise HTTPException(status_code=422, detail="mode 只能是 mock 或 live")
    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        row = next((r for r in resolved_rows(db) if r["task_key"] == task_key), None)
        if not row:
            raise HTTPException(status_code=404, detail="未找到任务")
        existing = db.execute("SELECT run_id,status,mode FROM runs WHERE kind='reanalyze' AND task_key=? AND status IN ('queued','running')", (task_key,)).fetchone()
        if existing:
            return {"run_id": existing["run_id"], "task_key": task_key, "status": existing["status"], "mode": existing["mode"], "reused": True}
        run_id = str(uuid.uuid4())
        timestamp = now_iso()
        generation = int(db.execute("SELECT value FROM app_meta WHERE key='dataset_generation'").fetchone()[0])
        db.execute("INSERT INTO runs(run_id, task_key, kind, status, mode, created_at, updated_at, dataset_generation) VALUES(?,?,?,?,?,?,?,?)",
                   (run_id, task_key, "reanalyze", "queued", body.mode, timestamp, timestamp, generation))
        db.commit()
    background.add_task(dispatch_job, run_id)
    return {"run_id": run_id, "task_key": task_key, "status": "queued", "mode": body.mode}


@app.post("/api/v1/retry", status_code=202)
def retry_task(task_key: str, background: BackgroundTasks) -> dict[str, Any]:
    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        task = next((r for r in all_records(db, "expected_task") if task_key_of(r) == task_key), None)
        if not task:
            raise HTTPException(status_code=404, detail="未找到任务")
        fixtures = [r for r in all_records(db, "recovery_fixture") if task_key_of(r) == task_key or r.get("asset_id") == task.get("asset_id") and r.get("platform") == task.get("platform") and r.get("business_date") == task.get("business_date")]
        if not fixtures:
            raise HTTPException(status_code=409, detail="数据包中没有该任务的补采输入")
        active_run = db.execute("SELECT run_id,status,job_payload FROM runs WHERE kind='retry' AND task_key=? AND status IN ('queued','running')", (task_key,)).fetchone()
        if active_run:
            payload = json.loads(active_run["job_payload"] or "{}")
            return {"task_key": task_key, "attempt_id": payload.get("attempt_id"), "run_id": active_run["run_id"], "status": active_run["status"], "reused": True}
        existing = next((r for r in all_records(db, "task_attempt")
                         if task_key_of(r) == task_key and str(r.get("status", "")).lower() in {"queued", "pending", "running", "processing"}), None)
        if existing:
            return {"task_key": task_key, "attempt_id": existing.get("attempt_id") or existing.get("id"), "status": existing.get("status"), "reused": True}
        fixture = fixtures[0]
        attempt_id = str(uuid.uuid4())
        timestamp = now_iso()
        attempt = {"attempt_id": attempt_id, "task_key": task_key, "status": "queued", "created_at": timestamp, "source": "recovery_fixture"}
        save_record(db, "task_attempt", attempt)
        run_id = str(uuid.uuid4())
        generation = int(db.execute("SELECT value FROM app_meta WHERE key='dataset_generation'").fetchone()[0])
        payload = json.dumps({"attempt_id": attempt_id, "fixture": fixtures[0]}, ensure_ascii=False)
        db.execute("INSERT INTO runs(run_id,task_key,kind,status,mode,job_payload,created_at,updated_at,dataset_generation,max_attempts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (run_id, task_key, "retry", "queued", "mock", payload, timestamp, timestamp, generation, 1))
        db.commit()
    background.add_task(dispatch_job, run_id)
    return {"task_key": task_key, "attempt_id": attempt_id, "run_id": run_id, "status": "queued", "reused": False}


def run_retry(run_id: str, task_key: str, attempt_id: str, fixture: dict[str, Any]) -> None:
    with closing(connect()) as db:
        claimed = db.execute("UPDATE runs SET status='running',attempt_count=attempt_count+1,next_run_at=NULL,updated_at=? WHERE run_id=? AND status='queued'", (now_iso(), run_id))
        if claimed.rowcount != 1:
            db.commit()
            return
        run = db.execute("SELECT dataset_generation FROM runs WHERE run_id=?", (run_id,)).fetchone()
        generation = run["dataset_generation"]
        rows = all_records(db, "task_attempt")
        attempt = next((row for row in rows if str(row.get("attempt_id") or row.get("id")) == attempt_id), None)
        if not attempt:
            db.execute("UPDATE runs SET status='failed',error=?,updated_at=? WHERE run_id=?", ("执行尝试记录不存在", now_iso(), run_id))
            db.commit()
            return
        attempt.update({"status": "running", "started_at": now_iso()})
        save_record(db, "task_attempt", attempt, attempt_id)
        db.commit()
        try:
            answer = first_value(fixture, ("raw_answer", "answer", "content", "response"))
            if not answer:
                raise ValueError("补采输入中没有原始回答")
            timestamp = now_iso()
            urls = first_value(fixture, ("citation_urls", "source_urls", "urls"), None)
            if urls is None:
                url = first_value(fixture, ("source_url", "url"))
                urls = [url] if url else []
            elif isinstance(urls, str):
                urls = [urls]
            expected = next((row for row in all_records(db, "expected_task") if task_key_of(row) == task_key), {})
            asset_id = expected.get("asset_id")
            assets = all_records(db, "asset")
            collection = {"capture_id": str(uuid.uuid4()), "task_key": task_key, "asset_id": asset_id, "attempt_id": attempt_id,
                          "raw_answer": answer, "citation_urls": urls, "source_url": urls[0] if urls else None, "created_at": timestamp}
            analysis = mock_analysis(collection, assets, asset_id)
            valid, reason = validate_analysis(collection, analysis)
            if not valid:
                raise ValueError(reason or "补采后的分析结果校验失败")
            analysis.update({"task_key": task_key, "capture_id": collection["capture_id"], "mode": "mock",
                             "model_name": "mock-parser", "created_at": timestamp})
            db.execute("BEGIN IMMEDIATE")
            current_generation = int(db.execute("SELECT value FROM app_meta WHERE key='dataset_generation'").fetchone()[0])
            current_run = db.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if current_generation != generation or not current_run or current_run["status"] != "running":
                db.rollback()
                return
            save_record(db, "collection_record", collection)
            save_record(db, "analysis_result", analysis, f"{task_key}:{uuid.uuid4()}")
            attempt.update({"state": "completed", "status": "succeeded", "finished_at": timestamp})
            save_record(db, "task_attempt", attempt, attempt_id)
            db.execute("UPDATE runs SET status='succeeded',result=?,error=NULL,next_run_at=NULL,updated_at=? WHERE run_id=?",
                       (json.dumps(analysis, ensure_ascii=False), timestamp, run_id))
            db.commit()
        except Exception as exc:
            current_generation = int(db.execute("SELECT value FROM app_meta WHERE key='dataset_generation'").fetchone()[0])
            current_run = db.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if current_generation == generation and current_run and current_run["status"] == "running":
                attempt.update({"status": "failed", "state": "collection_failed", "finished_at": now_iso(), "failure_reason": str(exc)})
                save_record(db, "task_attempt", attempt, attempt_id)
                db.execute("UPDATE runs SET status='failed',error=?,updated_at=? WHERE run_id=? AND status='running'", (str(exc), now_iso(), run_id))
            db.commit()


@app.get("/api/v1/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    with closing(connect()) as db:
        row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="未找到运行记录")
        result = dict(row)
        if result.get("result"):
            result["result"] = json.loads(result["result"])
        result.pop("job_payload", None)
        return result
