"""扫描待上传目录里的密钥，避免把 token 推到 GitHub。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERNS = [
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"LTAI[0-9A-Za-z]{12,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]

TEXT_SUFFIXES = {
    ".py", ".md", ".js", ".css", ".html", ".yml", ".yaml", ".ini",
    ".ps1", ".mmd", ".txt", ".toml", ".cfg", ".json", ".env", ".example",
}


def scan(root: Path) -> list[str]:
    hits: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {".env", ".env.example", ".gitignore"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for pattern in PATTERNS:
                if pattern.search(line):
                    hits.append(f"{path.relative_to(root)}:{number}: {line.strip()[:120]}")
                    break
    return hits


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    hits = scan(root)
    print(f"扫描目录: {root.resolve()}")
    print(f"命中 {len(hits)} 处")
    for hit in hits:
        print("  " + hit)
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())