"""Fetch a shared web article (WeChat 公众号 first) behind a strict host allowlist.

This is the only code in LinkMind that touches the open web, and the OpenClaw agent has no web or shell
tools of its own, so every outbound request goes through these checks:
- https only, default port, no credentials in the URL, host must be on the allowlist (exact match);
- the host must resolve to a public address (or a fake-IP proxy address, see below), never loopback/LAN;
- redirects are followed by hand and every hop is checked the same way;
- the response must be HTML and is capped in size.
The fetched text is stored as data; it is never executed or treated as instructions."""

import codecs
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode

import httpx
from bs4 import BeautifulSoup, Tag

from .config import settings

MAX_BYTES = 8_000_000
MAX_REDIRECTS = 3
TIMEOUT = httpx.Timeout(20.0, connect=8.0)
MAX_PARAGRAPHS = 2_000
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)
# Clash / Surge / sing-box "fake-IP" DNS answers come from the RFC 2544 benchmark range. The user's Mac
# runs such a proxy, so every public host resolves there; the proxy then connects to the real address.
FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")
URL_RE = re.compile(r"https?://[^\s<>\"'，。、；！？「」【】（）]+", re.IGNORECASE)
_WECHAT_ARTICLE_KEYS = ("__biz", "mid", "idx", "sn")
_BLOCK_TAGS = ["p", "section", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "figcaption", "pre"]


class FetchError(ValueError):
    pass


@dataclass
class Article:
    url: str  # canonical
    title: str | None
    account: str | None  # 公众号 name
    author: str | None
    published_at: str | None  # ISO 8601
    description: str | None
    paragraphs: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(self.paragraphs)


def allowed_hosts() -> set[str]:
    return {h.strip().lower() for h in settings().linkmind_fetch_hosts.split(",") if h.strip()}


def extract_urls(text: str) -> list[str]:
    return [m.rstrip(".,;:!?)]}") for m in URL_RE.findall(text or "")]


def check_url(url: str | httpx.URL) -> httpx.URL:
    try:
        u = httpx.URL(str(url))
    except Exception as e:
        raise FetchError(f"不是有效的链接：{url}") from e
    if u.scheme != "https":
        raise FetchError("只支持 https 链接")
    if u.userinfo:
        raise FetchError("链接里不能带用户名或密码")
    if u.port not in (None, 443):
        raise FetchError("只支持默认端口的链接")
    host = (u.host or "").lower().rstrip(".")
    if host not in allowed_hosts():
        raise FetchError(f"暂不支持抓取 {host or url}（目前只支持：{'、'.join(sorted(allowed_hosts()))}）")
    return u


def _resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]


def check_resolves_public(host: str) -> None:
    try:
        addrs = _resolve(host)
    except OSError as e:
        raise FetchError(f"无法解析域名 {host}") from e
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not (ip.is_global or ip in FAKE_IP_NET):
            raise FetchError(f"{host} 解析到了非公网地址 {ip}，已拒绝")


def canonical_url(url: str | httpx.URL) -> str:
    """Stable identity for dedupe: WeChat articles keep only the params that identify the article."""
    u = httpx.URL(str(url))
    host = (u.host or "").lower()
    if host == "mp.weixin.qq.com" and u.path == "/s":
        q = dict(parse_qsl(u.query.decode()))
        kept = {k: q[k] for k in _WECHAT_ARTICLE_KEYS if k in q}
        return f"https://{host}/s?{urlencode(kept)}" if kept else f"https://{host}/s"
    if host == "mp.weixin.qq.com" and u.path.startswith("/s/"):
        return f"https://{host}{u.path}"
    return str(u.copy_with(fragment=None))


def _charset(content_type: str) -> str:
    # mp.weixin.qq.com sends Content-Type twice; httpx joins them, so parse the first charset ourselves.
    if m := re.search(r"charset=([\w.-]+)", content_type, re.IGNORECASE):
        try:
            return codecs.lookup(m.group(1)).name
        except LookupError:
            pass
    return "utf-8"


def _download(url: httpx.URL, transport: httpx.BaseTransport | None = None) -> tuple[httpx.URL, str]:
    with httpx.Client(
        timeout=TIMEOUT, follow_redirects=False, trust_env=False, transport=transport, headers={"User-Agent": UA}
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            url = check_url(url)
            if transport is None:
                check_resolves_public(url.host)
            with client.stream("GET", url) as resp:
                if resp.is_redirect:
                    url = url.join(resp.headers.get("location", ""))
                    continue
                if resp.status_code != 200:
                    raise FetchError(f"网页返回 HTTP {resp.status_code}")
                ctype = resp.headers.get("content-type", "")
                if "text/html" not in ctype:
                    raise FetchError(f"不是网页（{ctype or '未知类型'}）")
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise FetchError("网页太大，已放弃")
                return url, body.decode(_charset(ctype), errors="replace")
        raise FetchError("跳转次数过多")


def _meta(soup: BeautifulSoup, **attrs) -> str | None:
    tag = soup.find("meta", attrs=attrs)
    val = tag.get("content") if isinstance(tag, Tag) else None
    return val.strip() if isinstance(val, str) and val.strip() else None


def _text(tag) -> str | None:
    if not isinstance(tag, Tag):
        return None
    t = " ".join(tag.get_text(" ", strip=True).split())
    return t or None


def _paragraphs(content: Tag) -> list[str]:
    for t in content.find_all(["script", "style", "noscript"]):
        t.decompose()
    for br in content.find_all("br"):
        br.replace_with("\n")
    out: list[str] = []
    for el in content.find_all(_BLOCK_TAGS):
        if el.find(_BLOCK_TAGS):  # only leaf blocks, so nested sections are not repeated
            continue
        for line in el.get_text("", strip=False).split("\n"):
            line = " ".join(line.split())
            if line and (not out or out[-1] != line):
                out.append(line)
        if len(out) >= MAX_PARAGRAPHS:
            break
    if not out:  # flat markup without block tags
        out = [ln for ln in (" ".join(x.split()) for x in content.get_text("\n").split("\n")) if ln]
    return out[:MAX_PARAGRAPHS]


def parse_wechat_article(html: str, url: str) -> Article:
    soup = BeautifulSoup(html, "html.parser")
    content = soup.find(id="js_content")
    if not isinstance(content, Tag):
        raise FetchError("没有拿到正文：文章可能已删除、需要登录，或触发了微信的访问验证")
    published = None
    if m := re.search(r'var\s+create_time\s*=\s*"(\d{9,11})"', html) or re.search(r'var\s+ct\s*=\s*"(\d{9,11})"', html):
        published = datetime.fromtimestamp(int(m.group(1)), UTC).isoformat()
    account = _text(soup.find(id="js_name"))
    if not account and (m := re.search(r'var\s+nickname\s*=\s*htmlDecode\("([^"]*)"\)', html)):
        account = m.group(1).strip() or None
    paragraphs = _paragraphs(content)
    if not paragraphs:
        raise FetchError("正文为空：可能是纯图片或视频文章")
    return Article(
        url=canonical_url(_meta(soup, property="og:url") or url),
        title=_meta(soup, property="og:title") or _text(soup.find(id="activity-name")),
        account=account,
        author=_meta(soup, name="author"),
        published_at=published,
        description=_meta(soup, property="og:description"),
        paragraphs=paragraphs,
    )


def fetch_article(url: str, transport: httpx.BaseTransport | None = None) -> Article:
    try:
        final_url, html = _download(check_url(url), transport=transport)
    except httpx.TimeoutException as e:
        raise FetchError("网页响应超时") from e
    except httpx.HTTPError as e:
        raise FetchError(f"网络错误（{type(e).__name__}）") from e
    return parse_wechat_article(html, str(final_url))
