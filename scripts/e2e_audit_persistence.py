#!/usr/bin/env python3
"""端到端验证：审计记录追加式持久化 + 重启恢复 + 配置版本联动。

流程：
  1. 用临时数据目录启动 uvicorn（第一轮进程），等待健康检查。
  2. 校验 AUDI-0001~0003 迁移后的操作类型、结果摘要与复核/归档标记。
  3. 对 AUDI-0001 执行复核并回写 VERS-0002，再追加一条修正事件。
  4. 停掉进程，用同一数据目录再次启动（第二轮进程，模拟重启）。
  5. 校验记录、状态、事件顺序、版本待核对/回写视图、迁移状态全部可恢复。

仅依赖标准库；数据目录与本脚本分离，通过环境变量传入。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
BASE = "http://127.0.0.1:8765"


def call(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def wait_healthy(proc: subprocess.Popen) -> None:
    for _ in range(50):
        if proc.poll() is not None:
            raise SystemExit("后端进程提前退出")
        try:
            if call("GET", "/api/health").get("ok"):
                return
        except OSError:
            time.sleep(0.2)
    raise SystemExit("后端健康检查超时")


def start(data_dir: str) -> subprocess.Popen:
    env = dict(os.environ, AUDIT_DATA_DIR=data_dir, APP_ENV="e2e")
    proc = subprocess.Popen(
        [".venv/bin/uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8765"],
        cwd=BACKEND_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    wait_healthy(proc)
    return proc


def stop(proc: subprocess.Popen) -> str:
    proc.send_signal(signal.SIGINT)
    out, _ = proc.communicate(timeout=15)
    return out


def main() -> int:
    with tempfile.TemporaryDirectory() as data_dir:
        # ---- 第一轮进程：迁移 + 运行期写入 ----
        proc = start(data_dir)
        try:
            listing = call("GET", "/api/audit?size=100")
            assert listing["total"] == 3, listing
            by_no = {r["记录编号"]: r for r in listing["items"]}
            assert by_no["AUDI-0001"]["操作类型"] == "采集审计日志样例1"
            assert by_no["AUDI-0001"]["status"] == "已记录"
            assert by_no["AUDI-0002"]["status"] == "已复核", by_no["AUDI-0002"]
            assert by_no["AUDI-0003"]["status"] == "已归档", by_no["AUDI-0003"]
            assert by_no["AUDI-0003"]["结果摘要"] == "采集审计日志样例3"
            print("[第一轮] AUDI-0001~0003 迁移保真与复核/归档标记：OK")

            # 复核 AUDI-0001 并回写到 VERS-0002
            res = call("POST", "/api/audit/1/actions",
                       {"values": {"action": "标记复核", "版本编号": "VERS-0002"}})
            assert res["ok"] and res["entry"]["status"] == "已复核", res
            # 追加修正
            res = call("POST", "/api/audit/1/amend",
                       {"values": {"结果摘要": "E2E 追加修正摘要"}, "remark": "e2e"})
            assert res["ok"], res
            # 同编号重复登记：首写为准，拒绝
            res = call("POST", "/api/audit",
                       {"values": {"记录编号": "AUDI-0001", "操作类型": "x", "采集计划": "y"}})
            assert not res["ok"], res
            print("[第一轮] 复核回写、追加修正、首写冲突拦截：OK")

            view = call("GET", "/api/version/check-items?version_no=VERS-0002")
            writes = view["items"][0]["审计回写"]
            assert any(w["记录编号"] == "AUDI-0001" for w in writes), view
            print("[第一轮] 配置版本待核对/回写视图：OK")

            events = call("GET", "/api/audit/1/events")
            seq = [e["seq"] for e in events["items"]]
            assert seq == sorted(seq) and len(seq) >= 4, events
            print("[第一轮] 追加事件按 seq 稳定排序：OK")

            migration = call("GET", "/api/audit/migration-status")
            assert migration["状态"] == "completed", migration
            print("[第一轮] 迁移状态：", migration["状态"])
        finally:
            log_out = stop(proc)

        # 事件日志与快照确实落盘
        files = sorted(os.listdir(data_dir))
        assert "audit-events.jsonl" in files and "legacy-snapshot.json" in files, files
        assert "migration-state.json" in files, files
        print("[第一轮] 持久化文件落盘：", files)

        # ---- 第二轮进程：同一数据目录重启，纯回放恢复 ----
        proc2 = start(data_dir)
        try:
            listing = call("GET", "/api/audit?size=100")
            by_no = {r["记录编号"]: r for r in listing["items"]}
            assert by_no["AUDI-0001"]["status"] == "已复核"
            assert by_no["AUDI-0001"]["结果摘要"] == "E2E 追加修正摘要"
            assert by_no["AUDI-0002"]["status"] == "已复核"
            assert by_no["AUDI-0003"]["status"] == "已归档"
            assert by_no["AUDI-0001"]["操作类型"] == "采集审计日志样例1"
            print("[第二轮] 重启后操作类型/摘要/复核归档标记完整恢复：OK")

            events = call("GET", "/api/audit/1/events")
            types = [e["type"] for e in events["items"]]
            assert types.count("audit.recorded") == 1, types  # 重放无副本
            assert "audit.amended" in types and "audit.reviewed" in types
            print("[第二轮] 重放无副本、事件序列稳定：OK")

            view = call("GET", "/api/version/check-items?version_no=VERS-0002")
            writes = view["items"][0]["审计回写"]
            assert any(w["记录编号"] == "AUDI-0001" for w in writes), view
            print("[第二轮] 版本回写视图重启可回看：OK")

            # 列表/详情/导出旧入口全部可用
            detail = call("GET", "/api/audit/3")
            assert detail["记录编号"] == "AUDI-0003"
            export = call("GET", "/api/audit/export")
            assert export["total"] == 3
            print("[第二轮] 列表/详情/导出旧入口可用：OK")
        finally:
            stop(proc2)

    print("端到端验证全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
