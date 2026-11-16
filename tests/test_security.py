"""签名验证的单测。

这一类安全代码，宁可多写五个用例，也不要相信"看起来对"。
"""

from __future__ import annotations

import pytest

from app.security import sign_github_payload, verify_github_signature

SECRET = "test-secret"
BODY = b'{"hello":"world"}'


def test_valid_sha256_signature_passes() -> None:
    header = sign_github_payload(SECRET, BODY, "sha256")
    assert verify_github_signature(SECRET, BODY, header) is True


def test_valid_sha1_signature_passes() -> None:
    # GitHub 的旧头 X-Hub-Signature 用的是 sha1，仍然要兼容
    header = sign_github_payload(SECRET, BODY, "sha1")
    assert verify_github_signature(SECRET, BODY, header) is True


def test_tampered_body_fails() -> None:
    header = sign_github_payload(SECRET, BODY, "sha256")
    assert verify_github_signature(SECRET, b'{"hello":"WORLD"}', header) is False


def test_wrong_secret_fails() -> None:
    header = sign_github_payload("another-secret", BODY, "sha256")
    assert verify_github_signature(SECRET, BODY, header) is False


def test_missing_header_fails() -> None:
    assert verify_github_signature(SECRET, BODY, None) is False
    assert verify_github_signature(SECRET, BODY, "") is False


@pytest.mark.parametrize(
    "header",
    [
        "deadbeef",                    # 没有 "=" 前缀
        "sha256=",                     # 摘要为空
        "md5=abc123",                  # 不支持的算法
        "sha256=not-a-hex-string",     # 长度不对
    ],
)
def test_malformed_header_fails(header: str) -> None:
    assert verify_github_signature(SECRET, BODY, header) is False


def test_uppercase_hex_is_accepted() -> None:
    # 十六进制大小写不敏感（GitHub 一直发小写，但防御性处理没坏处）
    header = sign_github_payload(SECRET, BODY, "sha256").replace("sha256=", "SHA256=")
    assert verify_github_signature(SECRET, BODY, header) is True


def test_empty_secret_refuses_everything() -> None:
    # 配置漏了 secret 时必须拒绝，而不是"空密钥也能算出一个签名"
    header = sign_github_payload("", BODY, "sha256")
    assert verify_github_signature("", BODY, header) is False


def test_reformatted_json_fails() -> None:
    """这条解释了为什么必须 await request.body() 而不能用 payload 重新 dumps。"""
    header = sign_github_payload(SECRET, BODY, "sha256")
    reformatted = b'{"hello": "world"}'  # 只多了一个空格
    assert verify_github_signature(SECRET, reformatted, header) is False
