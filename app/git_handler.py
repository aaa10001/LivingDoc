"""Git 操作封装：只负责"把代码弄到本地、算出改了哪些文件"，不做任何业务判断。

【为什么用 subprocess 而不是 GitPython】
GitPython 在 Windows + 大仓库 + 非 ASCII 路径下容易踩三个坑：进度输出阻塞、
编码错误、以及 Repo 对象没有正确 close 导致文件句柄泄漏（Windows 上目录删不掉）。
subprocess 调 git 的报错就是 git 的原始 stderr，排查成本最低，而且这些命令
和你在命令行里敲的完全一样，人类可读、可复现。代价是要自己解析输出 —— 下面的
解析函数都配了单测。

【四个必须做对的细节】
1. GIT_TERMINAL_PROMPT=0。私有仓库令牌过期时，git 会弹凭据对话框等待输入；
   Celery worker 没有终端，任务会永久挂起而不是失败。这是本项目的真实事故点。
2. 令牌不写进 .git/config。用 -c http.extraHeader=... 逐条命令注入，
   否则仓库目录被拷走/打包上传时令牌就泄露了。
3. 克隆先落到 tmp 目录再 os.replace 原子落位。两个 webhook 并发触发同一仓库时，
   直接 clone 到最终目录会互相撕碎工作区。
4. -c core.quotepath=false 且解析统一走 -z（NUL 分隔）。中文文件名在默认输出里
   会被转义成 "\346\226\207"，用 -z 拿到的是原始 UTF-8 字节，不用解转义。
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings

ZERO_SHA = "0" * 40

# 只有"环境/网络抖动"类错误才值得重试；认证和仓库不存在重试一万次也没用。
#
# 这里必须用正则而不是 in 子串匹配：git 的真实报错是
#   fatal: repository 'https://github.com/x/y.git' not found
# 仓库 URL 夹在 "repository" 和 "not found" 中间，子串 "repository not found"
# 永远匹配不上 —— 这个 bug 是单测抓出来的，直接后果是对着不存在的仓库重试 3 次。
_NON_RETRYABLE = (
    re.compile(r"authentication failed", re.I),
    re.compile(r"could not read (username|password)", re.I),
    re.compile(r"invalid username or password", re.I),
    re.compile(r"permission denied", re.I),
    re.compile(r"terminal prompts disabled", re.I),
    re.compile(r"repository\b.*\bnot found", re.I),
    re.compile(r"does not appear to be a git repository", re.I),
    re.compile(r"returned error: 404", re.I),
)

_CRED_IN_URL = re.compile(r"(?P<scheme>https?://)[^/@\s]+@")


class GitError(RuntimeError):
    """git 命令执行失败。message 已脱敏，可以直接写日志/返回给前端。"""

    def __init__(self, message: str, *, returncode: int = -1, stderr: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr

    @property
    def retryable(self) -> bool:
        # 认证失败 / 仓库不存在 / 404：重试没有意义，直接落 failed 让人去看
        if any(p.search(self.stderr or "") for p in _NON_RETRYABLE):
            return False
        # 其余（超时、连接重置、5xx、索引锁）视为可重试
        return True


@dataclass(frozen=True, slots=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class RepoRef:
    """仓库定位信息。只保留我们真正需要的两个字段。"""

    full_name: str  # "owner/repo"
    clone_url: str  # "https://github.com/owner/repo.git"

    @classmethod
    def from_webhook(cls, repo: dict) -> "RepoRef":
        full_name = str(repo.get("full_name") or "").strip()
        if not full_name or "/" not in full_name:
            raise GitError(f"webhook payload 里缺少合法的 repository.full_name: {full_name!r}")
        clone_url = str(repo.get("clone_url") or f"https://github.com/{full_name}.git")
        return cls(full_name=full_name, clone_url=clone_url)


@dataclass(frozen=True, slots=True)
class FileChange:
    """一个文件的变更。status 用 git 的字母码：A 新增 / M 修改 / D 删除 / T 类型变更。"""

    path: str
    status: str
    additions: int = 0
    deletions: int = 0

    @property
    def is_deleted(self) -> bool:
        return self.status == "D"


# --------------------------------------------------------------------------- #
# 底层：执行 git
# --------------------------------------------------------------------------- #
def _redact(text: str) -> str:
    """把令牌从 stderr 里抹掉。git 的报错经常原样回显带凭据的 URL。"""
    if not text:
        return text
    settings = get_settings()
    token = settings.github_token
    if token:
        text = text.replace(token, "***")
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        text = text.replace(encoded, "***")
    return _CRED_IN_URL.sub(r"\g<scheme>***@", text)


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    # 避免继承了外部 shell 的 GIT_DIR（在仓库目录里启动 API 时很常见）
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",  # 绝不等待用户输入
            "GIT_ASKPASS": "echo",
            "GCM_INTERACTIVE": "never",
            "GIT_PAGER": "cat",
        }
    )
    return env


def _run_git(
    args: list[str],
    *,
    cwd: Path | str | None = None,
    timeout: int | None = None,
    auth: bool = False,
    check: bool = True,
) -> GitResult:
    settings = get_settings()
    cmd = ["git", "-c", "core.quotepath=false"]
    if auth and settings.github_token:
        cred = base64.b64encode(
            f"x-access-token:{settings.github_token}".encode()
        ).decode("ascii")
        # 注意：-c 只对本次命令生效，不会写进 .git/config
        cmd += ["-c", f"http.extraHeader=AUTHORIZATION: basic {cred}"]
    cmd += list(args)

    limit = timeout if timeout is not None else settings.git_timeout_seconds
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=_git_env(),
            capture_output=True,
            timeout=limit,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} 超时（{limit}s）", returncode=-1) from exc
    except FileNotFoundError as exc:
        raise GitError("找不到 git 可执行文件，请确认 PATH 里有 git") from exc

    result = GitResult(
        returncode=proc.returncode,
        stdout=proc.stdout.decode("utf-8", "replace"),
        stderr=_redact(proc.stderr.decode("utf-8", "replace")),
    )
    if check and result.returncode != 0:
        raise GitError(
            f"git {args[0]} 失败（exit={result.returncode}）: {result.stderr.strip()[:600]}",
            returncode=result.returncode,
            stderr=result.stderr,
        )
    return result


def is_zero_sha(sha: str | None) -> bool:
    """git 用 40 个 0 表示"不存在"，分支删除事件的 after 就是它。"""
    return not sha or set(sha) == {"0"}


# --------------------------------------------------------------------------- #
# 仓库缓存
# --------------------------------------------------------------------------- #
def repo_path_for(full_name: str) -> Path:
    """owner/repo -> <repo_cache_dir>/owner__repo（做过字符白名单清洗）。"""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", full_name.replace("/", "__")).strip("._")
    if not safe:
        raise GitError(f"无法把仓库名映射为目录名: {full_name!r}")
    return get_settings().repo_cache_dir / safe


def ensure_repo(ref: RepoRef, *, timeout: int | None = None) -> Path:
    """确保本地有一份该仓库的镜像，返回目录路径。幂等，可并发调用。"""
    settings = get_settings()
    dest = repo_path_for(ref.full_name)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if (dest / ".git").exists():
        # 增量：只更新远端分支引用，不 checkout（分析只需要对象，不需要工作区）
        _run_git(
            ["fetch", "--prune", "--no-tags", "origin",
             "+refs/heads/*:refs/remotes/origin/*"],
            cwd=dest,
            timeout=timeout,
            auth=True,
        )
        return dest

    tmp = dest.with_name(f"{dest.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}")
    shutil.rmtree(tmp, ignore_errors=True)
    base = ["clone", "--no-checkout", "--single-branch"]
    if settings.git_clone_depth:
        base += ["--depth", str(settings.git_clone_depth)]
    try:
        _run_git(
            [*base, "--filter=blob:none", ref.clone_url, str(tmp)],
            timeout=timeout,
            auth=True,
        )
    except GitError as exc:
        if not exc.retryable:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        # 服务端不支持 partial clone 或 git 版本太老 -> 退回普通克隆
        shutil.rmtree(tmp, ignore_errors=True)
        _run_git([*base, ref.clone_url, str(tmp)], timeout=timeout, auth=True)

    try:
        os.replace(tmp, dest)
    except OSError:
        # 另一个 worker 抢先落位了：删掉自己的临时目录，直接复用对方的
        shutil.rmtree(tmp, ignore_errors=True)
        if not (dest / ".git").exists():
            raise
    return dest


def has_commit(repo_dir: Path, sha: str, *, timeout: int | None = None) -> bool:
    if is_zero_sha(sha):
        return False
    res = _run_git(
        ["cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo_dir, timeout=timeout, check=False
    )
    return res.returncode == 0


def fetch_commits(
    repo_dir: Path, shas: list[str], *, timeout: int | None = None
) -> list[str]:
    """把指定 commit 拉到本地（克隆时 --single-branch 只拿了默认分支）。

    返回仍然缺失的 sha 列表 —— 调用方必须检查，否则后续 diff 会报
    "bad object"，而那个报错完全看不出根因是"没拉下来"。
    """
    missing: list[str] = []
    for sha in dict.fromkeys(s for s in shas if not is_zero_sha(s)):
        if has_commit(repo_dir, sha, timeout=timeout):
            continue
        try:
            # GitHub 允许按 sha 直接取对象（uploadpack.allowAnySHA1InWant）
            _run_git(["fetch", "--no-tags", "origin", sha], cwd=repo_dir,
                     timeout=timeout, auth=True)
        except GitError:
            # 老服务端只允许取 ref：造一个临时 ref 再取
            _run_git(
                ["fetch", "--no-tags", "origin", f"+{sha}:refs/livingdoc/{sha}"],
                cwd=repo_dir, timeout=timeout, auth=True,
            )
        if not has_commit(repo_dir, sha, timeout=timeout):
            missing.append(sha)
    return missing


# --------------------------------------------------------------------------- #
# diff 解析
# --------------------------------------------------------------------------- #
def _parse_name_status(raw: str) -> list[tuple[str, str]]:
    """"M\0a.py\0D\0b.py\0" -> [("M", "a.py"), ("D", "b.py")]"""
    parts = raw.split("\0")
    if parts and parts[-1] == "":
        parts.pop()
    pairs: list[tuple[str, str]] = []
    for i in range(0, len(parts) - 1, 2):
        status, path = parts[i], parts[i + 1]
        # "M" / "A" / "D" / "T" 只会是单字母；R100 之类在 --no-renames 下不会出现
        pairs.append((status[:1], path))
    return pairs


def _parse_numstat(raw: str) -> dict[str, tuple[int, int]]:
    """"1\t1\ta.py\0" -> {"a.py": (1, 1)}；二进制文件是 "-\t-\tb.png"。"""
    counts: dict[str, tuple[int, int]] = {}
    for chunk in raw.split("\0"):
        if not chunk:
            continue
        fields = chunk.split("\t")
        if len(fields) < 3:
            continue
        added, removed, path = fields[0], fields[1], "\t".join(fields[2:])
        try:
            counts[path] = (int(added), int(removed))
        except ValueError:
            counts[path] = (0, 0)  # 二进制文件
    return counts


def diff_commits(
    repo_dir: Path,
    base_sha: str,
    head_sha: str,
    *,
    max_files: int | None = None,
    timeout: int | None = None,
) -> list[FileChange]:
    """列出 base..head 之间所有变更文件（含增删行数）。

    刻意用 --no-renames：改名在语义上就是"删掉旧文件 + 新增新文件"，对文档重生成
    来说这是**正确**的（旧类实体消失、新类实体出现），而且解析零歧义。
    代价是移动大文件时 diff 体积变大 —— 阶段三会改用 --find-renames 并在
    changed_files 表里记 old_path，用于保留文档链接。
    """
    if is_zero_sha(base_sha):
        raise GitError("base_sha 为空，无法计算 diff（首次推送应改用 HEAD^ 或跳过）")

    limit = max_files if max_files is not None else get_settings().diff_max_files
    common = ["--no-renames", "--no-ext-diff", "--no-color", "-z"]

    name_status = _run_git(
        ["diff", *common, "--name-status", base_sha, head_sha],
        cwd=repo_dir, timeout=timeout,
    )
    numstat = _run_git(
        ["diff", *common, "--numstat", base_sha, head_sha],
        cwd=repo_dir, timeout=timeout,
    )

    counts = _parse_numstat(numstat.stdout)
    changes: list[FileChange] = []
    for status, path in _parse_name_status(name_status.stdout):
        added, removed = counts.get(path, (0, 0))
        changes.append(FileChange(path=path, status=status, additions=added, deletions=removed))
        if len(changes) >= limit:
            break
    return changes


def read_blob(
    repo_dir: Path, sha: str, path: str, *, timeout: int | None = None
) -> str:
    """读某个 commit 下某个文件的全文。阶段三把这段源码喂给 tree-sitter。"""
    res = _run_git(["show", f"{sha}:{path}"], cwd=repo_dir, timeout=timeout, check=False)
    if res.returncode != 0:
        raise GitError(
            f"读取 {path}@{sha[:8]} 失败: {res.stderr.strip()[:200]}",
            returncode=res.returncode,
            stderr=res.stderr,
        )
    return res.stdout


def repo_size_mb(repo_dir: Path) -> float:
    total = sum(p.stat().st_size for p in repo_dir.rglob("*") if p.is_file())
    return round(total / (1024 * 1024), 1)
