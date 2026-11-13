"""事件日志与仓储折叠测试：持久化、幂等、首写为准、稳定排序。"""
from __future__ import annotations

from app.persistence.repository import ConflictError
from tests.support import PersistenceTestCase


class EventLogTest(PersistenceTestCase):
    def test_append_is_persisted_and_replayed_after_restart(self):
        self.repo.record(
            {
                "记录编号": "AUDI-0001",
                "操作类型": "计划下发",
                "采集计划": "PLAN-A",
                "执行人": "张三",
                "执行时间": "2026-09-01",
                "结果摘要": "首轮采集完成",
            }
        )
        repo = self.reopen()
        record = repo.get_by_record_no("AUDI-0001")
        self.assertIsNotNone(record)
        self.assertEqual(record["操作类型"], "计划下发")
        self.assertEqual(record["status"], "已记录")

    def test_idempotent_key_does_not_grow_duplicate(self):
        payload = {"记录编号": "AUDI-0001", "操作类型": "a", "采集计划": "b"}
        first, _, c1 = self.repo.record(payload, idem_key="k:1")
        self.assertFalse(c1)
        # 同幂等键重放：返回同一事件，记录数不增加
        again, _, c2 = self.repo.record(payload, idem_key="k:1", allow_conflict=True)
        self.assertTrue(c2)
        self.assertEqual(len(self.repo.all_records()), 1)
        self.assertEqual(first["id"], again["id"])

    def test_duplicate_record_no_raises_conflict_and_first_write_wins(self):
        self.repo.record({"记录编号": "AUDI-0001", "操作类型": "首写", "采集计划": "P"})
        with self.assertRaises(ConflictError):
            self.repo.record({"记录编号": "AUDI-0001", "操作类型": "后写", "采集计划": "P"})
        record = self.repo.get_by_record_no("AUDI-0001")
        self.assertEqual(record["操作类型"], "首写")

    def test_amend_appends_without_overwriting_history(self):
        self.repo.record({"记录编号": "AUDI-0001", "操作类型": "旧值", "采集计划": "P"})
        entry = self.repo.get_by_record_no("AUDI-0001")
        updated, message = self.repo.amend(int(entry["id"]), {"结果摘要": "修正后的摘要"}, note="补录")
        self.assertIn("追加修正", message)
        self.assertEqual(updated["结果摘要"], "修正后的摘要")
        # 事件流里登记与修正各一条，按 seq 稳定排序
        events = self.repo.events_for("AUDI-0001")
        self.assertEqual([e["type"] for e in events], ["audit.recorded", "audit.amended"])
        self.assertLess(events[0]["seq"], events[1]["seq"])
        # 状态字段不允许通过修正改动
        _, bad = self.repo.amend(int(entry["id"]), {"status": "已归档"})
        self.assertIn("没有可修正的字段", bad)

    def test_review_archive_flow_and_restart_keeps_marks(self):
        self.repo.record({"记录编号": "AUDI-0002", "操作类型": "复核演练", "采集计划": "P"})
        entry = self.repo.get_by_record_no("AUDI-0002")
        self.repo.review(int(entry["id"]), conclusion="复核通过")
        self.repo.archive(int(entry["id"]))
        repo = self.reopen()
        record = repo.get_by_record_no("AUDI-0002")
        self.assertEqual(record["status"], "已归档")
        self.assertFalse(record["pending"])
        self.assertEqual(record["复核状态"], "复核通过")
        events = repo.events_for("AUDI-0002")
        self.assertEqual(
            [e["type"] for e in events],
            ["audit.recorded", "audit.reviewed", "audit.archived"],
        )

    def test_records_sorted_stably_by_record_no_order(self):
        for no in ["AUDI-0003", "AUDI-0001", "AUDI-0002"]:
            self.repo.record({"记录编号": no, "操作类型": "t", "采集计划": "p"})
        rows, total = self.repo.list_records(page=1, size=20)
        self.assertEqual(total, 3)
        self.assertEqual([r["记录编号"] for r in rows], ["AUDI-0003", "AUDI-0001", "AUDI-0002"])

    def test_corrupted_tail_is_truncated_but_history_kept(self):
        self.repo.record({"记录编号": "AUDI-0001", "操作类型": "a", "采集计划": "b"})
        path = f"{self.data_dir}/audit-events.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"seq": 2, "type": "broken", "pa')  # 模拟半截写入
        repo = self.reopen()
        self.assertEqual(len(repo.all_records()), 1)
        # 截断后新追加仍可继续，且 seq 不与历史冲突
        repo.record({"记录编号": "AUDI-0002", "操作类型": "c", "采集计划": "d"})
        self.assertEqual(len(repo.all_records()), 2)
