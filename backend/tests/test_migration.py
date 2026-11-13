"""迁移器测试：保真、幂等、失败区间、断点续跑与并发首写仲裁。"""
from __future__ import annotations

import json
import os

from app.persistence.eventlog import AppendOnlyLog
from app.persistence.migration import AuditMigration
from app.persistence.repository import AuditRepository
from tests.support import PersistenceTestCase


class MigrationTest(PersistenceTestCase):
    def _migration(self) -> AuditMigration:
        return AuditMigration(self.data_dir, self.log, self.repo)

    def test_migrate_keeps_history_fields_and_status_marks(self):
        rows = self.seed_three()
        status = self._migration().run(rows, version_resolver=lambda _r: "VERS-0003")
        self.assertEqual(status["状态"], "completed")
        self.assertEqual(status["已迁移"], 3)

        records = {r["记录编号"]: r for r in self.repo.all_records()}
        for idx, no in enumerate(["AUDI-0001", "AUDI-0002", "AUDI-0003"], start=1):
            record = records[no]
            self.assertEqual(record["操作类型"], f"操作类型{idx}")
            self.assertEqual(record["采集计划"], f"采集计划{idx}")
            self.assertEqual(record["执行人"], f"执行人{idx}")
            self.assertEqual(record["执行时间"], f"2026-09-0{idx}")
            self.assertEqual(record["结果摘要"], f"结果摘要{idx}")
        self.assertEqual(records["AUDI-0001"]["status"], "已记录")
        self.assertEqual(records["AUDI-0002"]["status"], "已复核")
        self.assertEqual(records["AUDI-0003"]["status"], "已归档")
        # 旧的复核/归档标记以事件形式保留（过滤掉版本联动事件后核对主链路）
        types = [e["type"] for e in self.repo.events_for("AUDI-0003")]
        self.assertEqual(
            [t for t in types if t.startswith("audit.")],
            ["audit.recorded", "audit.reviewed", "audit.archived"],
        )
        # 复核/归档还产生了配置版本回写事件
        self.assertIn("version.archive_written", types)

    def test_snapshot_preserves_original_data(self):
        rows = self.seed_three()
        self._migration().run(rows)
        with open(os.path.join(self.data_dir, "legacy-snapshot.json"), encoding="utf-8") as fh:
            snapshot = json.load(fh)
        self.assertEqual(len(snapshot["rows"]), 3)
        self.assertEqual(snapshot["rows"][1]["记录编号"], "AUDI-0002")

    def test_migration_is_idempotent_across_runs_and_restarts(self):
        rows = self.seed_three()
        self._migration().run(rows)
        # 再跑一次：幂等键命中，不产生副本
        again = self._migration().run(rows)
        self.assertEqual(again["状态"], "completed")
        self.assertEqual(len(self.repo.all_records()), 3)
        # 模拟重启后重新装配，记录与状态事件仍只有一份
        self.reopen()
        migration = AuditMigration(self.data_dir, self.log, self.repo)
        migration.run(rows)
        self.assertEqual(len(self.repo.all_records()), 3)
        for no in ["AUDI-0001", "AUDI-0002", "AUDI-0003"]:
            types = [e["type"] for e in self.repo.events_for(no)]
            self.assertEqual(types.count("audit.recorded"), 1)

    def test_failure_records_range_and_resumes_from_it(self):
        rows = self.seed_three()
        first = self._migration()
        status = first.run(rows, fail_at_index=1)  # 第二条失败
        self.assertEqual(status["状态"], "failed")
        self.assertEqual(status["已迁移"], 1)
        self.assertEqual(status["失败区间"][0]["起始下标"], 1)
        self.assertEqual(status["失败区间"][0]["结束下标"], 1)
        # 原数据仍在快照中
        self.assertTrue(os.path.exists(os.path.join(self.data_dir, "legacy-snapshot.json")))

        # 重新打开（模拟服务恢复），从失败区间继续
        self.log = AppendOnlyLog(f"{self.data_dir}/audit-events.jsonl")
        self.repo = AuditRepository(self.log)
        resumed = AuditMigration(self.data_dir, self.log, self.repo)
        status2 = resumed.run(rows)
        self.assertEqual(status2["状态"], "completed")
        self.assertEqual(status2["已迁移"], 3)
        self.assertEqual(status2["失败区间"], [])
        self.assertEqual(len(self.repo.all_records()), 3)

    def test_runtime_write_wins_and_legacy_value_left_as_amend_event(self):
        rows = self.seed_three()
        # 运行期先写入 AUDI-0002（与迁移并行的新写入）
        self.repo.record(
            {"记录编号": "AUDI-0002", "操作类型": "运行期首写", "采集计划": "PLAN-RT"},
            idem_key="runtime:AUDI-0002",
        )
        self._migration().run(rows)
        record = self.repo.get_by_record_no("AUDI-0002")
        # 首写为准
        self.assertEqual(record["操作类型"], "运行期首写")
        self.assertEqual(record["采集计划"], "PLAN-RT")
        events = self.repo.events_for("AUDI-0002")
        # 迁移登记被首写拦截，差异以 amended 留痕；状态事件仍补齐
        types = [e["type"] for e in events]
        self.assertIn("audit.amended", types)
        amended = next(e for e in events if e["type"] == "audit.amended")
        self.assertEqual(amended["payload"]["changes"]["操作类型"], "操作类型2")
        # 重跑迁移不会重复留痕
        self._migration().run(rows)
        self.assertEqual(
            [e["type"] for e in self.repo.events_for("AUDI-0002")].count("audit.amended"),
            1,
        )

    def test_missing_fields_are_filled_with_trace(self):
        rows = self.seed_three()
        rows[0]["执行人"] = ""
        rows[0]["结果摘要"] = ""
        self._migration().run(rows)
        record = self.repo.get_by_record_no("AUDI-0001")
        self.assertEqual(record["执行人"], "")
        self.assertIn("执行人", record["补齐字段"])
        self.assertIn("结果摘要", record["补齐字段"])
        self.assertEqual(record["迁移来源"], "legacy-memory")

    def test_version_check_and_writeback_folded_from_events(self):
        rows = self.seed_three()
        self._migration().run(rows, version_resolver=lambda _r: "VERS-0003")
        view = self.repo.version_check_items("VERS-0003")
        item = view["items"][0]
        # 复核、归档都产生回写，因此没有遗留待核对项
        self.assertEqual(item["回写数量"], 3)  # AUDI-0002 复核 + AUDI-0003 复核与归档
        record_nos = {w["记录编号"] for w in item["审计回写"]}
        self.assertEqual(record_nos, {"AUDI-0002", "AUDI-0003"})
