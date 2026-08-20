# 持久化抽象、SQLite 备份与 MySQL Store

## 1. 已完成阶段

阶段 0 到阶段 3 已完成：

1. 使用 SQLite Online Backup API 创建一致性备份和 SHA-256 manifest；
2. 将业务层改为五类数据库后端无关 Store 接口；
3. 建立 MySQL 七张业务表、Alembic 迁移、健康检查和 Schema 校验；
4. 实现五类 MySQL Store，并注册到统一工厂；
5. 保留 SQLite 实现作为默认后端、迁移来源和回滚通道。

## 2. SQLite 备份

```powershell
python scripts/backup_sqlite.py
```

默认从 `data/agent_memory.db` 备份到 `data/backups/`。脚本使用 SQLite Online Backup API，兼容 WAL，并执行完整性检查和 manifest 校验。`data/` 已被 Git 忽略。

## 3. 统一 Store 接口

接口位于 `app/persistence/interfaces.py`：

- `AgentStateStore`：检查点、会话列表、执行和质量基线；
- `TripVersionStore`：草稿、候选版本和确认版本；
- `TripTaskStore`：异步任务、租约、取消和 SSE 事件；
- `RouteCacheStore`：真实路线缓存；
- `RestaurantCacheStore`：餐饮候选快照缓存。

Orchestrator、TripDraftService、TripTaskWorker 和任务上下文只依赖 Protocol，不感知底层数据库。

## 4. Store 工厂

`app/persistence/factory.py` 支持：

```env
DATABASE_BACKEND=sqlite
```

或：

```env
DATABASE_BACKEND=mysql
```

SQLite 使用 `AGENT_MEMORY_DB_PATH`。MySQL 使用 `MYSQL_*` 配置并共享 SQLAlchemy Engine；测试可以直接注入 Engine。未知后端会在应用启动时明确失败，不会静默回退。

## 5. 五类 MySQL 实现

```text
app/persistence/mysql_agent_state_store.py
app/persistence/mysql_route_cache.py
app/persistence/mysql_restaurant_cache.py
app/persistence/mysql_trip_version_store.py
app/persistence/mysql_trip_task_store.py
```

MySQL Store 保持 SQLite 的业务语义，并增加数据库级并发控制：版本确认使用行锁；Worker 领取使用 `FOR UPDATE SKIP LOCKED`；任务创建通过幂等键、请求指纹、唯一约束和命名锁防止重复；任务快照和 SSE 事件在同一事务提交。

## 6. 验证

普通测试默认不连接 MySQL：

```powershell
python -m unittest discover -s tests
```

真实 MySQL 契约和并发测试需显式启用：

```powershell
$env:RUN_MYSQL_INTEGRATION_TESTS="1"
python -m unittest tests.test_mysql_stores -v
Remove-Item Env:RUN_MYSQL_INTEGRATION_TESTS
```

切换前还应执行：

```powershell
alembic upgrade head
python scripts/check_mysql.py
```

## 7. 当前边界与回滚

- 当前尚未把 SQLite 历史数据迁移到 MySQL；
- 在迁移脚本完成前，不应把“切换后看不到旧会话”误判为数据丢失；
- Redis 尚未接入，后续只做加速和协调，MySQL 仍是事实来源；
- 回滚时将 `DATABASE_BACKEND` 改回 `sqlite`，不要删除 SQLite 文件；
- 不要在后端或 Worker 运行时直接覆盖 SQLite 数据库文件。
