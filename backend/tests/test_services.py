"""服务层与启动引导测试：验证旧入口在持久化底座上行为不变。"""
from __future__ import annotations

import tempfile
import unittest

from app.persistence import bootstrap
from app.seed import SEED_ROWS
from app.services.audit import AuditService
from app.services.version import VersionService
from app.store import store


def _reset_store() -> None:
    """把内存仓库恢复成出厂示例数据，避免用例间相互污染。"""
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}


class AuditServicePersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name
        _reset_store()
        # 每个用例重置引导单例，保证数据目录隔离
        bootstrap._audit_repo = None
        bootstrap._migration = None
        status = bootstrap.bootstrap(
            data_dir=self.data_dir,
            legacy_rows=[dict(r) for r in store.rows("audit")],
        )
        self.assertEqual(status["状态"], "completed")
        self.service = AuditService()

    def tearDown(self) -> None:
        bootstrap._audit_repo = None
        bootstrap._migration = None
        self._tmp.cleanup()
        _reset_store()

    def test_seed_records_migrated_with_same_fields_and_marks(self):
        items, total = self.service.list_entries(page=1, size=20)
        self.assertEqual(total, 3)
        by_no = {row["记录编号"]: row for row in items}
        self.assertEqual(by_no["AUDI-0001"]["操作类型"], "采集审计日志样例1")
        self.assertEqual(by_no["AUDI-0002"]["status"], "已复核")
        self.assertEqual(by_no["AUDI-0003"]["status"], "已归档")
        # 详情入口可用
        detail = self.service.get_entry(3)
        self.assertEqual(detail["记录编号"], "AUDI-0003")

    def test_review_and_archive_actions_still_work(self):
        entry, message = self.service.run_action(1, "标记复核")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "已复核")
        entry, _ = self.service.run_action(1, "归档记录")
        self.assertEqual(entry["status"], "已归档")
        # 非法动作仍被拦截
        entry, message = self.service.run_action(2, "删除记录")
        self.assertIsNone(entry)
        self.assertIn("不属于", message)

    def test_restart_restores_actions_and_keeps_list_contract(self):
        self.service.run_action(1, "标记复核")
        # 重新引导：新进程视角
        bootstrap._audit_repo = None
        bootstrap._migration = None
        bootstrap.bootstrap(data_dir=self.data_dir, legacy_rows=[])
        service = AuditService()
        items, total = service.list_entries(page=1, size=20)
        self.assertEqual(total, 3)
        by_no = {row["记录编号"]: row for row in items}
        self.assertEqual(by_no["AUDI-0001"]["status"], "已复核")
        self.assertEqual(by_no["AUDI-0003"]["status"], "已归档")

    def test_duplicate_create_is_rejected_first_write_wins(self):
        entry, missing = self.service.create_entry(
            {"记录编号": "AUDI-0001", "操作类型": "试图覆盖", "采集计划": "P"}
        )
        self.assertIsNone(entry)
        self.assertTrue(missing)
        items, _ = self.service.list_entries(page=1, size=20)
        by_no = {row["记录编号"]: row for row in items}
        self.assertEqual(by_no["AUDI-0001"]["操作类型"], "采集审计日志样例1")

    def test_amend_creates_new_append_event(self):
        entry, message = self.service.amend_entry(1, {"结果摘要": "补充摘要"}, note="人工补录")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["结果摘要"], "补充摘要")
        events, _ = self.service.get_events(1)
        self.assertEqual(events[-1]["type"], "audit.amended")

    def test_version_check_items_and_writeback(self):
        version_service = VersionService()
        view = version_service.check_items()
        self.assertGreaterEqual(view["待核对总数"], 0)
        # 对 AUDI-0001 复核并显式回写到 VERS-0001
        entry, _ = self.service.run_action_with_version(1, "标记复核", "VERS-0001")
        self.assertEqual(entry["status"], "已复核")
        view = version_service.check_items("VERS-0001")
        item = view["items"][0]
        self.assertTrue(any(w["记录编号"] == "AUDI-0001" for w in item["审计回写"]))
        # store 同步：版本行上也能看到回写与待核对数量
        version_row = store.find("version", 1)
        self.assertIn("审计回写", version_row)
        self.assertIn("待核对数量", version_row)

    def test_store_audit_rows_replaced_by_repository_view(self):
        # 兼容层 Store 的 audit 表内容来自权威仓储，概览统计口径保持可用
        audit_rows = store.rows("audit")
        self.assertEqual(len(audit_rows), 3)
        overview = store.overview()
        audit_module = next(m for m in overview["modules"] if m["name"] == "audit")
        self.assertEqual(audit_module["created"], 3)
