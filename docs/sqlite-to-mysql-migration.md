# SQLite → MySQL 历史数据迁移

## 1. 目标与安全边界

迁移覆盖当前 SQLite 中的七张业务表：

1. `agent_sessions`；
2. `route_cache`；
3. `restaurant_cache`；
4. `trip_plan_versions`；
5. `trip_drafts`；
6. `trip_planning_tasks`；
7. `trip_task_events`。

迁移遵守以下约束：

- 执行前自动使用 SQLite Online Backup API 创建一致性快照；
- 不覆盖 MySQL 中已经存在的主键记录；
- 相同主键、相同内容视为幂等跳过；
- 相同主键、不同内容默认终止，并保留结构化冲突统计；
- 每条实际插入的数据都会写入迁移凭证；
- 回滚只删除本批次插入且此后未被修改的数据；
- SQLite 原文件不会被修改或删除；
- `verify.safe_to_cutover=true` 之前不得切换 `DATABASE_BACKEND=mysql`。

## 2. Schema 准备

迁移审计使用两张表：

| 表 | 用途 |
|---|---|
| `data_migration_batches` | 保存源快照摘要、批次状态和结构化报告 |
| `data_migration_records` | 保存本批次真实插入的目标主键和行摘要 |

先升级开发库和测试库：

```powershell
alembic upgrade head
alembic -x database=travel_agent_test upgrade head
```

随后检查：

```powershell
python scripts/check_mysql.py
python scripts/check_mysql.py --database travel_agent_test
```

## 3. Dry-run

Dry-run 不写 SQLite 和 MySQL，只检查：

- SQLite 完整性；
- 必需列和旧库默认列兼容；
- JSON 是否可解析；
- 时间能否转换为 UTC `DATETIME(6)`；
- 字符串是否超过 MySQL 字段长度；
- 目标记录是缺失、同值还是冲突。

```powershell
python scripts/migrate_sqlite_to_mysql.py dry-run
```

无错误且无冲突时返回码为 `0`。存在无效源数据或目标冲突时返回码为 `2`。

## 4. Execute

正式执行会先把 `data/agent_memory.db` 备份到 `data/backups/`，然后只读取该不可变快照：

```powershell
python scripts/migrate_sqlite_to_mysql.py execute
```

输出中的以下字段必须保存到验收报告：

```text
batch_id
source_path
source_sha256
backup_manifest
tables
totals
```

如果进程在中途退出，可使用同一批次继续：

```powershell
python scripts/migrate_sqlite_to_mysql.py execute --resume-batch-id <batch-id>
```

脚本会从批次记录读取原快照路径，并校验 SHA-256。已写入且摘要一致的记录计入 `resumed`，不会重复插入。

默认遇到不同内容的主键冲突会失败。只有在明确接受“保留 MySQL 现有值、稍后人工解决”的情况下才使用：

```powershell
python scripts/migrate_sqlite_to_mysql.py execute --allow-conflicts
```

即使允许冲突，后续 verify 也不会给出 `safe_to_cutover=true`。

## 5. Verify

使用 execute 输出的批次 ID 逐行比较原快照与 MySQL：

```powershell
python scripts/migrate_sqlite_to_mysql.py verify --batch-id <batch-id>
```

验收条件：

```json
{
  "verified": true,
  "safe_to_cutover": true
}
```

验证包含完整行摘要，不只是记录数量。任意缺失行或不同值行都会使命令返回码为 `3`。

## 6. Rollback

回滚指定批次：

```powershell
python scripts/migrate_sqlite_to_mysql.py rollback --batch-id <batch-id>
```

回滚按外键依赖逆序执行。每行删除前再次计算摘要：

- 摘要与迁移凭证一致：删除；
- 记录已经不存在：计入 `missing`；
- 记录迁移后被业务修改：计入 `protected_modified`，不会删除。

`protected_modified > 0` 时批次状态为 `rollback_partial`，命令返回码为 `4`。

## 7. 切换步骤

只有 verify 完整通过后才执行：

1. 停止 API 和 Worker，避免切换窗口继续产生 SQLite 新数据；
2. 再创建一次最终 SQLite 快照；
3. 对最终快照执行 execute 和 verify；
4. 修改本地 `.env`：

   ```env
   DATABASE_BACKEND=mysql
   ```

5. 重启 API 和 Worker；
6. 验证历史会话、结果页、草稿版本、任务恢复和 SSE；
7. 执行杭州固定端到端验收；
8. 保留 SQLite 原库和迁移快照，不立即删除。

## 8. 常用返回码

| 返回码 | 含义 |
|---:|---|
| `0` | 操作成功 |
| `1` | 连接、Schema、输入或执行错误 |
| `2` | Dry-run 存在无效数据或冲突 |
| `3` | Verify 不完整，禁止切换 |
| `4` | Rollback 部分完成，存在受保护的后续修改 |

## 9. 凭据要求

脚本从本地 `.env` 和高优先级 `.env.local` 读取 MySQL 凭据，不接受命令行密码参数，也不会在报告中输出用户名或密码。执行前确认 `MYSQL_PASSWORD` 已在 `.env.local` 配置，并确保 `.env`、`.env.local` 均被 Git 忽略。
