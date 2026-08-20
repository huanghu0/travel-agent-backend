# MySQL 正式切换与运行后端验收报告（2026-08-20）

## 1. 验收结论

本地 SQLite → MySQL 正式切换的数据、Schema、Store 和运行链路已通过验收：

```text
DATABASE_BACKEND=mysql
MySQL: 8.4.10
Alembic revision: a31f0c8d4b72
最终迁移批次: c673ab47-1597-4c8d-8ab0-871d15f07235
最终快照记录: 399
逐行匹配: 399
冲突: 0
缺失: 0
verified: true
safe_to_cutover: true
```

本地 `.env` 已切换为 MySQL。SQLite 原库、最终一致性快照和 manifest 均保留，未删除或覆盖。

当前有一个需要用户完成的运行操作：原先由 IDE/宿主环境启动并占用 `8000` 端口的旧进程不受当前 Codex 执行环境管理，因此它仍持有启动时加载的 SQLite 配置。请在 IDE/原终端中停止并重新启动该进程；重启后会读取当前 `.env` 的 MySQL 配置。MySQL 新进程已在隔离的 `8001` 端口完成全套运行验收。

## 2. 最终迁移

最终迁移使用 SQLite Online Backup API 创建不可变快照：

```text
data/backups/agent_memory-20260820T091938Z.db
```

迁移结果：

| 表 | 源记录 | 新插入 | 已存在且一致 | Verify 匹配 |
|---|---:|---:|---:|---:|
| `agent_sessions` | 54 | 0 | 54 | 54 |
| `route_cache` | 276 | 0 | 276 | 276 |
| `restaurant_cache` | 53 | 0 | 53 | 53 |
| `trip_plan_versions` | 0 | 0 | 0 | 0 |
| `trip_drafts` | 0 | 0 | 0 | 0 |
| `trip_planning_tasks` | 1 | 0 | 1 | 1 |
| `trip_task_events` | 15 | 0 | 15 | 15 |
| **总计** | **399** | **0** | **399** | **399** |

本批次没有覆盖 MySQL 已有数据。最终快照与此前已迁移数据完全一致，因此 execute 全部按幂等规则跳过，verify 再按完整行摘要确认 399/399 一致。

结构化报告保存在被 Git 忽略的：

```text
build/reports/sqlite-mysql-cutover-execute.json
build/reports/sqlite-mysql-cutover-verify.json
```

## 3. 最小权限应用账号

新增安全配置脚本：

```powershell
python scripts/configure_mysql_app_user.py
```

安全边界：

- MySQL 管理员密码只通过交互式隐藏输入读取；
- 不接受命令行密码参数；
- 不把管理员密码或应用密码输出到控制台和报告；
- 为本地应用账号生成高熵密码，仅写入被 Git 忽略的 `.env.local`；
- 仅授予开发库和测试库的 `SELECT、INSERT、UPDATE、DELETE`；
- 同时验证新账号可以连接目标开发库。

数据库创建和 Alembic DDL 仍应使用单独的迁移/管理员身份，运行时账号不承担 DDL 权限。

## 4. 运行后端验收

使用 MySQL 配置启动 API 和内置 Worker 后，完成以下检查：

| 检查项 | 结果 |
|---|---|
| `/api/health` | 通过 |
| 历史会话列表 | 通过，能读取迁移后的会话 |
| 会话详情 | 通过 |
| `execution-view` | 通过，能返回路线分段和日程数据 |
| 历史异步任务查询 | 通过 |
| 创建任务短时间返回 HTTP 202 | 通过 |
| 相同 `Idempotency-Key` 重试复用同一任务 | 通过 |
| 等待中任务取消 | 通过 |
| SSE 从事件 0 回放 | 通过，按事件 ID 返回 queued/cancelled |
| 服务重启后任务仍可查询 | 通过 |
| Worker 不领取已取消任务 | 通过，任务 attempt 仍为 0 |
| MySQL/SQLite 写入隔离 | 通过，新验收任务只存在 MySQL |

验收创建了一条已取消的脱敏任务，仅用于证明运行进程真实使用 MySQL；该任务没有调用高德或 LLM。

## 5. 质量门

```text
完整确定性质量门：通过
Python unittest：310/310 通过（其中默认跳过 9 个显式 MySQL Live 用例）
MySQL Live Schema + Store 并发用例：9/9 通过
Orchestrator 故障安全：14/14 通过
固定端到端 synthetic 基线：15/15 通过
Alembic current：a31f0c8d4b72 (head)
Alembic check：No new upgrade operations detected
MySQL Schema 校验：通过
```

MySQL Live 测试覆盖：

- Schema 与 SQLAlchemy 元数据一致；
- 五类 Store 基本契约；
- 缓存 TTL；
- 版本确认；
- 任务和事件原子提交；
- 同键和不同键并发幂等；
- 多 Worker 排他领取；
- 过期租约恢复；
- 取消阻止领取和旧 Worker 拒绝。

## 6. 切换后的运行步骤

1. 在 IDE 或原启动终端停止当前 `8000` 端口旧进程；
2. 确认 `.env` 中 `DATABASE_BACKEND=mysql`，并确认 `.env.local` 存在；
3. 从项目虚拟环境重新启动 API；
4. 查询一个只存在于 MySQL 的任务或新建并取消一个验收任务；
5. 确认前端历史会话、结果页、草稿版本和异步进度正常；
6. 保留 `data/agent_memory.db` 和 `data/backups/`，暂不删除；
7. 若需紧急回滚，先停止写入并评估 MySQL 切换后新增数据，不能直接把后端改回 SQLite 而忽略增量数据。

## 7. 下一阶段

下一阶段进入 Redis：

1. Redis 任务唤醒，替代 Worker 高频轮询；
2. Redis Pub/Sub 或 Stream 推送进度，MySQL SSE 事件表继续作为可回放事实来源；
3. 取消信号快速广播；
4. 短 TTL 热点缓存；
5. 分布式限流和协调锁；
6. Redis 故障时自动降级到 MySQL 轮询，不丢任务、不丢终态。
