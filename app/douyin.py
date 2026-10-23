"""从抖音分享文案解析并下载公开作品。

短链先跳转取出作品编号。详情接口要带 uifid 和 x-secsdk-web-signature，
签名是 md5(uifid_timestamp_salt_query)，salt 来自公开页面脚本。
匿名访问时先向 /ttwid/register 要一个 ttwid，uifid 用首页返回的
x-tt-trace-host。不需要登录。清晰度用 aid=6383 才能拿到 1080p。
下载 CDN 时必须带 Referer: https://www.douyin.com/。
分享页里的 _ROUTER_DATA 只作为旧页面结构的备用。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse

import requests

URL_RE = re.compile(
    r"https?://(?:[A-Za-z0-9-]+\.)*(?:douyin|iesdouyin)\.com/"
    r"[A-Za-z0-9_.?=&%/-]*[A-Za-z0-9_/-]",
    re.IGNORECASE,
)
PATH_ID_RE = re.compile(r"/(?:video|note|slides|share/video|share/note)/(\d+)")
RENDER_DATA_RE = re.compile(
    r'<script[^>]*id=["\']RENDER_DATA["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
ALLOWED_HOSTS = ("douyin.com", "iesdouyin.com")
MEDIA_HOSTS = (
    "douyin.com",
    "iesdouyin.com",
    "douyinvod.com",
    "douyinpic.com",
    "douyincdn.com",
    "snssdk.com",
    "amemv.com",
    "byteimg.com",
    "bytecdn.cn",
    "ibytedtos.com",
    "bytetos.com",
    "pstatp.com",
    "ipstatp.com",
    "zjcdn.com",
)
CHALLENGE_MARKERS = ("waf-jschallenge", "out-sha256.js", "byted_acrawler", "captcha")
PHOTO_TYPES = {2, 68}
RATIOS = ("1080p", "720p", "540p")
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
SEC_SALT = "A96D855A08C0A9707F8BEF0D9A527E4E"
DETAIL_AID = 6383
CACHE_TTL = 180.0
_cache: dict[str, tuple[float, "Post"]] = {}
_cache_lock = threading.Lock()
_web: requests.Session | None = None
_uifid = ""
_web_lock = threading.Lock()


class DouyinError(Exception):
    """用户能直接看懂的失败原因。"""


@dataclass
class Post:
    aweme_id: str
    title: str
    author: str
    is_photo: bool
    cover_url: str = ""
    duration_ms: int = 0
    quality: str = "1080p"
    video_urls: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return {
            "aweme_id": self.aweme_id,
            "title": self.title,
            "author": self.author,
            "is_photo": self.is_photo,
            "duration_ms": self.duration_ms,
            "image_count": len(self.image_urls),
            "cover_path": f"/api/cover/{self.aweme_id}" if self.cover_url else "",
        }


def extract_url(text: str) -> str:
    match = URL_RE.search(text or "")
    if match:
        return match.group(0).rstrip("/")
    bare = re.search(
        r"(?:[A-Za-z0-9-]+\.)*(?:douyin|iesdouyin)\.com/[A-Za-z0-9_/-]+",
        text or "",
        re.IGNORECASE,
    )
    if bare:
        return "https://" + bare.group(0)
    return ""


def _host_ok(url: str, suffixes: tuple[str, ...] = ALLOWED_HOSTS) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved):
        return False
    return any(host == name or host.endswith("." + name) for name in suffixes)


def extract_aweme_id(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    modal = parse_qs(parsed.query).get("modal_id") or parse_qs(parsed.query).get("aweme_id")
    if modal and modal[0].isdigit():
        return modal[0]
    match = PATH_ID_RE.search(parsed.path)
    return match.group(1) if match else ""


def _headers() -> dict[str, str]:
    return {
        "User-Agent": MOBILE_UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.douyin.com/",
    }


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(_headers())
    return session


def _abs(base: str, location: str) -> str:
    return urljoin(base, location.strip())


def _next_link(url: str, timeout: int = 15) -> str:
    """只看一跳重定向或页面里的下一个抖音链接，不把分享页正文读进来。"""
    if not _host_ok(url):
        raise DouyinError("链接跳到了抖音以外的网站，没法继续解析")
    try:
        response = _session().get(url, allow_redirects=False, timeout=timeout, stream=True)
    except requests.RequestException as exc:
        raise DouyinError(f"打开链接失败：{exc}") from exc
    location = response.headers.get("Location") or ""
    if location:
        response.close()
        return _abs(url, location)
    content_type = response.headers.get("Content-Type", "")
    if "html" not in content_type and "text" not in content_type and content_type:
        response.close()
        return ""
    raw = response.content[: 1_000_000]
    response.close()
    text = raw.decode(response.encoding or "utf-8", errors="ignore")
    if any(marker in text for marker in CHALLENGE_MARKERS):
        raise DouyinError("抖音返回了验证页面，等一会儿再试")
    for found in URL_RE.findall(text):
        if found.rstrip("/") != url.rstrip("/"):
            return found
    refresh = re.search(r'url=([^"\'>\s]+)', text, re.IGNORECASE)
    if refresh:
        return _abs(url, refresh.group(1))
    return ""


def resolve_aweme_id(text: str) -> str:
    current = extract_url(text)
    if not current:
        raise DouyinError("没有找到抖音链接。把分享文案或链接整段粘贴进来")
    seen: set[str] = set()
    for _ in range(6):
        if current in seen:
            break
        seen.add(current)
        if not _host_ok(current):
            raise DouyinError("链接跳到了抖音以外的网站，没法继续解析")
        aweme_id = extract_aweme_id(current)
        if aweme_id:
            return aweme_id
        nxt = _next_link(current)
        if not nxt:
            break
        current = nxt
    raise DouyinError("这个链接不是单个作品，主页、直播和音乐页不支持")


def _json_object_at(html: str, start: int) -> dict | None:
    depth = 0
    in_str = False
    escaped = False
    for index in range(start, len(html)):
        char = html[index]
        if in_str:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_str = False
            continue
        if char == '"':
            in_str = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(html[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return data if isinstance(data, dict) else None
    return None


def extract_router_data(html: str) -> dict | None:
    marker = html.find("_ROUTER_DATA")
    if marker >= 0:
        start = html.find("{", marker)
        if start >= 0:
            data = _json_object_at(html, start)
            if data:
                return data
    match = RENDER_DATA_RE.search(html)
    if not match:
        return None
    raw = unquote(match.group(1).strip())
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _video_info(router: dict) -> dict | None:
    loader = router.get("loaderData")
    if isinstance(loader, dict):
        for page in loader.values():
            if isinstance(page, dict) and isinstance(page.get("videoInfoRes"), dict):
                return page["videoInfoRes"]
    return None


def _first_http(value: object) -> str:
    if isinstance(value, str) and value.startswith("http"):
        return value
    if isinstance(value, dict):
        urls = value.get("url_list") or []
        for url in urls:
            if isinstance(url, str) and url.startswith("http"):
                return url
    return ""


def _image_url(image: dict) -> str:
    urls = [url for url in (image.get("url_list") or []) if isinstance(url, str) and url.startswith("http")]
    return urls[-1] if urls else ""


def _append_unique(urls: list[str], url: str) -> None:
    cleaned = url.replace("playwm", "play")
    if cleaned not in urls and _host_ok(cleaned, MEDIA_HOSTS):
        urls.append(cleaned)


def _bitrate_urls(video: dict, quality: str) -> list[str]:
    target = {"1080p": 1080, "720p": 720, "540p": 540}.get(quality, 1080)
    ranked: list[tuple[tuple, list[str]]] = []
    for item in video.get("bit_rate") or []:
        if not isinstance(item, dict):
            continue
        addr = item.get("play_addr") or {}
        found = [url for url in (addr.get("url_list") or []) if isinstance(url, str) and url.startswith("http")]
        if not found:
            continue
        width = addr.get("width") or 0
        height = addr.get("height") or 0
        side = min(width, height) if width and height else 0
        h265 = 1 if item.get("is_h265") else 0
        over = 0 if side <= target + 40 else 1
        ranked.append(((h265, over, -side, -(item.get("bit_rate") or 0)), found))
    urls: list[str] = []
    for _, found in sorted(ranked, key=lambda entry: entry[0]):
        for url in found:
            _append_unique(urls, url)
    return urls


def _play_candidates(item: dict, quality: str) -> list[str]:
    video = item.get("video") or {}
    play_addr = video.get("play_addr") or {}
    uri = play_addr.get("uri") or ""
    prefer = quality if quality in RATIOS else "1080p"
    ratios = (prefer,) + tuple(ratio for ratio in RATIOS if ratio != prefer)
    urls = _bitrate_urls(video, prefer)
    if isinstance(uri, str) and uri and not uri.startswith("http"):
        for ratio in ratios:
            _append_unique(
                urls,
                "https://aweme.snssdk.com/aweme/v1/play/"
                f"?video_id={quote(uri, safe='')}&ratio={ratio}&line=0",
            )
    for key in ("play_addr", "download_addr"):
        for url in (video.get(key) or {}).get("url_list") or []:
            if isinstance(url, str) and url.startswith("http"):
                _append_unique(urls, url)
    return urls


def post_from_item(item: dict, aweme_id: str, quality: str = "1080p") -> Post:
    images = [image for image in (item.get("images") or []) if isinstance(image, dict)]
    image_urls = [url for url in (_image_url(image) for image in images) if url]
    aweme_type = item.get("aweme_type")
    is_photo = aweme_type in PHOTO_TYPES or bool(image_urls)
    video = item.get("video") or {}
    cover = ""
    if is_photo and image_urls:
        cover = image_urls[0]
    else:
        cover = (
            _first_http(video.get("origin_cover"))
            or _first_http(video.get("cover"))
            or _first_http(video.get("dynamic_cover"))
        )
    if cover and not _host_ok(cover, MEDIA_HOSTS):
        cover = ""
    duration = video.get("duration") or 0
    chosen = quality if quality in RATIOS else "1080p"
    return Post(
        aweme_id=str(item.get("aweme_id") or aweme_id),
        title=(item.get("desc") or "").strip(),
        author=((item.get("author") or {}).get("nickname") or "").strip(),
        is_photo=is_photo,
        cover_url=cover,
        duration_ms=int(duration) if isinstance(duration, (int, float)) else 0,
        quality=chosen,
        video_urls=[] if is_photo else _play_candidates(item, chosen),
        image_urls=image_urls if is_photo else [],
    )


def parse_share_html(html: str, aweme_id: str, quality: str = "1080p") -> Post:
    if any(marker in html for marker in CHALLENGE_MARKERS) and "_ROUTER_DATA" not in html:
        raise DouyinError("抖音返回了验证页面，等一会儿再试")
    router = extract_router_data(html)
    if not router:
        raise DouyinError("分享页里没有作品数据，抖音页面结构可能变了")
    info = _video_info(router)
    if not info:
        raise DouyinError("分享页结构变了，没有找到作品信息")
    items = info.get("item_list") or []
    if not items:
        filtered = info.get("filter_list") or []
        if filtered and isinstance(filtered[0], dict):
            reason = filtered[0].get("detail_msg") or filtered[0].get("notice") or "作品不可用"
            raise DouyinError(f"这个作品打不开：{reason}")
        raise DouyinError("作品不存在，或已被删除、设为私密")
    item = items[0]
    if not isinstance(item, dict):
        raise DouyinError("分享页里的作品数据无法识别")
    return post_from_item(item, aweme_id, quality)


def signed_params(params: dict, uifid: str, timestamp: str) -> dict:
    query = {**params, "uifid": uifid, "timestamp": timestamp}
    encoded = urlencode(query)
    signature = hashlib.md5(f"{uifid}_{timestamp}_{SEC_SALT}_{encoded}".encode()).hexdigest()
    return {**query, "x-secsdk-web-signature": signature}


def _reset_web() -> None:
    global _web, _uifid
    _web = None
    _uifid = ""


def _ensure_web() -> tuple[requests.Session, str]:
    global _web, _uifid
    with _web_lock:
        if _web is not None and _uifid:
            return _web, _uifid
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": DESKTOP_UA,
                "Referer": "https://www.douyin.com/",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        session.cookies.set("passport_csrf_token", uuid.uuid4().hex, domain=".douyin.com")
        try:
            registered = session.post(
                "https://www.douyin.com/ttwid/register",
                json={"aid": DETAIL_AID, "service": "www.douyin.com"},
                timeout=20,
            )
            registered.raise_for_status()
            home = session.get("https://www.douyin.com/", timeout=20)
            home.raise_for_status()
        except requests.RequestException as exc:
            raise DouyinError(f"连接抖音失败：{exc}") from exc
        if not any(cookie.name == "ttwid" for cookie in session.cookies):
            raise DouyinError("抖音没有发访问凭证，稍后再试")
        uifid = ""
        for cookie in session.cookies:
            if "uifid" in cookie.name.lower() and cookie.value:
                uifid = cookie.value
                break
        if not uifid:
            uifid = home.headers.get("x-tt-trace-host") or ""
        if not uifid:
            raise DouyinError("抖音没有返回签名参数，稍后再试")
        _web, _uifid = session, uifid
        return session, uifid


def fetch_detail(aweme_id: str) -> dict:
    last_error = "这个作品打不开"
    for attempt in range(2):
        session, uifid = _ensure_web()
        params = signed_params(
            {"aweme_id": aweme_id, "aid": DETAIL_AID},
            uifid,
            str(int(time.time())),
        )
        url = "https://www.douyin.com/aweme/v1/web/aweme/detail/?" + urlencode(params)
        try:
            response = session.get(url, timeout=20)
        except requests.RequestException as exc:
            last_error = f"读取作品信息失败：{exc}"
            _reset_web()
            continue
        if response.status_code == 403:
            last_error = "抖音拒绝了这次请求，稍后再试"
            _reset_web()
            continue
        try:
            data = response.json()
        except ValueError:
            last_error = "作品信息不是有效数据"
            if attempt == 0:
                _reset_web()
                continue
            break
        detail = data.get("aweme_detail") if isinstance(data, dict) else None
        if isinstance(detail, dict) and (detail.get("aweme_id") or detail.get("video") or detail.get("images")):
            return detail
        raise DouyinError("这个作品打不开，可能已删除或设为私密")
    raise DouyinError(last_error)


def _fetch_share_post(aweme_id: str, quality: str) -> Post:
    url = f"https://www.iesdouyin.com/share/note/{aweme_id}"
    try:
        response = _session().get(url, timeout=20)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise DouyinError(f"打开分享页失败：{exc}") from exc
    return parse_share_html(response.text, aweme_id, quality)


def fetch_post(aweme_id: str, quality: str = "1080p", force: bool = False) -> Post:
    if not aweme_id.isdigit():
        raise DouyinError("作品编号不正确")
    chosen = quality if quality in RATIOS else "1080p"
    now = time.monotonic()
    if not force:
        with _cache_lock:
            cached = _cache.get(aweme_id)
            if cached and now - cached[0] < CACHE_TTL and _quality_matches(cached[1], chosen):
                return cached[1]
    try:
        post = post_from_item(fetch_detail(aweme_id), aweme_id, chosen)
    except DouyinError as first_error:
        try:
            post = _fetch_share_post(aweme_id, chosen)
        except DouyinError:
            raise first_error
    if post.is_photo and not post.image_urls:
        raise DouyinError("这个图集没有图片")
    if not post.is_photo and not post.video_urls:
        raise DouyinError("这个作品没有可下载的视频")
    with _cache_lock:
        _cache[aweme_id] = (time.monotonic(), post)
    return post


def _quality_matches(post: Post, quality: str) -> bool:
    if post.is_photo:
        return True
    return post.quality == (quality if quality in RATIOS else "1080p")


def parse_text(text: str, quality: str = "1080p") -> Post:
    if not text or not text.strip():
        raise DouyinError("先粘贴抖音分享链接")
    if len(text) > 4000:
        raise DouyinError("粘贴的内容太长了")
    return fetch_post(resolve_aweme_id(text), quality)


def _looks_like_media(response: requests.Response, photo: bool = False) -> bool:
    if response.status_code != 200:
        return False
    if not _host_ok(response.url, MEDIA_HOSTS):
        return False
    content_type = (response.headers.get("Content-Type") or "").lower()
    if "json" in content_type or "html" in content_type or "text/" in content_type:
        return False
    if photo:
        return "image/" in content_type or "octet-stream" in content_type or not content_type
    if content_type.startswith("video/") or "octet-stream" in content_type or not content_type:
        return True
    return False


@dataclass
class VideoDownload:
    response: requests.Response
    first: bytes

    @property
    def content_length(self) -> str:
        return self.response.headers.get("Content-Length") or ""

    def __iter__(self):
        try:
            if self.first:
                yield self.first
            for chunk in self.response.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            self.response.close()


def open_video(post: Post) -> VideoDownload:
    if not post.video_urls:
        raise DouyinError("这个作品没有可下载的视频")
    last_status = ""
    for url in post.video_urls:
        if not _host_ok(url, MEDIA_HOSTS):
            last_status = "视频地址不在抖音下载域名上"
            continue
        try:
            response = _ensure_web()[0].get(url, timeout=(15, 120), stream=True, allow_redirects=True)
        except requests.RequestException as exc:
            last_status = str(exc)
            continue
        if not _looks_like_media(response):
            host = urlparse(response.url).hostname or ""
            last_status = f"HTTP {response.status_code} {response.headers.get('Content-Type', '')} {host}".strip()
            response.close()
            continue
        first = next(response.iter_content(chunk_size=64 * 1024), b"")
        if b"ftyp" in first[:64] or first.startswith(b"\x1aE\xdf\xa3"):
            return VideoDownload(response, first)
        last_status = "返回的内容不是视频文件"
        response.close()
    raise DouyinError(f"视频地址失效了，稍后再试。{last_status}".strip())


def fetch_cover(post: Post) -> tuple[bytes, str]:
    if not post.cover_url or not _host_ok(post.cover_url, MEDIA_HOSTS):
        raise DouyinError("没有封面")
    try:
        response = _ensure_web()[0].get(post.cover_url, timeout=20)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise DouyinError("封面加载失败") from exc
    media = response.headers.get("Content-Type", "image/jpeg")
    if "image/" not in media.lower():
        raise DouyinError("封面加载失败")
    return response.content, media


def download_images(post: Post) -> list[tuple[str, bytes]]:
    if not post.image_urls:
        raise DouyinError("这个图集没有图片")
    files: list[tuple[str, bytes]] = []
    session = _ensure_web()[0]
    for index, url in enumerate(post.image_urls, start=1):
        if not _host_ok(url, MEDIA_HOSTS):
            raise DouyinError(f"第 {index} 张图片地址不在抖音图床上")
        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise DouyinError(f"第 {index} 张图片下载失败：{exc}") from exc
        if not _looks_like_media(response, photo=True):
            raise DouyinError(f"第 {index} 张图片地址失效了")
        ext = ".png" if "png" in (response.headers.get("Content-Type") or "") else ".jpg"
        files.append((f"{index:02d}{ext}", response.content))
    return files


def safe_name(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", value or "").strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:40]


def filename_for(post: Post, ext: str) -> str:
    parts = [part for part in (safe_name(post.author), safe_name(post.title)) if part]
    stem = "_".join(parts) if parts else "douyin"
    return f"{stem}_{post.aweme_id}.{ext.lstrip('.')}"


def content_disposition(name: str) -> str:
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "douyin.mp4"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


def save_to(post: Post, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if post.is_photo:
        import io
        import zipfile

        target = directory / filename_for(post, "zip")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, data in download_images(post):
                archive.writestr(name, data)
        target.write_bytes(buffer.getvalue())
        return target

    target = directory / filename_for(post, "mp4")
    with target.open("wb") as handle:
        for chunk in open_video(post):
            handle.write(chunk)
    if target.stat().st_size < 1024:
        target.unlink(missing_ok=True)
        raise DouyinError("下载到的文件太小，不像是视频")
    return target


def save_text(text: str, directory: str | Path = "downloads", quality: str = "1080p") -> Path:
    post = parse_text(text, quality)
    return save_to(post, Path(directory))
