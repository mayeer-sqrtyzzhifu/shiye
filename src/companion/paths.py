"""运行期文件的位置。

源码目录只放源码。数据库、密钥、``.env`` 都属于**运行期状态**，放在用户
数据目录里，而不是仓库工作区内：

    Windows : %LOCALAPPDATA%\\shiye\\
    macOS   : ~/Library/Application Support/shiye/
    Linux   : $XDG_DATA_HOME/shiye/ 或 ~/.local/share/shiye/

可以用 ``SHIYE_HOME`` 覆盖（测试和便携使用都靠它）。

为什么值得单独一个模块：早先把 ``data/`` 和 ``.env`` 放在仓库根目录，
结果它们和源码混在一起——虽然被 .gitignore 挡住了，但目录一看就乱，
而且换机器 / 重装时容易和源码一起被删。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_NAME = "shiye"


def user_data_dir() -> Path:
    """运行期数据的根目录（不创建）。"""
    override = os.environ.get("SHIYE_HOME")
    if override:
        return Path(override).expanduser()

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / APP_NAME


def data_dir() -> Path:
    """数据库与密钥所在目录。"""
    return user_data_dir() / "data"


def env_file() -> Path:
    """运行期配置（含 API key）。"""
    return user_data_dir() / ".env"


def legacy_env_file() -> Path:
    """旧版本放在仓库根目录的 .env；只读兼容，不再写入。"""
    return REPO_ROOT / ".env"


def ensure_dirs() -> Path:
    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory
