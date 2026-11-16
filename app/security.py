"""Webhook 签名验证：整个系统唯一的安全边界，所以单独成文件、单独单测。

GitHub 的规则（官方文档 "Validating webhooks payloads"）：
    HMAC-SHA256(secret, 原始请求体) 的十六进制摘要，
    放在请求头 X-Hub-Signature-256 里，形如 "sha256=abc123..."。
旧版本用 X-Hub-Signature + SHA1，两个都要认。

三个必须遵守的细节（AI 生成代码最常翻车的地方）：
1. 必须对**原始字节**计算。先把 body 解析成 JSON 再 dumps 回去，键序、空格、
   Unicode 转义都会变，摘要必然对不上 —— 所以要先 await request.body()。
2. 必须用 hmac.compare_digest 而不是 ==。== 是短路比较，攻击者可以用响应
   时间差逐字节猜出签名。这是计时攻击（timing attack）的教科书案例。
3. 请求头里的 "sha256=" 前缀要先剥掉再比，不能直接拿整个头做比较。
"""

from __future__ import annotations

import hashlib
import hmac

# 支持多种摘要算法：GitHub 新旧两套头、Gitee 也走 sha 系列
_SUPPORTED = {
    "sha256": hashlib.sha256,
    "sha1": hashlib.sha1,
}


def _parse_header(header: str) -> tuple[str, str] | None:
    """把 "sha256=abcdef" 拆成 (算法, 十六进制摘要)；格式不对返回 None。"""
    if not header:
        return None
    algo, sep, digest = header.partition("=")
    if not sep or not digest:
        return None
    algo = algo.strip().lower()
    digest = digest.strip().lower()
    return (algo, digest) if algo in _SUPPORTED else None


def verify_github_signature(secret: str, body: bytes, header: str | None) -> bool:
    """校验 GitHub Webhook 签名。任何异常情况一律返回 False（fail-closed）。"""
    if not secret or header is None:
        return False
    parsed = _parse_header(header)
    if parsed is None:
        return False
    algo, received = parsed
    expected = hmac.new(secret.encode("utf-8"), body, _SUPPORTED[algo]).hexdigest()
    # compare_digest 必须是 str 对 str、且长度可能不同也不会抛异常
    return hmac.compare_digest(expected, received)


def sign_github_payload(secret: str, body: bytes, algo: str = "sha256") -> str:
    """生成签名。测试和本地模拟脚本用，生产代码不会调用它。"""
    digest = hmac.new(secret.encode("utf-8"), body, _SUPPORTED[algo]).hexdigest()
    return f"{algo}={digest}"
