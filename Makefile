.PHONY: install backend frontend test migrate rollback

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

# 后端测试：覆盖追加日志、迁移幂等、失败区间续跑、首写仲裁与版本联动
test:
	cd backend && .venv/bin/python -m unittest discover -s tests -v

# 执行/续跑审计记录迁移（可通过 LEGACY_EXPORT 指定旧库导出文件）
migrate:
	cd backend && ../scripts/migrate.sh

# 回退：保留事件日志与旧数据快照，复制一份带时间戳的备份
rollback:
	cd backend && ../scripts/rollback.sh
