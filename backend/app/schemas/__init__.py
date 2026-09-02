"""Schema package exports."""

from app.schemas.auth import LoginRequest, LogoutResponse, TokenResponse
from app.schemas.common import ErrorResponse, PageMeta
from app.schemas.file import FileListOut, FileOut, FileStatsOut, FileActionOut
from app.schemas.role import PermissionListOut, PermissionOut, RoleListOut, RoleOut
from app.schemas.setting import SettingOut, SettingUpdateRequest, SettingsOut
from app.schemas.user import (
    UserActionOut,
    UserCreate,
    UserListOut,
    UserOut,
    UserPasswordChange,
    UserUpdate,
)

__all__ = [
    "ErrorResponse",
    "FileListOut",
    "FileOut",
    "FileStatsOut",
    "FileActionOut",
    "LoginRequest",
    "LogoutResponse",
    "PageMeta",
    "PermissionListOut",
    "PermissionOut",
    "RoleListOut",
    "RoleOut",
    "SettingOut",
    "SettingUpdateRequest",
    "SettingsOut",
    "TokenResponse",
    "UserActionOut",
    "UserCreate",
    "UserListOut",
    "UserOut",
    "UserPasswordChange",
    "UserUpdate",
]
