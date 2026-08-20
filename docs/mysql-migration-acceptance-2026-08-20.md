# SQLite → MySQL 本地迁移验收报告（2026-08-20）

## 1. 验收结论

本地开发库 `travel_agent` 已完成当前 SQLite 一致性快照的历史数据迁移和逐行验证：

```text
Alembic revision: a31f0c8d4b72
迁移批次: c9ad9067-4e48-46c9-af19-8410e2942c57
源记录: 399
插入记录: 399
冲突: 0
缺失: 0
逐行匹配: 399
verified: true
safe_to_cutover: true
```

当前只完成“历史快照迁移验收”，没有修改本地 `.env` 的 `DATABASE_BACKEND`。正式切换前仍应停止 API/Worker 写入，并对最终 SQLite 快照再执行一次 execute + verify。

## 2. 开发库迁移明细

| 表 | SQLite 源记录 | MySQL 插入 | Verify 匹配 |
|---|---:|---:|---:|
| `agent_sessions` | 54 | 54 | 54 |
| `route_cache` | 276 | 276 | 276 |
| `restaurant_cache` | 53 | 53 | 53 |
| `trip_plan_versions` | 0 | 0 | 0 |
| `trip_drafts` | 0 | 0 | 0 |
| `trip_planning_tasks` | 1 | 1 | 1 |
| `trip_task_events` | 15 | 15 | 15 |
| **总计** | **399** | **399** | **399** |

迁移使用 SQLite Online Backup API 生成的不可变快照，快照和 manifest 位于被 Git 忽略的 `data/backups/`。

## 3. 测试库回滚演练

在 `travel_agent_test` 对同一快照完成了一次完整演练：

1. execute 插入 399 条；
2. verify 匹配 399 条；
3. rollback 跟踪并删除 399 条；
4. `protected_modified=0`；
5. 回滚后 dry-run 再次确认 399 条全部处于目标缺失状态。

测试批次：

```text
7259df9e-0dfe-49a2-bbee-d9fbf6d43aab
```

这验证了 MySQL 环境中的插入、逐行摘要校验、外键逆序回滚和批次审计链路。

## 4. 质量门

```text
Python 单元与 MySQL 集成测试：305/305 通过
Orchestrator 故障安全：14/14 通过
固定端到端验收：15/15 通过
Alembic check（开发库）：通过
Alembic check（测试库）：通过
MySQL Schema 校验（开发库）：通过
MySQL Schema 校验（测试库）：通过
```

## 5. 正式切换前待办

1. 为运行时 MySQL 最小权限账号配置本地凭据；
2. 停止 API 和 Worker，形成短暂的 SQLite 停写窗口；
3. 对最终 SQLite 快照再执行 execute 和 verify；
4. 确认 `safe_to_cutover=true`；
5. 将 `DATABASE_BACKEND` 改为 `mysql`；
6. 重启并验证历史会话、异步任务、草稿版本、结果页和 SSE；
7. 保留 SQLite 原库和迁移快照，暂不删除。
