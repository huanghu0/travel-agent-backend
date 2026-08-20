# 持久化抽象与 SQLite 安全备份

## 1. 本阶段目标

阶段 0 和阶段 1 不改变生产数据格式，也不接入 MySQL。主要完成：

1. 对当前 SQLite 数据库创建一致性备份；
2. 将业务层对 SQLite 具体类的依赖改为统一 Store 接口；
3. 通过 Store 工厂集中选择数据库后端；
4. 为下一阶段 MySQL Store 和 SQLite → MySQL 迁移脚本建立稳定边界。

## 2. SQLite 备份

使用下面的命令创建在线一致性备份：

```powershell
python scripts/backup_sqlite.py
```

脚本默认备份：

```text
data/agent_memory.db
```

到：

```text
data/backups/
```

备份使用 SQLite Online Backup API，可以正确处理 WAL 模式下的数据库快照。脚本会在备份前后执行完整性检查，并在备份旁生成包含文件大小和 SHA-256 的 manifest。

`data/` 已被 `.gitignore` 排除，数据库和备份不会进入 Git。

## 3. 统一 Store 接口

接口集中在：

```text
app/persistence/interfaces.py
```

当前包含：

- `AgentStateStore`：AgentState 检查点、会话列表和质量基线；
- `TripVersionStore`：草稿、候选版本和确认版本；
- `TripTaskStore`：异步任务、租约、取消和 SSE 事件；
- `RouteCacheStore`：真实路线缓存；
- `RestaurantCacheStore`：餐饮候选快照缓存。

Orchestrator、TripDraftService、TripTaskWorker 和任务执行上下文只依赖这些接口，不再要求 SQLite 具体实现。

## 4. Store 工厂

应用通过：

```text
app/persistence/factory.py
```

创建全部 Store。当前配置：

```env
DATABASE_BACKEND=sqlite
AGENT_MEMORY_DB_PATH=data/agent_memory.db
```

阶段一只注册 SQLite。若错误设置为 `mysql`，应用会在启动阶段明确报错，而不是静默回退到 SQLite。阶段二已完成 MySQL Schema 和 Alembic 基础设施；MySQL Store 仍将在下一阶段实现并注册。

## 5. 兼容性

现有 SQLite 类和旧导入路径继续保留，因此当前测试、故障注入脚本和离线验收样本不需要同时重写。数据库无关异常已经移动到：

```text
app/persistence/exceptions.py
```

旧模块仍会重新导出同一个异常对象，避免破坏已有调用方。

## 6. 回滚

本阶段没有修改 SQLite 表结构。若需要回滚代码，只需恢复改动；若运行数据库出现意外，可以停止服务后将已验证的备份复制回 `data/agent_memory.db`。

不要在后端或 Worker 正在运行时直接覆盖数据库文件。


## 7. 阶段二进展

MySQL 连接池、七张业务表、Alembic 初始迁移、开发/测试库初始化和 Schema 校验已经完成，详见：

```text
docs/mysql-infrastructure.md
```

当前运行时仍使用 SQLite。只有五类 MySQL Store 通过接口一致性、事务和并发测试后，才会在 Store 工厂中开放 `DATABASE_BACKEND=mysql`。
