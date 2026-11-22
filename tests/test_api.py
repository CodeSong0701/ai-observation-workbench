import io
import json
import os
import tempfile
import unittest
import zipfile
import asyncio
import urllib.error
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

os.environ["WORKBENCH_DB"] = str(Path(tempfile.gettempdir()) / "ai-workbench-test.sqlite3")

from fastapi.testclient import TestClient

from backend import app as api


class WorkbenchApiTests(unittest.TestCase):
    def setUp(self):
        api.DB_PATH = Path(tempfile.gettempdir()) / f"ai-workbench-{os.getpid()}-{id(self)}.sqlite3"
        api.init_db()
        self.client = TestClient(api.app)
        self.seed = {
            "assets": [{"asset_id": "a1", "brand_id": "b1", "asset_name": "Northwind"}],
            "intents": [{"intent_id": "i1", "question": "推荐 Northwind"}],
            "expected_tasks": [{"task_key": "a1|ChatGPT|2026-11-20", "asset_id": "a1", "intent_id": "i1", "platform": "ChatGPT", "business_date": "2026-11-20", "question": "推荐 Northwind"}],
            "task_attempts": [{"attempt_id": "try1", "task_key": "a1|ChatGPT|2026-11-20", "state": "completed", "finished_at": "2026-11-20T09:00:00"}],
            "collection_records": [{"capture_id": "c1", "task_key": "a1|ChatGPT|2026-11-20", "attempt_id": "try1", "raw_answer": "Northwind is recommended.", "source_url": "https://example.test/source", "captured_at": "2026-11-20T09:00:00", "payload_valid": True}],
            "analysis_results": [{"result_id": "an1", "task_key": "a1|ChatGPT|2026-11-20", "capture_id": "c1", "model_mode": "seed_mock", "analyzed_at": "2026-11-20T09:10:00", "summary": "Northwind is recommended.", "brand_mentioned": True, "recommended": True, "evidence_text": "Northwind is recommended.", "citation_urls": ["https://example.test/source"], "risk_flags": []}],
            "metric_snapshots": [{"brand_id": "b1", "platform": "ChatGPT", "business_date": "2026-11-20", "expected_tasks": 1, "reported_completed": 1, "reported_mention_rate": 1.0}],
            "recovery_fixtures": [{"task_key": "a1|ChatGPT|2026-11-20", "raw_answer": "Northwind is recommended again.", "source_url": "https://example.test/source"}],
        }

    def tearDown(self):
        self.client.close()
        if api.DB_PATH.exists():
            api.DB_PATH.unlink()

    def test_import_overview_and_reimport_are_idempotent(self):
        first = self.client.post("/api/v1/seed/import", json=self.seed)
        second = self.client.post("/api/v1/seed/import", json=self.seed)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["imported"], second.json()["imported"])
        overview = self.client.get("/api/v1/overview").json()
        self.assertEqual(overview["expected"], 1)
        self.assertEqual(overview["valid_complete"], 1)
        self.assertEqual(overview["completion_rate"], 100)
        self.assertEqual(overview["statistic_diffs"], 0)

    def test_merge_keeps_records_from_previous_batch(self):
        self.client.post("/api/v1/seed/import?mode=replace", json=self.seed)
        additional = {"expected_tasks": [{"task_key": "a2|Gemini|2026-11-20", "asset_id": "a2", "platform": "Gemini", "business_date": "2026-11-20"}]}
        response = self.client.post("/api/v1/seed/import?mode=merge", json=additional)
        overview = self.client.get("/api/v1/overview").json()
        self.assertEqual(response.json()["mode"], "merge")
        self.assertEqual(overview["expected"], 2)

    def test_inspection_detects_invalid_evidence_and_retry_persists_attempt(self):
        broken = {**self.seed, "analysis_results": [{**self.seed["analysis_results"][0], "evidence_text": "invented evidence"}]}
        self.client.post("/api/v1/seed/import", json=broken)
        result = self.client.post("/api/v1/inspections", json={}).json()
        self.assertTrue(any(issue["code"] == "evidence_mismatch" for issue in result["issues"]))
        retry = self.client.post("/api/v1/retry", params={"task_key": "a1|ChatGPT|2026-11-20"})
        self.assertEqual(retry.status_code, 202)
        detail = self.client.get("/api/v1/tasks/a1%7CChatGPT%7C2026-11-20").json()
        self.assertEqual(len(detail["attempts"]), 2)
        self.assertTrue(detail["valid"])
        self.assertEqual(detail["raw_answer"], "Northwind is recommended again.")

    def test_mock_reanalysis_records_mode_and_result(self):
        self.client.post("/api/v1/seed/import", json=self.seed)
        response = self.client.post("/api/v1/reanalyze", params={"task_key": "a1|ChatGPT|2026-11-20"}, json={"mode": "mock"})
        self.assertEqual(response.status_code, 202)
        run = self.client.get(f"/api/v1/runs/{response.json()['run_id']}").json()
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["mode"], "mock")

    def test_zip_import_and_snapshot_audit(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
            package.writestr("nested/seed_all.json", json.dumps(self.seed))
        imported = self.client.post("/api/v1/seed/import-archive", content=archive.getvalue(), headers={"Content-Type": "application/zip"})
        self.assertEqual(imported.status_code, 200)
        damaged = {**self.seed, "metric_snapshots": [{**self.seed["metric_snapshots"][0], "reported_completed": 0}]}
        self.client.post("/api/v1/seed/import", json=damaged)
        overview = self.client.get("/api/v1/overview").json()
        inspection = self.client.post("/api/v1/inspections", json={}).json()
        self.assertEqual(overview["statistic_diffs"], 1)
        self.assertTrue(any(issue["code"] == "metric_mismatch" for issue in inspection["issues"]))

    def test_seed_validation_rejects_duplicate_keys_and_bad_manifest(self):
        duplicate = {**self.seed, "expected_tasks": [*self.seed["expected_tasks"], self.seed["expected_tasks"][0]]}
        response = self.client.post("/api/v1/seed/import?mode=replace", json=duplicate)
        self.assertEqual(response.status_code, 422)
        self.assertIn("主键重复", response.json()["detail"])

        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
            package.writestr("seed_all.json", json.dumps(self.seed))
            package.writestr("dataset_manifest.json", json.dumps({"expected_tasks": 999}))
        response = self.client.post("/api/v1/seed/import-archive", content=archive.getvalue(), headers={"Content-Type": "application/zip"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("expected_tasks=999", response.json()["detail"])

    def test_live_transient_errors_retry_with_persisted_attempt_count(self):
        self.client.post("/api/v1/seed/import", json=self.seed)
        valid_result = {
            "summary": "Northwind is recommended.", "brand_mentioned": True, "recommended": True,
            "evidence_text": "Northwind is recommended.", "citation_urls": ["https://example.test/source"], "risk_flags": [],
        }
        errors = [urllib.error.HTTPError("https://example.test", 503, "Unavailable", {}, None) for _ in range(2)]
        with patch.object(api, "live_analysis", side_effect=[*errors, valid_result]):
            response = self.client.post("/api/v1/reanalyze", params={"task_key": "a1|ChatGPT|2026-11-20"}, json={"mode": "live"})
        self.assertEqual(response.status_code, 202)
        run = self.client.get(f"/api/v1/runs/{response.json()['run_id']}").json()
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["attempt_count"], 3)
        self.assertEqual(run["max_attempts"], 3)

    def test_non_transient_live_error_fails_without_retry(self):
        self.client.post("/api/v1/seed/import", json=self.seed)
        error = urllib.error.HTTPError("https://example.test", 400, "Bad request", {}, None)
        with patch.object(api, "live_analysis", side_effect=error):
            response = self.client.post("/api/v1/reanalyze", params={"task_key": "a1|ChatGPT|2026-11-20"}, json={"mode": "live"})
        run = self.client.get(f"/api/v1/runs/{response.json()['run_id']}").json()
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["attempt_count"], 1)
        self.assertIn("400", run["error"])

    def test_pagination_deadline_and_replace_mode(self):
        tasks = [{"task_key": f"a1|ChatGPT|2026-11-{day:02d}", "asset_id": "a1", "platform": "ChatGPT",
                  "business_date": f"2026-11-{day:02d}", "analysis_due_at": "2026-11-20T12:00:00", "question": "q"}
                 for day in range(1, 26)]
        data = {**self.seed, "expected_tasks": tasks, "task_attempts": [], "collection_records": [], "analysis_results": [], "metric_snapshots": [], "recovery_fixtures": []}
        self.client.post("/api/v1/seed/import?mode=replace", json=data)
        first = self.client.get("/api/v1/tasks?page=1&page_size=20").json()
        second = self.client.get("/api/v1/tasks?page=2&page_size=20").json()
        self.assertEqual((first["total"], len(first["items"]), first["page_count"]), (25, 20, 2))
        self.assertEqual((second["page"], len(second["items"])), (2, 5))
        self.assertTrue(all(item["status"] == "逾期未完成" for item in first["items"]))
        inspection = self.client.post("/api/v1/inspections", json={}).json()
        self.assertTrue(any(issue["code"] == "delivery_overdue" for issue in inspection["issues"]))

        replacement = {"assets": [{"asset_id": "new", "brand_id": "new", "asset_name": "New Asset"}],
                       "expected_tasks": [{"task_key": "new|Gemini|2026-11-22", "asset_id": "new", "platform": "Gemini", "business_date": "2026-11-22"}]}
        replaced = self.client.post("/api/v1/seed/import?mode=replace", json=replacement)
        summary = self.client.get("/api/v1/overview").json()
        dataset = self.client.get("/api/v1/dataset").json()
        self.assertEqual(replaced.json()["mode"], "replace")
        self.assertEqual(summary["expected"], 1)
        self.assertEqual(summary["assets"][0]["asset_id"], "new")
        self.assertEqual(dataset["latest_import"]["mode"], "replace")

    def test_mock_only_checks_current_asset_and_parent_brand(self):
        assets = [
            {"asset_id": "b1", "asset_type": "brand", "brand_id": "b1", "brand_name": "Northwind"},
            {"asset_id": "a1", "asset_type": "product", "brand_id": "b1", "asset_name": "Northwind Camera"},
            {"asset_id": "b2", "asset_type": "brand", "brand_id": "b2", "brand_name": "Contoso"},
        ]
        record = {"asset_id": "a1", "raw_answer": "Contoso is mentioned, but the target asset is not."}
        analysis = api.mock_analysis(record, assets, "a1")
        self.assertFalse(analysis["brand_mentioned"])
        record["raw_answer"] = "Northwind Camera is a suitable option."
        self.assertTrue(api.mock_analysis(record, assets, "a1")["brand_mentioned"])

    def test_startup_resumes_queued_job(self):
        self.client.post("/api/v1/seed/import", json=self.seed)
        with closing(api.connect()) as db:
            generation = int(db.execute("SELECT value FROM app_meta WHERE key='dataset_generation'").fetchone()[0])
            run_id = "recovery-on-startup"
            db.execute("INSERT INTO runs(run_id,task_key,kind,status,mode,created_at,updated_at,dataset_generation) VALUES(?,?,?,?,?,?,?,?)",
                       (run_id, "a1|ChatGPT|2026-11-20", "reanalyze", "queued", "mock", api.now_iso(), api.now_iso(), generation))
            db.commit()

        async def start_and_wait():
            await api.resume_persisted_jobs()
            for _ in range(30):
                await asyncio.sleep(0.05)
                with closing(api.connect()) as db:
                    row = db.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
                    if row["status"] != "queued" and row["status"] != "running":
                        return row["status"]
            return "timeout"

        self.assertEqual(asyncio.run(start_and_wait()), "succeeded")


if __name__ == "__main__":
    unittest.main()
