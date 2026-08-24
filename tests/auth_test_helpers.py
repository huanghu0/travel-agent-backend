"""API 测试使用的固定认证身份。"""

from datetime import datetime, timezone

from app.auth.models import User


TEST_USER = User(
    user_id="00000000-0000-0000-0000-000000000001",
    username="test_user",
    created_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
)


def install_main_auth_override(main_module) -> None:
    main_module.app.dependency_overrides[main_module.current_user_dependency] = (
        lambda: TEST_USER
    )


def remove_main_auth_override(main_module) -> None:
    main_module.app.dependency_overrides.pop(
        main_module.current_user_dependency,
        None,
    )
