"""GitHub 仓库回写测试：不碰真实网络，用 MockTransport 断言提交协议。

为什么值得单独测：这是唯一会修改用户仓库的渠道。文件路径、分支、commit 的
parent、PR 的 head/base 任何一个写错，都会把文档提交到错误的位置。
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.config import Settings
from app.publishers.base import DocumentPayload, RunDigest, PublisherError
from app.publishers.github_repo import GitHubRepoPublisher, document_path, git_blob_sha

DOC = DocumentPayload(
    document_id="doc-1",
    doc_type="class_diagram",
    target_key="module:src",
    title="src 模块类图",
    content="@startuml\ntitle 计算器类图\nclass Calculator {\n  +add(a: int, b: int): int\n}\n@enduml",
    content_format="plantuml",
    status="published",
    action="updated",
    version=2,
)
API = DocumentPayload(
    document_id="doc-2",
    doc_type="api_doc",
    target_key="api:calculator",
    title="计算器 API",
    content="## src.calculator\n\n### Calculator.add\n\n- 签名：`add(a, b)`\n- 说明：返回两数之和。",
    content_format="markdown",
    status="published",
    action="updated",
    version=1,
)


def digest() -> RunDigest:
    return RunDigest(
        run_id="run-12345678",
        project_id="project-1",
        project_slug="acme-demo",
        project_name="Demo",
        repo_full_name="acme/demo",
        trigger_type="push",
        trigger_ref="refs/heads/main",
        base_sha="a" * 40,
        head_sha="b" * 40,
        documents=(DOC, API),
        review_url="http://localhost:8000/ui/#project=project-1",
    )


def settings(**overrides) -> Settings:
    values = dict(
        _env_file=None,
        github_token="test-token",
        github_api_base="https://api.github.com",
        github_docs_branch="livingdoc/docs",
        github_docs_dir="docs/livingdoc",
        github_docs_open_pr=True,
        publish_targets="github_repo",
    )
    values.update(overrides)
    return Settings(**values)


class FakeGitHub:
    def __init__(self, *, existing: dict[str, str] | None = None, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.blobs: list[str] = []
        self.tree_entries: list[dict] = []
        self.commit_body: dict = {}
        self.ref_body: dict = {}
        self.pr_body: dict = {}
        self.existing = existing or {}
        self.fail = fail

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        self.calls.append((method, path))
        if self.fail:
            return httpx.Response(500, text="boom")

        if method == "GET" and path == "/repos/acme/demo/git/ref/heads/livingdoc/docs":
            return httpx.Response(404)
        if method == "GET" and path == "/repos/acme/demo":
            return httpx.Response(200, json={"default_branch": "main"})
        if method == "GET" and path == "/repos/acme/demo/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "basecommit"}})
        if method == "POST" and path == "/repos/acme/demo/git/refs":
            self.ref_body = json.loads(request.content)
            return httpx.Response(201, json={"ref": "refs/heads/livingdoc/docs"})
        if method == "GET" and path == "/repos/acme/demo/git/commits/basecommit":
            return httpx.Response(200, json={"tree": {"sha": "basetree"}})
        if method == "GET" and path == "/repos/acme/demo/git/trees/basetree":
            assert request.url.params.get("recursive") == "1"
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"path": path, "type": "blob", "sha": sha}
                        for path, sha in self.existing.items()
                    ],
                    "truncated": False,
                },
            )
        if method == "POST" and path == "/repos/acme/demo/git/blobs":
            body = json.loads(request.content)
            self.blobs.append(base64.b64decode(body["content"]).decode("utf-8"))
            return httpx.Response(200, json={"sha": f"blob-{len(self.blobs)}"})
        if method == "POST" and path == "/repos/acme/demo/git/trees":
            body = json.loads(request.content)
            self.tree_entries = body["tree"]
            assert body["base_tree"] == "basetree"
            return httpx.Response(201, json={"sha": "newtree"})
        if method == "POST" and path == "/repos/acme/demo/git/commits":
            self.commit_body = json.loads(request.content)
            return httpx.Response(201, json={"sha": "newcommit"})
        if method == "PATCH" and path.startswith("/repos/acme/demo/git/refs/heads/"):
            self.ref_body = json.loads(request.content)
            return httpx.Response(200, json={"ref": path.removeprefix("/repos/acme/demo/git/")})
        if method == "GET" and path == "/repos/acme/demo/pulls":
            return httpx.Response(200, json=[])
        if method == "POST" and path == "/repos/acme/demo/pulls":
            self.pr_body = json.loads(request.content)
            return httpx.Response(
                201, json={"html_url": "https://github.com/acme/demo/pull/9"}
            )
        return httpx.Response(404, text=f"unexpected {method} {path}")


def test_publishes_all_files_in_one_commit_and_opens_pr():
    fake = FakeGitHub()
    publisher = GitHubRepoPublisher(transport=httpx.MockTransport(fake))

    results = publisher.publish(digest(), settings())

    assert len(results) == 1
    assert results[0].status == "success"
    assert results[0].external_id == "newcommit"
    assert results[0].external_url == "https://github.com/acme/demo/pull/9"

    paths = {entry["path"] for entry in fake.tree_entries}
    assert document_path("docs/livingdoc", DOC) in paths
    assert document_path("docs/livingdoc", API) in paths
    assert "docs/livingdoc/README.md" in paths
    assert len(fake.blobs) == 3
    assert any("计算器类图" in text for text in fake.blobs)
    assert any("# LivingDoc 活文档" in text for text in fake.blobs)

    assert fake.commit_body["parents"] == ["basecommit"]
    assert "LivingDoc 活文档" in fake.commit_body["message"]
    assert fake.ref_body == {"sha": "newcommit", "force": False}
    assert fake.pr_body["head"] == "livingdoc/docs"
    assert fake.pr_body["base"] == "main"


def test_identical_files_produce_no_commit():
    fake = FakeGitHub()
    # README 由 publisher 生成，直接从一次正常渲染里抄出来，避免手写两份。
    from app.publishers.github_repo import _render_files

    rendered = _render_files(digest(), "docs/livingdoc")
    fake.existing = {path: git_blob_sha(data) for path, data in rendered.items()}

    publisher = GitHubRepoPublisher(transport=httpx.MockTransport(fake))
    results = publisher.publish(digest(), settings())

    assert results[0].status == "success"
    assert results[0].detail["no_changes"] is True
    assert not any(path.endswith("/git/blobs") for _, path in fake.calls)
    assert not any(path.endswith("/git/commits") and method == "POST" for method, path in fake.calls)


def test_missing_token_is_not_available():
    assert GitHubRepoPublisher().available(settings(github_token="")) is False
    assert GitHubRepoPublisher().available(settings()) is True


def test_api_failure_becomes_publisher_error():
    publisher = GitHubRepoPublisher(transport=httpx.MockTransport(FakeGitHub(fail=True)))
    with pytest.raises(PublisherError, match="500"):
        publisher.publish(digest(), settings())


def test_direct_main_does_not_open_pr():
    fake = FakeGitHub()
    publisher = GitHubRepoPublisher(transport=httpx.MockTransport(fake))
    results = publisher.publish(
        digest(),
        settings(github_docs_branch="main", github_docs_open_pr=True),
    )
    # 直接同步 main 时 branch == default_branch，不应调用 /pulls。
    assert not any(path.endswith("/pulls") for _, path in fake.calls)
    assert results[0].status == "success"