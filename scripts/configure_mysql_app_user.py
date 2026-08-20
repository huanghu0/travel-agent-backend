"""安全配置本地 MySQL 运行账号，并把生成的密码写入本地 ``.env``。

该脚本只在交互式终端中读取管理员密码，不接受命令行密码参数，避免密码
进入 shell 历史、进程列表或项目文件。应用账号密码由系统安全随机数生成，
仅写入已被 Git 忽略的本地 ``.env.local``，控制台和结果报告都不会输出密码。
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import secrets
import string
import sys
from pathlib import Path

import pymysql

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import settings
from app.persistence.database import (
    MySQLDatabaseConfig,
    check_mysql_health,
    create_mysql_engine,
)

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
_LOCAL_ACCOUNT_HOSTS = ("localhost", "127.0.0.1")
_RUNTIME_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE"


def _require_identifier(value: str, label: str) -> str:
    """限制数据库名和账号名，避免把不可信文本拼入管理 SQL。"""

    if not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} 只能包含字母、数字和下划线: {value!r}")
    return value


def _generate_password(length: int = 40) -> str:
    """生成适合写入 dotenv 的高熵密码，不包含引号、空格和反斜杠。"""

    # 排除 #、$、引号和反斜杠，避免 dotenv 注释、变量展开和转义歧义。
    alphabet = string.ascii_letters + string.digits + "!%&()*+,-./:;<=>?@[]^_{}~"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _replace_env_value(path: Path, key: str, value: str) -> None:
    """原位更新本地 .env；不把密码打印到控制台。"""

    text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    lines = text.splitlines()
    replacement = f"{key}={value}"
    replaced = False
    output: list[str] = []
    for line in lines:
        if line.startswith(f"{key}="):
            output.append(replacement)
            replaced = True
        else:
            output.append(line)
    if not replaced:
        if output and output[-1] != "":
            output.append("")
        output.append(replacement)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def _configure_account(
    *,
    admin_user: str,
    admin_password: str,
    app_user: str,
    app_password: str,
    databases: list[str],
) -> None:
    """创建/轮换本地应用账号，并只授予业务读写权限。"""

    connection = pymysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=admin_user,
        password=admin_password,
        charset=settings.MYSQL_CHARSET,
        connect_timeout=settings.MYSQL_CONNECT_TIMEOUT_SECONDS,
        read_timeout=settings.MYSQL_READ_TIMEOUT_SECONDS,
        write_timeout=settings.MYSQL_WRITE_TIMEOUT_SECONDS,
        autocommit=False,
    )
    try:
        with connection.cursor() as cursor:
            for database in databases:
                cursor.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{database}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            for account_host in _LOCAL_ACCOUNT_HOSTS:
                cursor.execute(
                    f"CREATE USER IF NOT EXISTS '{app_user}'@'{account_host}' "
                    "IDENTIFIED BY %s",
                    (app_password,),
                )
                cursor.execute(
                    f"ALTER USER '{app_user}'@'{account_host}' IDENTIFIED BY %s",
                    (app_password,),
                )
                for database in databases:
                    cursor.execute(
                        f"GRANT {_RUNTIME_PRIVILEGES} ON `{database}`.* "
                        f"TO '{app_user}'@'{account_host}'"
                    )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _verify_app_account(app_user: str, app_password: str, database: str) -> dict[str, str]:
    """使用新账号执行只读健康检查，确认凭据和授权立即可用。"""

    config = MySQLDatabaseConfig.from_settings(settings, database=database)
    config = MySQLDatabaseConfig(
        host=config.host,
        port=config.port,
        database=database,
        user=app_user,
        password=app_password,
        charset=config.charset,
        pool_size=config.pool_size,
        max_overflow=config.max_overflow,
        pool_recycle_seconds=config.pool_recycle_seconds,
        pool_pre_ping=config.pool_pre_ping,
        connect_timeout_seconds=config.connect_timeout_seconds,
        read_timeout_seconds=config.read_timeout_seconds,
        write_timeout_seconds=config.write_timeout_seconds,
    )
    engine = create_mysql_engine(config)
    try:
        health = check_mysql_health(engine, config)
        if not health.healthy:
            raise RuntimeError(health.error or "应用账号健康检查失败")
        return {
            "target": health.target,
            "database": health.database or database,
            "server_version": health.server_version or "unknown",
        }
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="安全配置本地 MySQL 最小权限应用账号")
    parser.add_argument("--admin-user", default="root", help="MySQL 管理账号，默认 root")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env.local")
    parser.add_argument("--result-file", type=Path, help="可选：写入不含密码的结构化结果")
    args = parser.parse_args()

    admin_user = _require_identifier(args.admin_user, "管理员账号")
    app_user = _require_identifier(settings.MYSQL_USER, "应用账号")
    databases = [
        _require_identifier(settings.MYSQL_DATABASE, "开发数据库"),
        _require_identifier(settings.MYSQL_TEST_DATABASE, "测试数据库"),
    ]
    app_password = _generate_password()
    admin_password = getpass.getpass(f"请输入 MySQL 管理账号 {admin_user} 的密码: ")
    if not admin_password:
        print("管理员密码不能为空。", file=sys.stderr)
        return 2

    try:
        _configure_account(
            admin_user=admin_user,
            admin_password=admin_password,
            app_user=app_user,
            app_password=app_password,
            databases=databases,
        )
        health = _verify_app_account(app_user, app_password, settings.MYSQL_DATABASE)
        _replace_env_value(args.env_file, "MYSQL_PASSWORD", app_password)
        result = {
            "status": "ok",
            "account": app_user,
            "account_hosts": list(_LOCAL_ACCOUNT_HOSTS),
            "databases": databases,
            "privileges": _RUNTIME_PRIVILEGES,
            "env_file_updated": str(args.env_file.resolve()),
            "health": health,
        }
        if args.result_file:
            args.result_file.parent.mkdir(parents=True, exist_ok=True)
            args.result_file.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("应用账号密码已安全写入本地 .env.local，未在控制台输出。")
        return 0
    except Exception as exc:
        error = {
            "status": "error",
            "error_type": exc.__class__.__name__,
            "message": " ".join(str(exc).split())[:500],
        }
        if args.result_file:
            args.result_file.parent.mkdir(parents=True, exist_ok=True)
            args.result_file.write_text(
                json.dumps(error, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
