"""GitHub 仓库回写适配器：把当前生效的文档提交回代码仓库。

为什么需要它：只把文档放在 LivingDoc 的评审站里，代码仓库仍然是"文档缺失"的。
真正让文档跟着代码走，是把它放到仓库内的固定目录（默认 ``docs/livingdoc/``），
这样 clone 代码的人天然就能看到文档，PR 里也能 review 文档 diff。

实现取舍：
- 用 Git Data API 一次提交多个文件，而不是逐个调 Contents API。一次 run 可能更新
  六份文档，逐个 PUT 会产生六条 commit，历史很难看。
- 默认提交到 ``livingdoc/docs`` 分支并开 PR，不直接改 main；需要直接同步时把
  ``GITHUB_DOCS_BRANCH`` 设成 main、``GITHUB_DOCS_OPEN_PR=false``。
- 文件内容按 git blob hash 比较，没有变化就不产生空 commit。
- 只同步 current 版本；人工候选留在 LivingDoc 的 ``_pending`` 语义里，不覆盖人改内容。

需要的 token 权限（fine-grained PAT）：
- Contents: Read and write（读默认分支、写 blob/tree/commit/ref）
- Pull requests: Read and write（默认要开 PR；直接推 main 时可关掉）
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any, Iterable

import httpx

from app.publishers.base import DocumentPayload, PublishResult, PublisherError, RunDigest
from app.publishers.local_kb import safe_name

logger = logging.getLogger(__name__)

_TYPE_LABEL = {
    "class_diagram": "类图",
    "sequence_diagram": "时序图",
    "api_doc": "API 文档",
}


def git_blob_sha(data: bytes) -> str:
    """计算 git blob 的 SHA-1，用来判断仓库里的文件是否已经一模一样。"""
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def document_path(prefix: str, doc: DocumentPayload) -> str:
    """仓库内路径：``docs/livingdoc/<doc_type>/<target_key>.<ext>``。"""
    return f"{prefix}/{doc.doc_type}/{safe_name(doc.target_key)}{doc.extension}"


def build_readme(digest: RunDigest, files: Iterable[tuple[str, bytes]]) -> str:
    """生成目录索引。链接用相对路径，GitHub 和本地编辑器都能点开。"""
    available = {path for path, _ in files}
    lines = [
        "# LivingDoc 活文档",
        "",
        "> 本目录由 LivingDoc 根据代码变更自动生成并同步；正文只包含当前生效版本，",
        "> 人工确认中的候选仍保留在 LivingDoc 评审站。",
        "",
        f"- 仓库：`{digest.repo_full_name}`",
        f"- 最近提交：`{digest.head_sha[:7]}`（base `{digest.base_sha[:7]}`）",
        f"- 触发：`{digest.trigger_type}`"
        + (f" · PR #{digest.pr_number}" if digest.pr_number else ""),
    ]
    if digest.review_url:
        lines.append(f"- 在线评审：[打开 LivingDoc]({digest.review_url})")
    lines += ["", "| 文档 | 类型 | 当前版本 | 文件 |", "|---|---|---|---|"]
    rows = 0
    for doc in digest.documents:
        relative = f"{doc.doc_type}/{safe_name(doc.target_key)}{doc.extension}"
        if not any(path.endswith("/" + relative) for path in available):
            continue
        label = _TYPE_LABEL.get(doc.doc_type, doc.doc_type)
        version = f"v{doc.version}" if doc.version else "-"
        status = " · 待人工确认" if doc.deferred else ""
        lines.append(f"| {doc.title}{status} | {label} | {version} | [{relative}]({relative}) |")
        rows += 1
    if not rows:
        lines.append("| _暂无文档_ | - | - | - |")
    lines.append("")
    return "\n".join(lines)


def _render_files(digest: RunDigest, prefix: str) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for doc in digest.documents:
        if not doc.content.strip():
            continue
        files[document_path(prefix, doc)] = doc.content.encode("utf-8")
    readme = build_readme(digest, files.items())
    files[f"{prefix}/README.md"] = readme.encode("utf-8")
    return files


class GitHubRepoPublisher:
    """把文档写回 GitHub 仓库；缺少 token 时由 dispatch 记 skipped。"""

    name = "github_repo"

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def available(self, settings: Any) -> bool:
        return bool(getattr(settings, "github_token", ""))

    def publish(self, digest: RunDigest, settings: Any) -> list[PublishResult]:
        repo = (digest.repo_full_name or "").strip()
        if not repo or "/" not in repo:
            return [PublishResult(target=self.name, status="skipped", error="missing repo_full_name")]
        prefix = (getattr(settings, "github_docs_dir", "") or "docs/livingdoc").strip("/")
        branch = (getattr(settings, "github_docs_branch", "") or "livingdoc/docs").strip()
        if not branch:
            branch = "livingdoc/docs"

        files = _render_files(digest, prefix)
        if len(files) <= 1:  # 只有 README，没有可同步的正文
            return [
                PublishResult(
                    target=self.name,
                    status="skipped",
                    error="no document content to sync",
                    detail={"prefix": prefix, "branch": branch},
                )
            ]

        with self._client(settings) as gh:
            base_sha, tree_sha, default_branch = self._base_ref(gh, repo, branch)
            existing, truncated = self._tree(gh, repo, tree_sha)
            changed = [
                (path, data)
                for path, data in files.items()
                if truncated or existing.get(path) != git_blob_sha(data)
            ]
            if not changed:
                return [
                    PublishResult(
                        target=self.name,
                        status="success",
                        external_id=base_sha,
                        external_url=self._commit_url(repo, base_sha),
                        detail={
                            "branch": branch,
                            "prefix": prefix,
                            "files": len(files),
                            "changed": 0,
                            "no_changes": True,
                        },
                    )
                ]

            entries = []
            for path, data in changed:
                blob = gh.request(
                    "POST",
                    f"/repos/{repo}/git/blobs",
                    json={
                        "content": base64.b64encode(data).decode("ascii"),
                        "encoding": "base64",
                    },
                )
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})

            tree = gh.request(
                "POST",
                f"/repos/{repo}/git/trees",
                json={"base_tree": tree_sha, "tree": entries},
            )
            message = self._commit_message(digest, changed)
            commit = gh.request(
                "POST",
                f"/repos/{repo}/git/commits",
                json={"message": message, "tree": tree["sha"], "parents": [base_sha]},
            )
            new_sha = commit["sha"]
            gh.request(
                "PATCH",
                f"/repos/{repo}/git/refs/heads/{branch}",
                json={"sha": new_sha, "force": False},
            )

            pr_url = ""
            pr_error = ""
            if getattr(settings, "github_docs_open_pr", True) and branch != default_branch:
                try:
                    pr_url = self._ensure_pull_request(
                        gh, repo, branch=branch, base=default_branch, digest=digest, changed=len(changed)
                    )
                except PublisherError as exc:
                    # 提交已经成功；开 PR 失败不应该把整次同步判成失败。
                    pr_error = str(exc)
                    logger.warning("文档已提交，但开 PR 失败：%s", pr_error)

            return [
                PublishResult(
                    target=self.name,
                    status="success",
                    external_id=new_sha,
                    external_url=pr_url or self._commit_url(repo, new_sha),
                    detail={
                        "branch": branch,
                        "prefix": prefix,
                        "files": len(files),
                        "changed": len(changed),
                        "commit": new_sha,
                        "pull_request": pr_url,
                        "pull_request_error": pr_error,
                    },
                )
            ]

    # ------------------------------------------------------------ HTTP
    def _client(self, settings: Any) -> _GitHubClient:
        return _GitHubClient(
            base_url=str(getattr(settings, "github_api_base", "https://api.github.com")),
            token=str(getattr(settings, "github_token", "")),
            timeout=float(getattr(settings, "publish_timeout_seconds", 15.0)),
            transport=self._transport,
        )

    def _base_ref(self, gh: _GitHubClient, repo: str, branch: str) -> tuple[str, str, str]:
        ref = gh.request("GET", f"/repos/{repo}/git/ref/heads/{branch}", allow_404=True)
        if ref is None:
            meta = gh.request("GET", f"/repos/{repo}")
            default_branch = meta.get("default_branch") or "main"
            base_ref = gh.request("GET", f"/repos/{repo}/git/ref/heads/{default_branch}")
            base_sha = base_ref["object"]["sha"]
            gh.request(
                "POST",
                f"/repos/{repo}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": base_sha},
            )
        else:
            base_sha = ref["object"]["sha"]
            meta = gh.request("GET", f"/repos/{repo}")
            default_branch = meta.get("default_branch") or "main"

        commit = gh.request("GET", f"/repos/{repo}/git/commits/{base_sha}")
        return base_sha, commit["tree"]["sha"], default_branch

    def _tree(self, gh: _GitHubClient, repo: str, tree_sha: str) -> tuple[dict[str, str], bool]:
        data = gh.request("GET", f"/repos/{repo}/git/trees/{tree_sha}?recursive=1")
        items = {
            item["path"]: item["sha"]
            for item in data.get("tree", [])
            if item.get("type") == "blob"
        }
        return items, bool(data.get("truncated"))

    def _ensure_pull_request(
        self, gh: _GitHubClient, repo: str, *, branch: str, base: str, digest: RunDigest, changed: int
    ) -> str:
        owner = repo.split("/", 1)[0]
        existing = gh.request(
            "GET", f"/repos/{repo}/pulls?state=open&head={owner}:{branch}"
        )
        if existing:
            return existing[0].get("html_url") or ""
        pr = gh.request(
            "POST",
            f"/repos/{repo}/pulls",
            json={
                "title": f"[LivingDoc] 更新活文档（{changed} 份）",
                "head": branch,
                "base": base,
                "body": (
                    "## LivingDoc 文档同步\n\n"
                    f"- run：`{digest.run_id}`\n"
                    f"- 代码提交：`{digest.head_sha[:7]}`\n"
                    f"- 更新文件：{changed} 份\n\n"
                    "文档由 LivingDoc 生成并通过机器校验；合并前请人工确认内容。"
                ),
            },
        )
        return pr.get("html_url") or ""

    @staticmethod
    def _commit_message(digest: RunDigest, changed: list[tuple[str, bytes]]) -> str:
        return (
            f"docs: 更新 LivingDoc 活文档（{len(changed)} 份）\n\n"
            f"run: {digest.run_id}\n"
            f"head: {digest.head_sha[:7]}\n"
            f"trigger: {digest.trigger_type}"
        )

    @staticmethod
    def _commit_url(repo: str, sha: str) -> str:
        return f"https://github.com/{repo}/commit/{sha}"


class _GitHubClient:
    """薄封装：统一鉴权、错误转换和 JSON 解析。"""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout: float,
        transport: httpx.BaseTransport | None,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "LivingDoc/1.0",
            },
        )

    def __enter__(self) -> "_GitHubClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._http.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> Any:
        response = self._http.request(method, path, json=json)
        if response.status_code == 404 and allow_404:
            return None
        if response.status_code >= 400:
            raise PublisherError(
                f"GitHub API {method} {path} -> {response.status_code}: {response.text[:300]}"
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise PublisherError(f"GitHub API 返回了非 JSON 响应：{response.text[:300]}") from exc