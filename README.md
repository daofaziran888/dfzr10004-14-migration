# 智能采集质量管控平台

面向数据采集团队的质量管控与效率优化平台。覆盖采集任务编排、抓取结果校验、采集链路监控、数据格式转换、去重比对、异常重试、采集审计与合规管理的完整采集质控链路。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   ├── app/persistence/      追加式事件日志、审计仓储与旧数据迁移
│   ├── app/store.py          内存数据仓库与示例数据（审计表由持久化仓储同步）
│   ├── tests/                持久化与迁移单元测试
│   └── examples/             旧数据库导出的迁移示例数据
├── scripts/                  上线迁移（migrate.sh）与回退（rollback.sh）脚本
├── .github/workflows/        流水线：先构建测试，通过后才切换
├── .gitignore
└── docker-compose.yml        backend 挂载 audit-data 命名卷支持重启恢复
```

## 审计记录持久化与迁移

采集审计日志已从进程内存迁移为**追加式事件日志**（JSON Lines，仅标准库实现）：

- 数据目录由 `AUDIT_DATA_DIR` 指定（本地默认 `backend/data`，容器内 `/srv/data/audit`），
  包含 `audit-events.jsonl`（事件日志）、`migration-state.json`（检查点）、
  `legacy-snapshot.json`（旧数据快照）。
- 每条写入立即 `flush + fsync`，服务重启后回放事件即可恢复；同一记录编号的
  重放事件带稳定幂等键，不会长出副本，事件按 `seq` 稳定排序。
- **首写为准**：运行期新写入与迁移并行撞编号时，以首次成功写入的记录为准，
  旧值差异只生成新的 `audit.amended` 追加事件，不改写历史。
- 迁移失败会保留原数据并在检查点记录失败区间（记录编号 + 下标范围），
  恢复后重跑迁移命令即从该区间继续。
- 复核/归档结论会回写到配置版本清单（待核对项 + 审计回写视图），
  旧的已复核、已归档标记随事件持久化，重启不蒸发。

常用命令：

```bash
make test                       # 后端单元测试（含迁移幂等、失败续跑、版本联动）
make migrate                    # 执行/续跑迁移
LEGACY_EXPORT=backend/examples/legacy-audit-export.json make migrate
make rollback                   # 回退：保留快照与事件日志，另存时间戳备份
python -m app.persistence.cli status   # 查看迁移状态与失败区间
```


## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集任务编排 | `taskplan` | 采集计划 | 计划编号、目标站点、采集频率 |
| 采集结果校验 | `verify` | 校验规则 | 规则编号、校验对象、校验类型 |
| 采集链路监控 | `pipeline` | 链路节点 | 节点编号、节点名称、上游节点 |
| 采集格式转换 | `transform` | 转换模板 | 模板编号、输入格式、输出格式 |
| 采集去重比对 | `dedup` | 去重策略 | 策略编号、业务键列、排序字段 |
| 异常重试管理 | `retry` | 重试规则 | 规则编号、异常类型、重试上限 |
| 采集审计日志 | `audit` | 审计记录 | 记录编号、操作类型、采集计划 |
| 采集合规配置 | `compliance` | 合规规则 | 规则编号、站点域名、协议标识 |
| 代理池管理 | `proxy` | 代理节点 | 节点编号、代理地址、代理类型 |
| 采集存储策略 | `storage` | 存储配置 | 配置编号、存储路径、文件格式 |
| 采集告警中心 | `alert` | 告警规则 | 规则编号、监控指标、告警级别 |
| 采集统计报表 | `report` | 统计报表 | 报表编号、统计维度、统计周期 |
| 采集频率控制 | `throttle` | 频率策略 | 策略编号、目标站点、请求间隔 |
| 采集数据标注 | `tag` | 标注记录 | 标注编号、数据记录、标注类型 |
| 采集配置版本 | `version` | 配置版本 | 版本编号、配置名称、版本号 |
| 采集沙箱测试 | `sandbox` | 沙箱实例 | 实例编号、测试脚本、目标站点 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
