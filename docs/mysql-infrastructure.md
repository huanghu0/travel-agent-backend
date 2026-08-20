# MySQL 基础设施（阶段 2）

## 1. 本阶段目标

阶段 2 为后续 MySQL Store 和 SQLite 数据迁移建立可重复、可检查的数据库基础设施。本阶段完成：

1. 增加 MySQL 连接配置和 SQLAlchemy 连接池；
2. 定义七张业务表的 SQLAlchemy 元数据；
3. 使用 Alembic 管理 MySQL Schema；
4. 提供开发库、测试库初始化脚本；
5. 提供连接、迁移版本和物理 Schema 校验；
6. 保持现有 SQLite 运行链路不变。

> 当前仍必须使用 `DATABASE_BACKEND=sqlite`。MySQL Store 尚未注册，不能把正式运行后端切换成 `mysql`。

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

## 6. 七张业务表

| 表 | 用途 |
|---|---|
| `agent_sessions` | AgentState 检查点、质量摘要和执行统计 |
| `route_cache` | 高德真实路线缓存 |
| `restaurant_cache` | 餐饮候选和营业信息缓存 |
| `trip_plan_versions` | 原始、候选和确认后的行程版本 |
| `trip_drafts` | 用户编辑中的行程草稿 |
| `trip_planning_tasks` | 异步规划任务、幂等键、租约和取消状态 |
| `trip_task_events` | 可回放 SSE 事件 |

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
- 七张业务表是否齐全；
- 字段、主键、索引、唯一约束和外键是否与 SQLAlchemy 元数据一致；
- 大对象是否为 `LONGTEXT`；
- 时间是否为 `DATETIME(6)`；
- 事件 ID 是否为 `BIGINT UNSIGNED`；
- 表是否为 InnoDB 和 utf8mb4。

成功结果应包含：

```json
{
  "healthy": true,
  "alembic_revision": "79714a229219",
  "schema_valid": true,
  "schema_errors": []
}
```

## 8. 测试

基础设施单元测试默认不连接 MySQL：

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

阶段 2 **没有**：

- 注册 MySQL Store；
- 把 API、Worker 或缓存切换到 MySQL；
- 迁移 `data/agent_memory.db` 中的数据；
- 删除 SQLite；
- 接入 Redis。

下一阶段应实现五类 MySQL Store，并重点保证：

1. 接口行为与现有 SQLite Store 一致；
2. 事务边界覆盖任务状态和 SSE 事件写入；
3. Worker 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 抢占任务；
4. 租约、心跳、取消和幂等操作具备并发测试；
5. MySQL Store 全量通过后，才允许 `DATABASE_BACKEND=mysql`。

随后再实现 SQLite → MySQL 的 `dry-run`、`execute`、`verify` 和可回滚迁移流程。
