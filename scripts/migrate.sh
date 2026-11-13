#!/usr/bin/env bash
# 上线切换脚本：在依赖安装与构建通过后执行。
#
# 做了什么：
#   1. 预检 AUDIT_DATA_DIR 可写；若已存在事件日志则只做续跑，不覆盖。
#   2. 执行/续跑审计记录迁移；失败时保留原数据快照与检查点，打印失败区间并退出非零。
#   3. 迁移完成后回放校验，确认记录数与旧环境一致，再标记切换完成。
#
# 环境变量：
#   AUDIT_DATA_DIR  事件日志/检查点/快照目录（容器内默认 /srv/data/audit）
#   LEGACY_EXPORT   可选，旧数据库导出的 JSON 文件路径
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
PYTHON="${PYTHON:-$BACKEND_DIR/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="python3"

export AUDIT_DATA_DIR="${AUDIT_DATA_DIR:-$BACKEND_DIR/data}"
mkdir -p "$AUDIT_DATA_DIR"

echo "[migrate] 数据目录：$AUDIT_DATA_DIR"
echo "[migrate] 旧数据快照将保存为：$AUDIT_DATA_DIR/legacy-snapshot.json"

cd "$BACKEND_DIR"
if [ -n "${LEGACY_EXPORT:-}" ]; then
  echo "[migrate] 从旧库导出文件迁移：$LEGACY_EXPORT"
  "$PYTHON" -m app.persistence.cli migrate --legacy "$LEGACY_EXPORT"
else
  "$PYTHON" -m app.persistence.cli migrate
fi

echo "[migrate] 迁移状态："
"$PYTHON" -m app.persistence.cli status

# 失败区间非空则视为切换失败，交由流水线触发回退
if "$PYTHON" - <<'PY'
import json, os, sys
from app.config import settings
state = os.path.join(settings.audit_data_dir, "migration-state.json")
with open(state, encoding="utf-8") as fh:
    data = json.load(fh)
if data.get("failures"):
    print("[migrate] 存在失败区间：", json.dumps(data["failures"], ensure_ascii=False))
    sys.exit(1)
if data.get("status") != "completed":
    print("[migrate] 迁移未完成，当前状态：", data.get("status"))
    sys.exit(1)
PY
then
  echo "[migrate] 迁移完成，事件日志已可重启恢复"
else
  echo "[migrate] 迁移存在失败区间，原数据已保留，请修复后重跑本脚本续跑" >&2
  exit 1
fi
