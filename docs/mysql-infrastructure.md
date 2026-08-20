# MySQL 基础设施、五类 Store 与历史迁移（阶段 2～4）

## 1. 本阶段目标

阶段 2 建立可重复、可检查的 MySQL 基础设施；阶段 3 实现五类业务 Store；阶段 4 增加 SQLite 历史迁移链路。当前完成：

1. 增加 MySQL 连接配置和 SQLAlchemy 连接池；
2. 定义七张业务表的 SQLAlchemy 元数据；
3. 使用 Alembic 管理 MySQL Schema；
4. 提供开发库、测试库初始化脚本；
5. 提供连接、迁移版本和物理 Schema 校验；
6. 实现 AgentState、路线缓存、餐饮缓存、行程版本和异步任务五类 MySQL Store；
7. 保持 SQLite 运行链路与回滚能力；
8. 提供 SQLite → MySQL 的 dry-run、execute、verify、resume 和安全 rollback。

> `DATABASE_BACKEND=sqlite` 与 `DATABASE_BACKEND=mysql` 均已注册。首次切换 MySQL 前必须完成 Alembic、迁移和 verify；只有输出 `safe_to_cutover=true` 后才允许切换。

## 2. 依赖

```text
SQLAlchemy==2.0.48
PyMySQL==1.2.0
alembic==1.18.4
```

安装：

```powershell
pip install -r requirements.txt
```

## 3. 本地配置

复制 `.env.example` 中的 MySQL 配置到本地 `.env`：

```env
DATABASE_BACKEND=sqlite
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=travel_agent
MYSQL_TEST_DATABASE=travel_agent_test
MYSQL_USER=你的本地账号
MYSQL_PASSWORD=你的本地密码
MYSQL_CHARSET=utf8mb4
MYSQL_POOL_SIZE=10
MYSQL_MAX_OVERFLOW=20
MYSQL_POOL_RECYCLE_SECONDS=1800
MYSQL_POOL_PRE_PING=true
MYSQL_CONNECT_TIMEOUT_SECONDS=5
MYSQL_READ_TIMEOUT_SECONDS=30
MYSQL_WRITE_TIMEOUT_SECONDS=30
```

安全约束：

- 密码只保存在已被 Git 忽略的本地 `.env`；
- `alembic.ini`、`.env.example`、源码和文档均不保存密码；
- 初始化阶段可使用本地管理员账号，正式运行前应创建最小权限的 `travel_agent_app` 账号；
- 健康检查只输出主机、端口、数据库名，并擦除异常中的原始和 URL 编码密码。

## 4. 创建数据库

脚本只从 `.env` 读取凭据，不接受命令行密码参数：

```powershell
python scripts/init_mysql_databases.py
```

默认创建：

```text
travel_agent
travel_agent_test
```

两者均使用：

```text
CHARACTER SET utf8mb4
COLLATE utf8mb4_unicode_ci
```

如只需创建指定数据库，可重复传入安全数据库名：

```powershell
python scripts/init_mysql_databases.py --database travel_agent
```

## 5. 执行 Alembic 迁移

开发库：

```powershell
alembic upgrade head
```

测试库：

```powershell
alembic -x database=travel_agent_test upgrade head
```

生成离线 SQL（不会连接数据库）：

```powershell
alembic upgrade head --sql > build/mysql-schema.sql
```

`migrations/env.py` 会从 `.env` 读取连接参数，并通过 SQLAlchemy `URL.create()` 安全构造 URL。数据库密码不会写入仓库。

## 6. 七张业务表与两张迁移审计表

| 表 | 用途 |
|---|---|
| `agent_sessions` | AgentState 检查点、质量摘要和执行统计 |
| `route_cache` | 高德真实路线缓存 |
| `restaurant_cache` | 餐饮候选和营业信息缓存 |
| `trip_plan_versions` | 原始、候选和确认后的行程版本 |
| `trip_drafts` | 用户编辑中的行程草稿 |
| `trip_planning_tasks` | 异步规划任务、幂等键、租约和取消状态 |
| `trip_task_events` | 可回放 SSE 事件 |
| `data_migration_batches` | SQLite 历史迁移批次、源摘要和报告 |
| `data_migration_records` | 逐行插入凭证和安全回滚摘要 |

物理约定：

- UUID：`VARCHAR(36)`；
- 请求和缓存指纹：`VARCHAR(64)`；
- 大型 Pydantic JSON 快照：`LONGTEXT`；
- 业务时间：`DATETIME(6)`，应用层统一使用 UTC；
- SSE 事件 ID：`BIGINT UNSIGNED AUTO_INCREMENT`；
- 存储引擎：InnoDB；
- 字符集：utf8mb4；
- 排序规则：utf8mb4_unicode_ci；
- `trip_task_events.task_id` 外键指向 `trip_planning_tasks.task_id`。

MySQL 不允许普通 `LONGTEXT` 使用非表达式默认值，因此 JSON 字符串由后续 Store 显式写入，不依赖数据库默认值。

## 7. 健康检查与 Schema 校验

开发库：

```powershell
python scripts/check_mysql.py
```

测试库：

```powershell
python scripts/check_mysql.py --database travel_agent_test
```

检查内容：

- MySQL 可连接；
- 当前数据库和服务端版本；
- Alembic revision；
- 七张业务表与两张迁移审计表是否齐全；
- 字段、主键、索引、唯一约束和外键是否与 SQLAlchemy 元数据一致；
- 大对象是否为 `LONGTEXT`；
- 时间是否为 `DATETIME(6)`；
- 事件 ID 是否为 `BIGINT UNSIGNED`；
- 表是否为 InnoDB 和 utf8mb4。

成功结果应包含：

```json
{
  "healthy": true,
  "alembic_revision": "a31f0c8d4b72",
  "schema_valid": true,
  "schema_errors": []
}
```

## 8. 测试

基础设施和普通单元测试默认不连接 MySQL：

```powershell
python -m unittest tests.test_mysql_infrastructure -v
```

显式启用本地 MySQL 集成测试：

```powershell
$env:RUN_MYSQL_INTEGRATION_TESTS="1"
python -m unittest tests.test_mysql_infrastructure.MySQLLiveSchemaTests -v
Remove-Item Env:RUN_MYSQL_INTEGRATION_TESTS
```

迁移回滚验收只应在测试库执行：

```powershell
alembic -x database=travel_agent_test downgrade base
alembic -x database=travel_agent_test upgrade head
python scripts/check_mysql.py --database travel_agent_test
```

## 9. 当前边界与下一阶段

阶段 4 已完成迁移工具，但正式切换仍保留以下边界：

- 必须在停写窗口对最终 SQLite 快照重新 execute 和 verify；
- 不删除 SQLite，继续把它作为迁移来源和回滚后端；
- 尚未接入 Redis；
- 首次切换前必须确保开发库已执行 `alembic upgrade head` 并通过 `scripts/check_mysql.py`；
- 只有 verify 返回 `safe_to_cutover=true` 后才允许修改 `DATABASE_BACKEND`。

下一阶段是在停写窗口完成最终迁移与后端切换演练；稳定后再接入 Redis 通知和协调能力。

## 10. 阶段 3：五类 MySQL Store

已实现文件：

```text
app/persistence/mysql_agent_state_store.py
app/persistence/mysql_route_cache.py
app/persistence/mysql_restaurant_cache.py
app/persistence/mysql_trip_version_store.py
app/persistence/mysql_trip_task_store.py
```

统一工厂 `app/persistence/factory.py` 根据 `DATABASE_BACKEND` 返回 SQLite 或 MySQL 实现。MySQL 模式从 `MySQLDatabaseConfig` 创建共享 SQLAlchemy Engine，也支持测试注入 Engine。

核心事务约束：

- AgentState 完整 JSON 与质量查询冗余列使用同一 UPSERT；
- 路线、餐饮缓存保持 TTL 非正数不写入、读取过期即删除的原语义；
- 行程版本确认会锁定同一会话版本，在一个事务内撤销旧 confirmed 并确认目标版本；
- 异步任务创建与 `task_queued` 事件同事务提交；
- 幂等键和请求指纹使用 MySQL 命名锁串行化，覆盖相同 key 和不同 key 双击；
- Worker 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 领取任务；
- 进度、终态和事件同事务提交，并检查 Worker 身份及未过期租约；
- 取消后的等待任务不会再次被领取，过期租约可以被其他 Worker 恢复。

真实 MySQL Store 契约与并发测试：

```powershell
$env:RUN_MYSQL_INTEGRATION_TESTS="1"
python -m unittest tests.test_mysql_stores -v
Remove-Item Env:RUN_MYSQL_INTEGRATION_TESTS
```

覆盖五类 Store、缓存过期、版本原子确认、事件原子写入、双 Worker 排他领取、过期租约恢复、取消阻止领取，以及相同/不同幂等键的并发去重。

## 11. 切换运行后端

完成迁移和健康检查后，在本地 `.env` 设置：

```env
DATABASE_BACKEND=mysql
```

启动前执行：

```powershell
alembic upgrade head
python scripts/check_mysql.py
```

当前边界：

- SQLite 实现和数据文件仍保留，作为迁移来源与回滚后端；
- SQLite → MySQL 迁移工具已实现，切换前必须保存批次报告并通过逐行 verify；
- 尚未接入 Redis；Redis 后续只承担任务通知、取消/SSE 唤醒、短期缓存和分布式协调，MySQL 继续作为事实来源。

## 12. SQLite 历史迁移

完整操作手册见 `docs/sqlite-to-mysql-migration.md`。核心命令：

```powershell
python scripts/migrate_sqlite_to_mysql.py dry-run
python scripts/migrate_sqlite_to_mysql.py execute
python scripts/migrate_sqlite_to_mysql.py verify --batch-id <batch-id>
python scripts/migrate_sqlite_to_mysql.py rollback --batch-id <batch-id>
```

迁移不会覆盖 MySQL 已有冲突行；rollback 也不会删除迁移后被业务修改的数据。

本地验收结果见 `docs/mysql-migration-acceptance-2026-08-20.md`。
