#!/usr/bin/env bash
# 回退脚本：新版本切换失败时使用。
#
# 原则：不删除任何已持久化数据。
#   - 事件日志 audit-events.jsonl、检查点 migration-state.json、旧数据快照
#     legacy-snapshot.json 全部保留，回退后旧版本进程仍可读取原环境记录。
#   - 把当前数据目录整体复制到带时间戳的回退备份目录，便于事后核对。
#   - 打印快照路径与回放命令，运维可直接用旧版本读取原始记录。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
PYTHON="${PYTHON:-$BACKEND_DIR/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="python3"

export AUDIT_DATA_DIR="${AUDIT_DATA_DIR:-$BACKEND_DIR/data}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="${AUDIT_DATA_DIR%/}-rollback-$STAMP"

echo "[rollback] 保留数据目录：$AUDIT_DATA_DIR"
if [ -d "$AUDIT_DATA_DIR" ]; then
  cp -a "$AUDIT_DATA_DIR" "$BACKUP_DIR"
  echo "[rollback] 已复制回退备份：$BACKUP_DIR"
fi

echo "[rollback] 原环境记录快照：$AUDIT_DATA_DIR/legacy-snapshot.json"
echo "[rollback] 旧版本可直接读取该快照；事件日志保持原样，未做任何删除或改写"

cd "$BACKEND_DIR"
if [ -f "$AUDIT_DATA_DIR/audit-events.jsonl" ]; then
  echo "[rollback] 当前事件日志回放结果（供核对，不修改文件）："
  "$PYTHON" -m app.persistence.cli status || true
fi
echo "[rollback] 完成。修复问题后重跑 scripts/migrate.sh 即可从失败区间续跑。"
