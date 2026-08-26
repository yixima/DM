"""robots.txt の確認。

相手サイトが自動アクセスを禁止しているパスには送信しない。
取得できない・書かれていない場合は「許可」とみなす（robots.txt は禁止の宣言であって、
存在しないことは禁止を意味しないため）。
"""
from __future__ import annotations

import urllib.error
import urllib.request
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

_CACHE: dict[str, RobotFileParser | None] = {}

DEFAULT_UA = "Mozilla/5.0 (compatible; DMOutreachBot/0.1)"


def _fetch(robots_url: str, timeout: float = 10.0) -> str | None:
    request = urllib.request.Request(robots_url, headers={"User-Agent": DEFAULT_UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            raw = response.read(512_000)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return None
    for encoding in ("utf-8", "cp932", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def allowed(url: str, user_agent: str = DEFAULT_UA, timeout: float = 10.0) -> tuple[bool, str]:
    """(送信してよいか, 理由)。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False, "URLが不正"

    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in _CACHE:
        body = _fetch(f"{origin}/robots.txt", timeout=timeout)
        if body is None:
            _CACHE[origin] = None
        else:
            parser = RobotFileParser()
            parser.parse(body.splitlines())
            _CACHE[origin] = parser

    parser = _CACHE[origin]
    if parser is None:
        return True, "robots.txt なし"
    if parser.can_fetch(user_agent, url):
        return True, "robots.txt で許可"
    return False, "robots.txt で禁止されたパス"


def clear_cache() -> None:
    _CACHE.clear()
