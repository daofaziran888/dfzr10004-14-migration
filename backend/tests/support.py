"""测试共用工具：在临时目录里搭建事件日志与审计仓储。"""
from __future__ import annotations

import tempfile
import unittest

from app.persistence.eventlog import AppendOnlyLog
from app.persistence.repository import AuditRepository


class PersistenceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例独立临时目录，避免事件日志与检查点互相污染
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name
        self.log = AppendOnlyLog(f"{self.data_dir}/audit-events.jsonl")
        self.repo = AuditRepository(self.log)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def reopen(self) -> AuditRepository:
        """模拟服务重启：重新打开日志并回放，仓储状态应完整恢复。"""
        self.log = AppendOnlyLog(f"{self.data_dir}/audit-events.jsonl")
        self.repo = AuditRepository(self.log)
        return self.repo

    def seed_three(self) -> list[dict]:
        """构造与 AUDI-0001~0003 同形态的三条记录。"""
        rows = []
        for idx, status in enumerate(["已记录", "已复核", "已归档"], start=1):
            rows.append(
                {
                    "id": idx,
                    "status": status,
                    "记录编号": f"AUDI-{idx:04d}",
                    "操作类型": f"操作类型{idx}",
                    "采集计划": f"采集计划{idx}",
                    "执行人": f"执行人{idx}",
                    "执行时间": f"2026-09-0{idx}",
                    "结果摘要": f"结果摘要{idx}",
                    "变更内容": f"变更内容{idx}",
                    "复核状态": f"复核状态{idx}",
                }
            )
        return rows
