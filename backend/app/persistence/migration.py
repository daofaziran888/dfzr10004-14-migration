"""旧内存数据 -> 追加式事件日志的迁移器。

迁移安全约定：

1. 迁移前先把旧数据原样快照到数据目录（``legacy-snapshot.json``），
   任何时刻回退都能读取原环境记录，原数据不会被删除或改写。
2. 进度写入检查点文件（``migration-state.json``），每条记录处理完即落盘；
   失败时记录失败区间（记录编号 + 在旧列表中的下标范围），重启后从该区间
   继续，已完成区间不会重复迁移。
3. 所有迁移事件带稳定幂等键，重跑不产生副本；与运行期新写入撞编号时
   以首次成功写入为准，旧值差异通过 ``audit.amended`` 追加留痕。
4. 迁移不阻断启动：失败时保留原数据与已迁移前缀，状态可通过接口/CLI 查询。
"""
from __future__ import annotations

import json
import os
from typing import Any

from app.persistence.eventlog import AppendOnlyLog, utc_now_iso
from app.persistence.repository import AUDIT_FIELDS, AuditRepository

MIGRATION_SOURCE = "legacy-memory"
STATE_FILENAME = "migration-state.json"
SNAPSHOT_FILENAME = "legacy-snapshot.json"


class MigrationError(RuntimeError):
    """迁移过程中出现可记录的失败（测试时可注入）。"""


class AuditMigration:
    def __init__(self, data_dir: str, log: AppendOnlyLog, repo: AuditRepository) -> None:
        self.data_dir = data_dir
        self.log = log
        self.repo = repo
        self.state_path = os.path.join(data_dir, STATE_FILENAME)
        self.snapshot_path = os.path.join(data_dir, SNAPSHOT_FILENAME)
        os.makedirs(data_dir, exist_ok=True)
        self.state = self._load_state()

    # ------------------------------------------------------------ 状态文件
    def _load_state(self) -> dict[str, Any]:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as fh:
                    state = json.load(fh)
                state.setdefault("done_indexes", [])
                state.setdefault("failures", [])
                return state
            except (ValueError, OSError):
                # 状态文件自身损坏：不猜进度，从头按幂等键重跑即可
                pass
        return {
            "status": "pending",
            "total": 0,
            "done_indexes": [],
            "failures": [],
            "started_at": None,
            "updated_at": None,
            "finished_at": None,
            "snapshot": None,
        }

    def _save_state(self) -> None:
        self.state["updated_at"] = utc_now_iso()
        tmp_path = self.state_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, self.state_path)

    def status(self) -> dict[str, Any]:
        done = len(self.state.get("done_indexes", []))
        total = int(self.state.get("total", 0))
        return {
            "状态": self.state.get("status", "pending"),
            "总量": total,
            "已迁移": done,
            "待迁移": max(total - done, 0),
            "失败区间": list(self.state.get("failures", [])),
            "快照文件": self.state.get("snapshot"),
            "开始时间": self.state.get("started_at"),
            "最近更新": self.state.get("updated_at"),
            "完成时间": self.state.get("finished_at"),
        }

    # ------------------------------------------------------------ 快照
    def _write_snapshot(self, legacy_rows: list[dict[str, Any]]) -> str:
        with open(self.snapshot_path, "w", encoding="utf-8") as fh:
            json.dump(
                {"captured_at": utc_now_iso(), "rows": legacy_rows},
                fh,
                ensure_ascii=False,
                indent=2,
            )
            fh.flush()
            os.fsync(fh.fileno())
        return self.snapshot_path

    # ------------------------------------------------------------ 主流程
    def run(
        self,
        legacy_rows: list[dict[str, Any]],
        *,
        version_resolver=None,
        fail_at_index: int | None = None,
    ) -> dict[str, Any]:
        """迁移旧审计记录；``fail_at_index`` 用于演练失败区间与断点续跑。

        ``version_resolver`` 给定记录时返回应回写的配置版本编号（可为空）。
        """
        rows = [dict(row) for row in legacy_rows]
        if self.state["status"] == "pending":
            self.state["status"] = "running"
            self.state["total"] = len(rows)
            self.state["started_at"] = utc_now_iso()
            if not os.path.exists(self.snapshot_path):
                self.state["snapshot"] = self._write_snapshot(rows)
            else:
                self.state["snapshot"] = self.snapshot_path
            self._save_state()

        done: set[int] = set(self.state["done_indexes"])
        for index, row in enumerate(rows):
            if index in done:
                continue
            record_no = str(row.get("记录编号", "")).strip()
            try:
                if fail_at_index is not None and index == fail_at_index:
                    raise MigrationError(f"注入失败：下标 {index}（{record_no or '无编号'}）写入中断")
                self._migrate_one(index, row, version_resolver)
            except Exception as exc:  # noqa: BLE001 - 失败要落状态而不是中断进程
                failure = {
                    "起始下标": index,
                    "结束下标": index,
                    "记录编号": record_no or f"index:{index}",
                    "原因": str(exc),
                    "时间": utc_now_iso(),
                }
                # 合并连续失败，形成可读的失败区间
                failures = self.state["failures"]
                if failures and failures[-1]["结束下标"] == index - 1:
                    failures[-1]["结束下标"] = index
                    failures[-1]["记录编号"] = f"{failures[-1]['记录编号']} ~ {failure['记录编号']}"
                    failures[-1]["原因"] = str(exc)
                    failures[-1]["时间"] = failure["时间"]
                else:
                    failures.append(failure)
                self.state["status"] = "failed"
                self._save_state()
                return self.status()
            done.add(index)
            self.state["done_indexes"] = sorted(done)
            self.state["failures"] = [f for f in self.state["failures"] if not (f["起始下标"] <= index <= f["结束下标"])]
            self._save_state()

        self.state["status"] = "completed"
        self.state["finished_at"] = utc_now_iso()
        self._save_state()
        return self.status()

    # ------------------------------------------------------------ 单条迁移
    def _migrate_one(self, index: int, row: dict[str, Any], version_resolver) -> None:
        record_no = str(row.get("记录编号", "")).strip()
        if not record_no:
            raise MigrationError(f"下标 {index} 的旧记录缺少记录编号，已保留在快照中待人工核对")

        filled = [f for f in AUDIT_FIELDS if not str(row.get(f) or "").strip() and f != "记录编号"]
        values = {f: row.get(f, "") for f in AUDIT_FIELDS}
        values["记录编号"] = record_no
        entry_id = int(row.get("id", index + 1))
        idem = f"legacy:record:{record_no}"

        record, _missing, conflict = self.repo.record(
            values,
            idem_key=idem,
            source=MIGRATION_SOURCE,
            filled_fields=filled,
            entry_id=entry_id,
            allow_conflict=True,
        )
        if conflict and record is not None:
            # 首写为准：不覆盖运行期数据，只把旧值差异作为追加事件留痕
            diff = {
                f: str(values.get(f, ""))
                for f in AUDIT_FIELDS
                if f != "记录编号"
                and str(values.get(f, "")).strip()
                and str(record.get(f, "")) != str(values.get(f, ""))
            }
            if diff:
                self.repo.append_amended_event(
                    record_no,
                    diff,
                    note="迁移发现旧值与首次写入不一致，旧值按追加事件留痕，不覆盖首写数据",
                    idem_key=f"legacy:conflict:{record_no}",
                )
            return

        # 旧的复核/归档标记不能蒸发：按原状态补发状态事件（同样幂等）。
        # 已处于目标状态（上次迁移中断在状态事件之后）时跳过，保证续跑不报错。
        legacy_status = str(row.get("status", "已记录"))
        target_version = version_resolver(row) if version_resolver else ""
        if legacy_status in ("已复核", "已归档"):
            current = self.repo.get_by_record_no(record_no)
            if current is not None and current["status"] == "已记录":
                self.repo.review(
                    int(current["id"]),
                    conclusion=str(row.get("复核状态") or "已复核"),
                    target_version=target_version,
                    idem_key=f"legacy:review:{record_no}",
                )
        if legacy_status == "已归档":
            current = self.repo.get_by_record_no(record_no)
            if current is not None and current["status"] == "已复核":
                self.repo.archive(
                    int(current["id"]),
                    target_version=target_version,
                    idem_key=f"legacy:archive:{record_no}",
                )
