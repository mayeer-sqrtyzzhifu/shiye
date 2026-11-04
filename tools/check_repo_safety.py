"""上传前安全检查。

用途::

    python tools/check_repo_safety.py          # 检查工作区 + 历史
    python tools/check_repo_safety.py --staged # 只查暂存区（可挂 pre-commit）

检查四类东西：

1. **密钥样式**：``sk-`` 开头的长串、GitHub token（``ghp_``/``github_pat_``）、
   AWS key。占位符（``sk-...``、``sk-your-key-here``）允许。
2. **敏感文件被跟踪**：``.env``、``data/``、``*.db``、``*.key``、``*.sqlite``。
3. **绝对路径**泄漏用户名（Windows 盘符路径、类 Unix 的 home 目录）。
4. **数据库/密钥文件**出现在 git 历史里（一旦进过历史，删文件也没用）。

退出码 1 表示发现风险，可直接用在推送前或 CI 里。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

SECRET_PATTERNS = (
    ("openai/deepseek style", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("github token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws key", re.compile(r"AKIA[0-9A-Z]{16}")),
)

SENSITIVE_PATH = re.compile(
    r"(^|/)(\.env$|data/|.*\.db$|.*\.db-wal$|.*\.db-shm$|.*\.key$|.*\.sqlite$|.*\.pem$)"
)

ABSOLUTE_USER_PATH = re.compile(r"[A-Za-z]:\\\\?Users\\\\?[^\\\\/\s\"']+" + "|" + r"/home/" + r"[^/\s\"']+")

# 这些是允许出现在仓库里的占位符
PLACEHOLDERS = ("sk-...", "sk-your-key-here", "sk-xxx", "sk-REDACTED")

# 检查器本身含有正则与示例文本，会自匹配 —— 显式豁免
ALLOWLIST = {"tools/check_repo_safety.py"}


def _run(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return result.stdout


def _is_placeholder(line: str) -> bool:
    return any(marker in line for marker in PLACEHOLDERS)


def scan_text(label: str, blob: str) -> list[str]:
    problems: list[str] = []
    for lineno, line in enumerate(blob.splitlines(), 1):
        for name, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(line):
                if _is_placeholder(line):
                    continue
                problems.append(f"{label}:{lineno} 疑似 {name}：{match.group(0)[:8]}…")
        for match in ABSOLUTE_USER_PATH.finditer(line):
            problems.append(f"{label}:{lineno} 绝对路径泄露：{match.group(0)}")
    return problems


def check_worktree() -> list[str]:
    problems: list[str] = []
    files = _run(["git", "ls-files"]).splitlines()
    for path in files:
        if path in ALLOWLIST:
            continue
        if SENSITIVE_PATH.search(path):
            problems.append(f"敏感文件被跟踪：{path}")
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                problems += scan_text(path, handle.read())
        except (OSError, UnicodeError):
            continue
    return problems


def check_history() -> list[str]:
    problems: list[str] = []
    for path in _run(["git", "log", "--all", "--name-only", "--pretty=format:"]).splitlines():
        if path and SENSITIVE_PATH.search(path):
            problems.append(f"历史里出现过敏感文件：{path}（删文件不够，需要改写历史）")
    # 只查"新增行"，避免把已有内容重复报一遍
    diff = _run([
        "git", "log", "--all", "-p", "--unified=0", "--", ".",
        ":(exclude)tools/check_repo_safety.py",
    ])
    return problems + scan_text("history", diff)


def check_staged() -> list[str]:
    problems: list[str] = []
    for path in _run(["git", "diff", "--cached", "--name-only"]).splitlines():
        if SENSITIVE_PATH.search(path):
            problems.append(f"暂存了敏感文件：{path}")
    diff = _run(["git", "diff", "--cached", "--unified=0"])
    return problems + scan_text("staged", diff)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged", action="store_true", help="只查暂存区")
    parser.add_argument("--skip-history", action="store_true", help="跳过历史扫描（较慢）")
    args = parser.parse_args(argv)

    problems: list[str] = []
    if args.staged:
        problems = check_staged()
    else:
        problems = check_worktree()
        if not args.skip_history:
            problems += check_history()

    problems = list(dict.fromkeys(problems))
    if problems:
        print("发现 %d 个风险：" % len(problems))
        for item in problems:
            print("  -", item)
        return 1

    print("OK：没有发现密钥、敏感文件或绝对路径。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

