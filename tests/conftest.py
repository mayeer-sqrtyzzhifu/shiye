import os
import sys

_override = os.environ.get("SHIYE_HOME")
if _override:
    _base = _override
else:
    _base = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local"),
        "shiye",
    )

# 字节码缓存挪出源码树：跑测试不该在 src/ 下留一堆 __pycache__。
# 这一段刻意不 import companion.paths —— 任何 companion 的导入都会在
# 设置生效前写出 .pyc，等于白设。
if sys.pycache_prefix is None:
    sys.pycache_prefix = os.path.join(_base, "pycache")

import pytest  # noqa: E402

from companion import db  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    db.apply_schema(c)
    try:
        yield c
    finally:
        c.close()
