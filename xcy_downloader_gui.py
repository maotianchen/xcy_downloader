# -*- coding: utf-8 -*-
"""
星次元下载器 (Desktop GUI)

核心策略：
  1. 从 app.log 中解析 JSON 上下文与正则兜底，建立「剧集号 ↔ 书名 ↔ 链接」及图片映射
  2. 支持本地日志自动提取 access_token、手动输入 Token 或通过 ADB 一键从设备/虚拟机拉取 Token 与日志
  3. Token 验证后可通过 API 直连发现书籍并补全全集，支持按系列与集数范围筛选
  4. 并发分片下载 M3U8（支持多密钥轮换/相对路径解析/分片重试/完整度校验/AES-128-CBC 解密），并调用 ffmpeg 无损转封装
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import queue
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from tkinter import ttk, filedialog, messagebox, scrolledtext
from typing import Callable, Dict, List, Optional, Set, Tuple
import tkinter as tk

try:
    import requests  # type: ignore
except ImportError:
    requests = None

try:
    import certifi  # type: ignore
except ImportError:
    certifi = None

try:
    from Crypto.Cipher import AES  # type: ignore
except ImportError:
    AES = None

# ── API 与配置常量 ──
API_BASE = "https://service.meother.com/qmyz-server-provider"
REMOTE_LOG_PATH = "/storage/emulated/0/Android/data/com.meother.qmyz/files/logs/app.log"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".xcy_downloader_config.json")
ANDROID_UA = "Mozilla/5.0 (Linux; Android 9; OnePlus) AppleWebKit/537.36"
DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

M3U8_FALLBACK_RE = re.compile(r'https?://[^\s"\'<>]+?\.m3u8(?:\?[^\s"\'<>]*)?', re.IGNORECASE)
IMAGE_FALLBACK_RE = re.compile(
    r'https?://[^\s"\'<>]+?\.(?:png|jpg|jpeg|gif|webp)(?:\?[^\s"\'<>]*)?',
    re.IGNORECASE,
)
IMAGE_URL_HINT_RE = re.compile(r'\.(?:png|jpg|jpeg|gif|webp)(?:$|\?)', re.IGNORECASE)
MEDIA_URL_HINT_RE = re.compile(r'\.(?:m3u8|ts|mp4|m4a|aac|mp3)(?:$|\?)', re.IGNORECASE)
TOKEN_RE = re.compile(r'"access_token"\s*:\s*"([^"]+)"')
TOKEN_BYTES_RE = re.compile(rb'"access_token"\s*:\s*"([^"]+)"')


# ──────────────────────────────────────────────
#  数据结构
# ──────────────────────────────────────────────

@dataclass
class EpisodeInfo:
    """单个剧集的完整信息"""
    book_title: str = ""
    book_id: str = ""
    episode_id: str = ""
    episode_num: int = 0
    title: str = ""
    oss_audio_url: str = ""
    duration: int = 0
    free_flag: int = 1
    weight: int = 0
    line_no: int = 0

    @property
    def display_name(self) -> str:
        return f"{self.title}"

    @property
    def save_filename(self) -> str:
        raw = f"{self.book_title} - {self.title}" if self.book_title else self.title
        ext = infer_media_ext(self.oss_audio_url, source="chapter")
        return sanitize_filename(raw) + ext


@dataclass
class SeriesInfo:
    """一个系列/一本书的汇总"""
    book_title: str = ""
    book_id: str = ""
    chapters: List[EpisodeInfo] = field(default_factory=list)

    @property
    def series_key(self) -> str:
        return self.book_id if self.book_id else f"title:{self.book_title}"

    @property
    def count(self) -> int:
        return len(self.chapters)

    @property
    def min_episode(self) -> int:
        return min((c.episode_num for c in self.chapters if c.episode_num > 0), default=0)

    @property
    def max_episode(self) -> int:
        return max((c.episode_num for c in self.chapters if c.episode_num > 0), default=0)

    @property
    def arc_breakdown(self) -> List[Tuple[str, int, int, int]]:
        """按篇名分组，返回 [(篇名, 起始集, 结束集, 集数), …]"""
        arcs: OrderedDict[str, List[int]] = OrderedDict()
        for ch in self.chapters:
            title = ch.title or ""
            m = re.match(r'(.+?篇)\s*第', title)
            arc_name = m.group(1) if m else "其他"
            arcs.setdefault(arc_name, []).append(ch.episode_num)
        result = []
        for name, nums in arcs.items():
            filtered = [n for n in nums if n > 0]
            if filtered:
                result.append((name, min(filtered), max(filtered), len(filtered)))
            else:
                result.append((name, 0, 0, len(nums)))
        return result

    @property
    def episode_range(self) -> str:
        if self.count == 0:
            return "（无剧集信息）"
        breakdown = self.arc_breakdown
        if not breakdown:
            return f"共{self.count}集"
        parts = []
        for name, lo, hi, cnt in breakdown:
            if lo and hi:
                parts.append(f"{name} 第{lo}~{hi}集({cnt}集)")
            else:
                parts.append(f"{name}({cnt}集)")
        display = " | ".join(parts)
        total = f" 共{self.count}集"
        if len(display) > 100:
            display = f"{parts[0]} | … | {parts[-1]}"
        return display + total


@dataclass
class M3u8Segment:
    """M3U8 单个分片及其对应的解密状态"""
    url: str
    seq: int
    key_method: Optional[str] = None
    key_url: Optional[str] = None
    iv: Optional[bytes] = None


@dataclass
class M3u8Playlist:
    """解析后的 M3U8 播放列表"""
    final_url: str
    media_sequence: int
    segments: List[M3u8Segment]


# ──────────────────────────────────────────────
#  工具函数与网络会话
# ──────────────────────────────────────────────

_thread_local = threading.local()


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """将非法文件名字符替换为下划线，并在超长时保留尾部序号防止剧集重名覆盖。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', '_', (name or "").strip()).strip(". ")
    if not cleaned:
        cleaned = "unknown"
    if len(cleaned) <= max_len:
        return cleaned
    digest = hashlib.sha1(cleaned.encode("utf-8", errors="ignore")).hexdigest()[:6]
    tail_len = min(40, max_len // 3)
    head_len = max_len - tail_len - len(digest) - 2
    if head_len <= 0:
        return cleaned[:max_len]
    return f"{cleaned[:head_len]}_{digest}_{cleaned[-tail_len:]}"


def infer_media_ext(url: str, source: str = "") -> str:
    """根据 URL 和来源推断默认音视频后缀。"""
    path_lower = urllib.parse.urlparse(url or "").path.lower()
    for direct_ext in (".mp3", ".aac", ".m4a", ".mp4", ".ts"):
        if path_lower.endswith(direct_ext):
            return direct_ext
    lower = (url or "").lower()
    if any(k in lower for k in ["story", "audio", "music", "voice", "sound"]) or source == "chapter":
        return ".m4a"
    return ".mp4"


def split_stem_and_ext(filename: str) -> Tuple[str, str]:
    """按最后一个点拆分子文件名和扩展名。"""
    base, ext = os.path.splitext(filename)
    return (base or filename), ext


def extract_token_from_text(text: str) -> Optional[str]:
    """从文本日志中提取最新的 access_token。"""
    matches = TOKEN_RE.findall(text or "")
    return matches[-1].strip() if matches else None


def extract_token_from_bytes(raw: bytes) -> Optional[str]:
    """从字节流日志中提取最新的 access_token。"""
    matches = TOKEN_BYTES_RE.findall(raw or b"")
    if not matches:
        return None
    return matches[-1].decode("utf-8", errors="ignore").strip() or None


def build_ssl_context(verify_ssl: bool = True) -> ssl.SSLContext:
    """构造 SSL 上下文，默认开启证书校验。"""
    if not verify_ssl:
        return ssl._create_unverified_context()
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


def _get_requests_session():
    if requests is None:
        return None
    sess = getattr(_thread_local, "session", None)
    if sess is None:
        sess = requests.Session()
        _thread_local.session = sess
    return sess


def fetch_url(
    url: str,
    ctx: Optional[ssl.SSLContext] = None,
    timeout: int = 12,
    retries: int = 3,
    verify_ssl: bool = True,
    headers: Optional[Dict[str, str]] = None,
) -> bytes:
    """通用 HTTP GET 获取，内置连接池与指数退避重试。"""
    req_headers = {"User-Agent": DESKTOP_UA}
    if headers:
        req_headers.update(headers)

    if ctx is not None and ctx.verify_mode == ssl.CERT_NONE:
        verify_ssl = False

    last_err: Optional[Exception] = None
    sess = _get_requests_session()
    verify_arg: object = certifi.where() if (verify_ssl and certifi is not None) else verify_ssl

    for attempt in range(1, max(1, retries) + 1):
        try:
            if sess is not None:
                resp = sess.get(url, headers=req_headers, timeout=timeout, verify=verify_arg)
                resp.raise_for_status()
                return resp.content
            ssl_ctx = ctx if ctx is not None else build_ssl_context(verify_ssl=verify_ssl)
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(min(0.4 * (2 ** (attempt - 1)), 2.5))

    raise last_err if last_err is not None else RuntimeError(f"请求失败: {url}")


def _api_request(
    method: str,
    path: str,
    body: Optional[dict] = None,
    token: str = "",
    verify_ssl: bool = True,
    timeout: int = 15,
) -> dict:
    """向 API 发起 HTTP 请求并返回解析后的 JSON。"""
    url = f"{API_BASE}{path}"
    headers = {"User-Agent": ANDROID_UA}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    sess = _get_requests_session()
    verify_arg: object = certifi.where() if (verify_ssl and certifi is not None) else verify_ssl

    for attempt in range(1, 3):
        try:
            if sess is not None:
                resp = sess.request(
                    method=method,
                    url=url,
                    json=body,
                    headers=headers,
                    timeout=timeout,
                    verify=verify_arg,
                )
                try:
                    return resp.json()
                except Exception:
                    return {"code": resp.status_code, "msg": resp.text[:200] or f"HTTP {resp.status_code}"}
            data = json.dumps(body).encode("utf-8") if body is not None else None
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            ssl_ctx = build_ssl_context(verify_ssl=verify_ssl)
            try:
                with urllib.request.urlopen(req, context=ssl_ctx, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8", errors="replace"))
            except urllib.error.HTTPError as e:
                try:
                    return json.loads(e.read().decode("utf-8", errors="replace"))
                except Exception:
                    return {"code": e.code, "msg": str(e)}
        except Exception as e:
            if attempt < 2:
                time.sleep(0.5)
                continue
            return {"code": -1, "msg": str(e)}

    return {"code": -1, "msg": "unknown error"}


def api_post(path: str, body: dict, token: str = "", verify_ssl: bool = True) -> dict:
    return _api_request("POST", path, body=body, token=token, verify_ssl=verify_ssl)


def api_get(path: str, token: str = "", verify_ssl: bool = True) -> dict:
    return _api_request("GET", path, body=None, token=token, verify_ssl=verify_ssl)


# ══════════════════════════════════════════════
#  API 直连模块
# ══════════════════════════════════════════════

def verify_token(token: str, verify_ssl: bool = True) -> Optional[str]:
    """验证 token 有效性，返回用户名或 None。"""
    token = (token or "").strip()
    if len(token) < 20:
        return None
    data = api_get("/sysUser/getUserInfo", token=token, verify_ssl=verify_ssl)
    if data and data.get("code") == 200:
        user_data = data.get("data") or {}
        return user_data.get("username") or user_data.get("nickname") or "已登录用户"
    return None


def fetch_book_list(token: str, verify_ssl: bool = True) -> List[dict]:
    """从广场推荐接口和分类接口获取可访问的书籍列表。"""
    books: List[dict] = []
    seen_ids: Set[str] = set()

    data = api_get("/business/square/recommend/books", token=token, verify_ssl=verify_ssl)
    if data and data.get("code") == 200:
        for section in data.get("data") or []:
            for book in (section or {}).get("books") or []:
                bid = str(book.get("id") or "")
                title = (book.get("title") or "").strip()
                if bid and title and bid not in seen_ids:
                    seen_ids.add(bid)
                    books.append({"id": bid, "title": title, "source": "recommend"})

    data2 = api_get("/business/square/recommend/books/type", token=token, verify_ssl=verify_ssl)
    if data2 and data2.get("code") == 200:
        dd = data2.get("data") or {}
        if isinstance(dd, dict):
            for region in dd.get("relaxRegionList") or []:
                for book in (region or {}).get("books") or []:
                    bid = str(book.get("id") or "")
                    title = (book.get("title") or "").strip()
                    if bid and title and bid not in seen_ids:
                        seen_ids.add(bid)
                        books.append({"id": bid, "title": title, "source": "type"})
    return books


def fetch_episodes_from_api(
    token: str,
    book_id: str,
    log_fn: Callable[[str], None] = lambda msg: None,
    verify_ssl: bool = True,
    stop_event: Optional[threading.Event] = None,
) -> List[EpisodeInfo]:
    """通过 API 直连分页拉取某本书的全部剧集，自动去重排序。"""
    all_records: List[dict] = []
    page = 1
    total_pages: Optional[int] = None
    log_fn(f"  [API] 开始拉取 book_id={book_id} 的全部剧集…")

    while True:
        if stop_event and stop_event.is_set():
            break
        resp = api_post(
            f"/business/book/{book_id}/chapter/page",
            {"pageObject": {"pageNum": page, "pageSize": 100}},
            token=token,
            verify_ssl=verify_ssl,
        )
        if not resp or resp.get("code") != 200:
            log_fn(f"  [✗] book_id={book_id} 第{page}页失败: {resp.get('msg', 'unknown')}")
            break
        d = resp.get("data") or {}
        records = d.get("records") or []
        if total_pages is None:
            total_pages = d.get("pages", 0) or 1
            total = d.get("total", 0)
            log_fn(f"  [API] book_id={book_id} 共 {total} 集, {total_pages} 页")

        for rec in records:
            url = rec.get("ossAudioUrl") or rec.get("ossVideoUrl") or ""
            if url and ".m3u8" in url.lower():
                all_records.append(rec)

        if page % 10 == 0:
            log_fn(f"  [API] 已获取 {len(all_records)} 集…")

        if page >= total_pages or not records:
            break
        page += 1
        time.sleep(0.2)

    seen: Set[str] = set()
    deduped: List[dict] = []
    for r in all_records:
        eid = str(r.get("id") or "")
        dedup_token = eid if eid else str(r.get("ossAudioUrl") or r.get("ossVideoUrl") or "")
        if dedup_token and dedup_token not in seen:
            seen.add(dedup_token)
            deduped.append(r)

    episodes: List[EpisodeInfo] = []
    for r in deduped:
        ep = _episode_from_dict(r, fallback_book_id=book_id, require_title=False)
        if ep is not None:
            episodes.append(ep)

    episodes.sort(key=_episode_sort_key)
    log_fn(f"  [API] ✓ book_id={book_id} 完成，共获取 {len(episodes)} 集")
    return episodes


# ══════════════════════════════════════════════
#  日志解析
# ══════════════════════════════════════════════

def _extract_episode_number(title: str) -> int:
    """从『第xxx集』『第xxx话』『第xxx章』中提取数字。"""
    m = re.search(r'第\s*(\d+)', title or "")
    return int(m.group(1)) if m else 0


def _episode_sort_key(ep: EpisodeInfo) -> Tuple[int, int, str]:
    num = ep.episode_num if ep.episode_num > 0 else 10**9
    return (num, ep.weight, ep.title)


def _episode_from_dict(
    rec: object,
    fallback_book_id: str = "",
    require_title: bool = True,
) -> Optional[EpisodeInfo]:
    """从接口或日志 JSON 对象构建 EpisodeInfo；无效时返回 None。"""
    if not isinstance(rec, dict):
        return None
    url = (rec.get("ossAudioUrl") or rec.get("ossVideoUrl") or "").strip()
    if ".m3u8" not in url.lower():
        return None
    title = (rec.get("title") or rec.get("name") or "").strip()
    if require_title and not title:
        return None
    return EpisodeInfo(
        book_title=(rec.get("bookTitle") or "").strip(),
        book_id=str(rec.get("bookId") or fallback_book_id or ""),
        episode_id=str(rec.get("id") or ""),
        episode_num=_extract_episode_number(title),
        title=title,
        oss_audio_url=url,
        duration=rec.get("duration", 0) or 0,
        free_flag=rec.get("freeFlag", 1) or 1,
        weight=rec.get("weight", 0) or 0,
    )


def _find_json_objects(text: str) -> List[dict]:
    """在单行日志文本中尽可能找出完整的 JSON 对象（容忍前后缀杂质或截断）。"""
    results: List[dict] = []
    decoder = json.JSONDecoder()
    idx = 0
    length = len(text)
    while idx < length:
        brace = text.find("{", idx)
        if brace < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, brace)
            if isinstance(obj, dict):
                results.append(obj)
            idx = end
        except (json.JSONDecodeError, ValueError):
            idx = brace + 1
    return results


def _collect_episodes_from_json(node: object, out: List[EpisodeInfo]) -> None:
    """递归从任意 JSON 结构中收集带 ossAudioUrl/ossVideoUrl 的剧集。"""
    if isinstance(node, dict):
        records = node.get("records")
        if isinstance(records, list):
            for rec in records:
                ep = _episode_from_dict(rec)
                if ep is not None:
                    out.append(ep)
        ep = _episode_from_dict(node)
        if ep is not None:
            out.append(ep)
        for k, v in node.items():
            if k != "records":
                _collect_episodes_from_json(v, out)
    elif isinstance(node, list):
        for item in node:
            _collect_episodes_from_json(item, out)


def parse_episodes_from_text(content: str) -> Dict[str, SeriesInfo]:
    """从日志文本解析系列与剧集，返回 { series_key -> SeriesInfo }。"""
    series_map: Dict[str, SeriesInfo] = {}
    dedup_key: Set[Tuple[str, str]] = set()

    for lineno, line in enumerate(content.splitlines(), 1):
        if "ossAudioUrl" not in line and "ossVideoUrl" not in line:
            continue
        for obj in _find_json_objects(line):
            target = obj.get("data") if isinstance(obj.get("data"), (dict, list)) else obj
            extracted: List[EpisodeInfo] = []
            _collect_episodes_from_json(target, extracted)

            for ep in extracted:
                key = (ep.book_id, ep.episode_id) if ep.episode_id else (ep.book_title, ep.oss_audio_url)
                if key in dedup_key:
                    continue
                dedup_key.add(key)
                ep.line_no = lineno

                bt = ep.book_title or "未分类"
                skey = ep.book_id if ep.book_id else f"title:{bt}"
                if skey not in series_map:
                    series_map[skey] = SeriesInfo(book_title=bt, book_id=ep.book_id)
                elif not series_map[skey].book_title and ep.book_title:
                    series_map[skey].book_title = ep.book_title
                series_map[skey].chapters.append(ep)

    for s in series_map.values():
        s.chapters.sort(key=_episode_sort_key)
    return series_map


def parse_episodes(log_path: str) -> Dict[str, SeriesInfo]:
    """流式/容错读取日志文件并返回 { series_key -> SeriesInfo }。"""
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    return parse_episodes_from_text(content)


def _extract_images_and_fallback_from_json(data: object, url_to_info: Dict[str, dict]) -> None:
    """递归提取日志 JSON 中的封面/头像及常规媒体的友好名称映射。"""
    if isinstance(data, dict):
        book_title = str(data.get("bookTitle") or "").strip()
        title = str(data.get("title") or "").strip()

        oss_url = str(data.get("ossAudioUrl") or data.get("ossVideoUrl") or data.get("url") or "").strip()
        if oss_url.startswith("http"):
            is_media = bool(
                MEDIA_URL_HINT_RE.search(oss_url)
                or any(kw in oss_url.lower() for kw in ["media", "chapter", "audio", "video"])
            )
            is_img = bool(IMAGE_URL_HINT_RE.search(oss_url))
            if is_media or is_img:
                raw_name = ""
                has_title = True
                if book_title and title:
                    raw_name = f"{book_title} - {title}"
                elif title:
                    raw_name = title
                elif data.get("name"):
                    raw_name = str(data.get("name")).strip()
                else:
                    has_title = False
                    seg = urllib.parse.urlparse(oss_url).path.rsplit("/", 1)[-1]
                    raw_name = seg or "unknown"

                if raw_name:
                    cleaned = sanitize_filename(raw_name.replace(" | ", " - ").replace("|", "-"))
                    url_to_info[oss_url] = {
                        "name": cleaned,
                        "type": "media" if is_media else "image",
                        "source": "chapter" if has_title else "json_url",
                    }

        cover = str(data.get("coverImage") or data.get("headerUrl") or data.get("image") or "").strip()
        if cover.startswith("http"):
            sibling_name = title or str(data.get("nickname") or data.get("name") or "").strip()
            if sibling_name:
                cleaned_sibling = sanitize_filename(
                    sibling_name.replace(" | ", " - ").replace("|", "-").strip()
                )
                url_to_info[cover] = {
                    "name": cleaned_sibling,
                    "type": "image",
                    "source": "cover",
                }

        for v in data.values():
            _extract_images_and_fallback_from_json(v, url_to_info)
    elif isinstance(data, list):
        for item in data:
            _extract_images_and_fallback_from_json(item, url_to_info)


def parse_log_legacy_from_text(content: str) -> Tuple[List[dict], List[dict]]:
    """从日志文本解析兜底 m3u8 列表与图片列表，返回 (m3u8_items, image_items)。"""
    m3u8_urls = sorted(set(M3U8_FALLBACK_RE.findall(content)))
    image_urls = sorted(set(IMAGE_FALLBACK_RE.findall(content)))

    url_to_info: Dict[str, dict] = {}
    for url in m3u8_urls:
        path_part = urllib.parse.urlparse(url).path
        base = path_part.rsplit("/", 1)[-1].rsplit(".m3u8", 1)[0] or "unknown"
        url_to_info[url] = {"name": sanitize_filename(base), "type": "media", "source": "fallback"}

    for url in image_urls:
        path_part = urllib.parse.urlparse(url).path
        base = path_part.rsplit("/", 1)[-1] or "unknown"
        url_to_info[url] = {"name": sanitize_filename(base), "type": "image", "source": "fallback"}

    for line in content.splitlines():
        if "{" in line and "}" in line:
            for obj in _find_json_objects(line):
                _extract_images_and_fallback_from_json(obj, url_to_info)

    series_map = parse_episodes_from_text(content)
    for s in series_map.values():
        for ch in s.chapters:
            stem, _ = os.path.splitext(ch.save_filename)
            url_to_info[ch.oss_audio_url] = {
                "name": stem,
                "type": "media",
                "source": "chapter",
            }

    m3u8_items: List[dict] = []
    image_items: List[dict] = []

    for url, inf in url_to_info.items():
        if inf["type"] == "media":
            ext = infer_media_ext(url, source=inf.get("source", ""))
            name = inf["name"]
            if not name.lower().endswith((".mp4", ".m4a", ".mp3", ".ts", ".aac")):
                name = f"{name}{ext}"
            m3u8_items.append({"url": url, "name": name})
        else:
            name = inf["name"]
            if not name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                path_part = urllib.parse.urlparse(url).path
                last_seg = path_part.rsplit("/", 1)[-1]
                ext = last_seg.rsplit(".", 1)[-1].lower() if "." in last_seg else "jpg"
                if ext not in {"png", "jpg", "jpeg", "gif", "webp"}:
                    ext = "jpg"
                name = f"{name}.{ext}"
            image_items.append({"url": url, "name": name})

    m3u8_items.sort(key=lambda x: x["name"])
    image_items.sort(key=lambda x: x["name"])
    return m3u8_items, image_items


def parse_log_legacy(log_path: str) -> Tuple[List[dict], List[dict]]:
    """读取日志文件并返回兜底 (m3u8_items, image_items)。"""
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    return parse_log_legacy_from_text(content)


# ══════════════════════════════════════════════
#  M3U8 解析与并发下载引擎
# ══════════════════════════════════════════════

def _parse_key_tag(tag_body: str, playlist_url: str) -> Tuple[Optional[str], Optional[str], Optional[bytes]]:
    key_method: Optional[str] = None
    key_url: Optional[str] = None
    iv: Optional[bytes] = None

    m = re.search(r'METHOD=([^,\s]+)', tag_body)
    if m:
        key_method = m.group(1).strip()

    if key_method == "NONE":
        return "NONE", None, None

    u = re.search(r'URI="([^"]+)"', tag_body)
    if u:
        key_url = urllib.parse.urljoin(playlist_url, u.group(1).strip())

    v = re.search(r'IV=0[xX]([0-9a-fA-F]+)', tag_body)
    if v:
        hex_str = v.group(1)
        if len(hex_str) % 2 == 1:
            hex_str = "0" + hex_str
        iv = bytes.fromhex(hex_str).rjust(16, b"\x00")[-16:]

    return key_method, key_url, iv


def parse_m3u8_text(raw: str, playlist_url: str) -> M3u8Playlist:
    """纯文本解析媒体 M3U8（支持多密钥轮换与 METHOD=NONE）。"""
    media_seq = 0
    seq_m = re.search(r'#EXT-X-MEDIA-SEQUENCE:(\d+)', raw)
    if seq_m:
        media_seq = int(seq_m.group(1))

    curr_method: Optional[str] = None
    curr_key_url: Optional[str] = None
    curr_iv: Optional[bytes] = None
    segments: List[M3u8Segment] = []
    seg_offset = 0

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-KEY:"):
            curr_method, curr_key_url, curr_iv = _parse_key_tag(line[len("#EXT-X-KEY:"):], playlist_url)
            continue
        if line.startswith("#"):
            continue
        seg_url = urllib.parse.urljoin(playlist_url, line)
        segments.append(
            M3u8Segment(
                url=seg_url,
                seq=media_seq + seg_offset,
                key_method=curr_method,
                key_url=curr_key_url,
                iv=curr_iv,
            )
        )
        seg_offset += 1

    return M3u8Playlist(final_url=playlist_url, media_sequence=media_seq, segments=segments)


def _pick_best_variant_url(raw: str, base_url: str) -> Optional[str]:
    """从 Master Playlist 中按 BANDWIDTH 选择最高画质/音质子流。"""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    best_bw = -1
    best_url: Optional[str] = None

    for idx, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            bw_match = re.search(r'BANDWIDTH=(\d+)', line)
            bw = int(bw_match.group(1)) if bw_match else 0
            for next_idx in range(idx + 1, len(lines)):
                cand = lines[next_idx]
                if not cand.startswith("#"):
                    resolved = urllib.parse.urljoin(base_url, cand)
                    if bw >= best_bw:
                        best_bw = bw
                        best_url = resolved
                    break
    return best_url


def resolve_m3u8_plan(
    url: str,
    ctx: Optional[ssl.SSLContext] = None,
    verify_ssl: bool = True,
    _depth: int = 0,
) -> M3u8Playlist:
    """拉取并解析 M3U8（自动追踪 Master Playlist 最高码率子流）。"""
    if _depth > 5:
        raise ValueError(f"M3U8 嵌套层级过多: {url}")

    raw = fetch_url(url, ctx=ctx, timeout=12, retries=3, verify_ssl=verify_ssl).decode("utf-8", errors="replace")
    if "#EXT-X-STREAM-INF" in raw:
        child_url = _pick_best_variant_url(raw, url)
        if child_url:
            return resolve_m3u8_plan(child_url, ctx=ctx, verify_ssl=verify_ssl, _depth=_depth + 1)

    return parse_m3u8_text(raw, url)


def resolve_m3u8(url: str, ctx: Optional[ssl.SSLContext] = None, verify_ssl: bool = True) -> tuple:
    """兼容旧签名的 M3U8 解析函数。"""
    plan = resolve_m3u8_plan(url, ctx=ctx, verify_ssl=verify_ssl)
    first_seg = plan.segments[0] if plan.segments else None
    return (
        plan.final_url,
        first_seg.key_method if first_seg else None,
        first_seg.key_url if first_seg else None,
        first_seg.iv if first_seg else None,
        plan.media_sequence,
        [s.url for s in plan.segments],
    )


def _decrypt_segment_aes128(data: bytes, key_bytes: bytes, iv: bytes) -> bytes:
    if AES is None:
        raise RuntimeError("缺少 pycryptodome 依赖，无法解密 AES-128 加密分片（请安装 pycryptodome）")
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(data)
    if decrypted:
        pad_len = decrypted[-1]
        if 0 < pad_len <= 16 and decrypted[-pad_len:] == bytes([pad_len] * pad_len):
            decrypted = decrypted[:-pad_len]
    return decrypted


def _subprocess_creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def download_m3u8_as_ts(
    url: str,
    base_out_dir: str,
    save_filename: str,
    ctx: Optional[ssl.SSLContext] = None,
    log_fn: Callable[[str], None] = print,
    stop_event: Optional[threading.Event] = None,
    verify_ssl: bool = True,
    max_workers: int = 6,
) -> str:
    """
    并发下载 M3U8 流，支持分片重试、AES-128 解密与完整性校验，并按流类型转封装为 .mp4/.m4a。
    返回状态: 'downloaded' | 'skipped' | 'failed' | 'cancelled'
    """
    if stop_event is None:
        stop_event = threading.Event()
    if stop_event.is_set():
        return "cancelled"

    base_name, _ = split_stem_and_ext(save_filename)
    for sub, ext in [("video", ".mp4"), ("video", ".ts"), ("music", ".m4a"), ("music", ".ts")]:
        existing = os.path.join(base_out_dir, sub, f"{base_name}{ext}")
        if os.path.isfile(existing) and os.path.getsize(existing) > 0:
            log_fn(f"  [⏭] 已存在，跳过: {sub}/{base_name}{ext}")
            return "skipped"

    os.makedirs(base_out_dir, exist_ok=True)
    tmp_fd, temp_ts_path = tempfile.mkstemp(prefix="xcy_tmp_", suffix=".ts", dir=base_out_dir)
    os.close(tmp_fd)

    try:
        plan = resolve_m3u8_plan(url, ctx=ctx, verify_ssl=verify_ssl)
        if not plan.segments:
            log_fn(f"  [!] {base_name}: 未找到 TS 片段，跳过。")
            return "failed"

        # 预先按去重顺序拉取本清单用到的全部密钥
        key_cache: Dict[str, bytes] = {}
        unique_key_urls = list(
            dict.fromkeys(
                s.key_url
                for s in plan.segments
                if s.key_method == "AES-128" and s.key_url
            )
        )
        if unique_key_urls:
            if AES is None:
                log_fn("  [✗] 未安装 pycryptodome (Crypto.Cipher.AES)，无法解密 AES-128 分片！")
                return "failed"
            log_fn(f"  [🔓] 检测到 AES-128 加密，正在获取 {len(unique_key_urls)} 个密钥…")
            for kurl in unique_key_urls:
                if stop_event.is_set():
                    return "cancelled"
                key_bytes = fetch_url(kurl, ctx=ctx, timeout=20, retries=3, verify_ssl=verify_ssl)
                if len(key_bytes) != 16:
                    raise ValueError(f"密钥长度异常 ({len(key_bytes)}B): {kurl}")
                key_cache[kurl] = key_bytes

        total = len(plan.segments)
        workers = max(1, min(max_workers, total))
        log_fn(f"  [→] 并发({workers}线程)下载 {total} 个分片…")

        def _fetch_and_decrypt(idx_and_seg: Tuple[int, M3u8Segment]) -> Tuple[int, bytes]:
            idx, seg = idx_and_seg
            if stop_event.is_set():
                raise InterruptedError("cancelled")
            raw_bytes = fetch_url(seg.url, ctx=ctx, timeout=15, retries=3, verify_ssl=verify_ssl)
            if seg.key_method == "AES-128" and seg.key_url:
                kbytes = key_cache[seg.key_url]
                seg_iv = seg.iv if seg.iv is not None else seg.seq.to_bytes(16, "big")
                raw_bytes = _decrypt_segment_aes128(raw_bytes, kbytes, seg_iv)
            return idx, raw_bytes

        completed_chunks: Dict[int, bytes] = {}
        next_to_write = 0
        done_count = 0
        failed_indices: List[int] = []
        progress_step = max(1, total // 10)

        with open(temp_ts_path, "wb") as out_f:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                future_map = {
                    pool.submit(_fetch_and_decrypt, (idx, seg)): idx
                    for idx, seg in enumerate(plan.segments)
                }
                for fut in concurrent.futures.as_completed(future_map):
                    idx = future_map[fut]
                    if stop_event.is_set():
                        for pending in future_map:
                            pending.cancel()
                        log_fn("  [×] 用户中止下载。")
                        return "cancelled"
                    try:
                        chunk_idx, chunk_bytes = fut.result()
                        completed_chunks[chunk_idx] = chunk_bytes
                        done_count += 1
                        while next_to_write in completed_chunks:
                            out_f.write(completed_chunks.pop(next_to_write))
                            next_to_write += 1
                        if done_count % progress_step == 0 or done_count == total:
                            log_fn(f"      分片 {done_count}/{total}")
                    except InterruptedError:
                        return "cancelled"
                    except Exception as e:
                        failed_indices.append(idx + 1)
                        log_fn(f"      [!] 分片 {idx + 1} 重试后仍失败: {e}")

        if stop_event.is_set():
            return "cancelled"

        if failed_indices or next_to_write != total:
            log_fn(f"  [✗] {base_name} 分片缺失（失败分片: {failed_indices[:10]}），已放弃保存残缺文件。")
            return "failed"

        # 判断音/视频流
        is_video = True
        ffmpeg_bin = shutil.which("ffmpeg")
        if ffmpeg_bin:
            try:
                r = subprocess.run(
                    [ffmpeg_bin, "-i", temp_ts_path],
                    capture_output=True,
                    text=True,
                    errors="ignore",
                    timeout=10,
                    creationflags=_subprocess_creation_flags(),
                )
                if "Video:" not in r.stderr and "Audio:" in r.stderr:
                    is_video = False
            except Exception:
                if infer_media_ext(url, source="chapter" if ".m4a" in save_filename.lower() else "") == ".m4a":
                    is_video = False
        else:
            if any(k in url.lower() for k in ["story", "audio", "music", "voice", "sound"]) or save_filename.lower().endswith(".m4a"):
                is_video = False

        target_dir = os.path.join(base_out_dir, "video" if is_video else "music")
        target_ext = ".mp4" if is_video else ".m4a"
        type_icon = "🎥" if is_video else "🎵"
        log_fn(f"  [{type_icon}] 检测为 {'视频' if is_video else '音频'} → {os.path.basename(target_dir)}/")

        os.makedirs(target_dir, exist_ok=True)
        final_path = os.path.join(target_dir, f"{base_name}{target_ext}")

        converted = False
        if ffmpeg_bin:
            try:
                subprocess.run(
                    [ffmpeg_bin, "-y", "-i", temp_ts_path, "-c", "copy", final_path],
                    capture_output=True,
                    timeout=120,
                    check=True,
                    creationflags=_subprocess_creation_flags(),
                )
                converted = True
                log_fn(f"  [✓] 转封装完成: {os.path.basename(target_dir)}/{base_name}{target_ext}")
            except Exception as e:
                log_fn(f"  [!] ffmpeg 转封装失败({e})，降级保存原始 TS 流")

        if converted:
            if os.path.exists(temp_ts_path):
                os.remove(temp_ts_path)
        else:
            fallback = os.path.join(target_dir, f"{base_name}.ts")
            os.replace(temp_ts_path, fallback)
            log_fn(f"  [✓] 原始流保存: {os.path.basename(target_dir)}/{base_name}.ts")
        return "downloaded"

    except Exception as e:
        log_fn(f"  [✗] {base_name} 失败: {e}")
        return "failed"
    finally:
        if os.path.exists(temp_ts_path):
            try:
                os.remove(temp_ts_path)
            except OSError:
                pass


def download_direct_file(
    url: str,
    target_dir: str,
    save_filename: str,
    label: str = "文件",
    ctx: Optional[ssl.SSLContext] = None,
    log_fn: Callable[[str], None] = print,
    stop_event: Optional[threading.Event] = None,
    verify_ssl: bool = True,
) -> str:
    """下载直链资源（图片或非 M3U8 直链音视频）。返回 'downloaded' | 'skipped' | 'failed' | 'cancelled'"""
    if stop_event is not None and stop_event.is_set():
        return "cancelled"
    save_path = os.path.join(target_dir, save_filename)
    if os.path.isfile(save_path) and os.path.getsize(save_path) > 0:
        log_fn(f"  [⏭] {label}已存在，跳过: {save_filename}")
        return "skipped"
    try:
        data = fetch_url(url, ctx=ctx, timeout=20, retries=3, verify_ssl=verify_ssl)
        if not data:
            raise ValueError("空响应内容")
        os.makedirs(target_dir, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(prefix="xcy_dl_", suffix=".part", dir=target_dir)
        os.close(tmp_fd)
        try:
            with open(tmp_path, "wb") as f:
                f.write(data)
            os.replace(tmp_path, save_path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise
        folder = os.path.basename(target_dir)
        log_fn(f"  [✓] {label}保存成功: {folder}/{save_filename}")
        return "downloaded"
    except Exception as e:
        log_fn(f"  [!] {label}失败: {save_filename} → {e}")
        return "failed"


def download_image(
    url: str,
    out_dir: str,
    save_filename: str,
    ctx: Optional[ssl.SSLContext] = None,
    log_fn: Callable[[str], None] = print,
    stop_event: Optional[threading.Event] = None,
    verify_ssl: bool = True,
) -> str:
    """下载单张图片。返回 'downloaded' | 'skipped' | 'failed' | 'cancelled'"""
    return download_direct_file(
        url=url,
        target_dir=out_dir,
        save_filename=save_filename,
        label="图片",
        ctx=ctx,
        log_fn=log_fn,
        stop_event=stop_event,
        verify_ssl=verify_ssl,
    )


def _deduplicate_target_filenames(items: List[dict]) -> List[dict]:
    """确保同一批次下载任务内不会因文件名重名而互相覆盖。"""
    used: Dict[Tuple[str, str], int] = {}
    resolved: List[dict] = []
    for item in items:
        fn = item["filename"]
        tp = item["type"]
        stem, ext = split_stem_and_ext(fn)
        key = (tp, fn.lower())
        if key not in used:
            used[key] = 1
            resolved.append(item)
        else:
            idx = used[key] + 1
            while (tp, f"{stem}_{idx}{ext}".lower()) in used:
                idx += 1
            used[key] = idx
            new_fn = f"{stem}_{idx}{ext}"
            used[(tp, new_fn.lower())] = 1
            resolved.append({**item, "filename": new_fn})
    return resolved


# ──────────────────────────────────────────────
#  下载任务运行器
# ──────────────────────────────────────────────

def run_download_task(
    selected_items: list,
    out_dir: str,
    log_fn: Callable[[str], None],
    progress_fn: Callable[[int, int], None],
    done_fn: Callable[[dict], None] | Callable[[], None],
    stop_event: threading.Event,
    verify_ssl: bool = True,
):
    """后台下载线程入口。"""
    ctx = build_ssl_context(verify_ssl=verify_ssl)
    items = _deduplicate_target_filenames(selected_items)

    log_fn("=" * 60)
    log_fn(f"[{datetime.now().strftime('%H:%M:%S')}] 开始下载任务")
    log_fn(f"输出目录: {out_dir}")
    log_fn(f"SSL 校验: {'开启' if verify_ssl else '已关闭'} | 总数: {len(items)} 个资源")
    log_fn("=" * 60)

    total = len(items)
    completed = 0
    stats = {"downloaded": 0, "skipped": 0, "failed": 0, "cancelled": False}

    for idx, item in enumerate(items, 1):
        if stop_event.is_set():
            stats["cancelled"] = True
            break
        fn = item["filename"]
        tp = item["type"]
        url = item["url"]

        if tp == "音视频":
            log_fn(f"\n[{idx}/{total}] 正在分析/下载: {fn}")
            if ".m3u8" in url.lower():
                status = download_m3u8_as_ts(
                    url,
                    out_dir,
                    fn,
                    ctx=ctx,
                    log_fn=log_fn,
                    stop_event=stop_event,
                    verify_ssl=verify_ssl,
                )
            else:
                sub = "music" if fn.lower().endswith((".mp3", ".m4a", ".aac")) else "video"
                status = download_direct_file(
                    url=url,
                    target_dir=os.path.join(out_dir, sub),
                    save_filename=fn,
                    label="直链媒体",
                    ctx=ctx,
                    log_fn=log_fn,
                    stop_event=stop_event,
                    verify_ssl=verify_ssl,
                )
            if status == "downloaded" and idx < total and not stop_event.is_set():
                time.sleep(0.8)
        elif tp == "图片":
            log_fn(f"\n[{idx}/{total}] 正在下载图片: {fn}")
            img_dir = os.path.join(out_dir, "picture")
            os.makedirs(img_dir, exist_ok=True)
            status = download_image(
                url,
                img_dir,
                fn,
                ctx=ctx,
                log_fn=log_fn,
                stop_event=stop_event,
                verify_ssl=verify_ssl,
            )
        else:
            status = "failed"

        if status == "cancelled":
            stats["cancelled"] = True
            break
        if status in stats:
            stats[status] += 1

        completed += 1
        progress_fn(completed, total)

    if stop_event.is_set() or stats["cancelled"]:
        stats["cancelled"] = True
        log_fn(f"\n[!] 任务已中止。成功 {stats['downloaded']} | 跳过 {stats['skipped']} | 失败 {stats['failed']}")
    else:
        log_fn(
            f"\n[{datetime.now().strftime('%H:%M:%S')}] 全部任务结束！"
            f"成功 {stats['downloaded']} | 跳过 {stats['skipped']} | 失败 {stats['failed']}"
        )

    try:
        done_fn(stats)  # type: ignore[arg-type]
    except TypeError:
        done_fn()  # type: ignore[call-arg]


# ══════════════════════════════════════════════
#  图形界面
# ══════════════════════════════════════════════

PAD = 12
BG = "#f8f9fa"
PANEL = "#ffffff"
BORDER = "#dee2e6"
ACC = "#495057"
ACC_LIGHT = "#e9ecef"
FG = "#212529"
SUB_FG = "#495057"
FONT_FAMILY = "Microsoft YaHei"
FONT_NORMAL = (FONT_FAMILY, 9)
FONT_BOLD = (FONT_FAMILY, 9, "bold")
FONT_TITLE = (FONT_FAMILY, 12, "bold")

CHECK_ON = "☑"
CHECK_OFF = "☐"


class App(tk.Tk):
    """主窗口"""

    def __init__(self):
        super().__init__()
        self.title("星次元下载器 v2.5.0")
        self.resizable(True, True)
        self.minsize(920, 760)
        self.configure(bg=BG)

        self._stop_event = threading.Event()
        self._ui_queue: queue.Queue = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._busy_task = False

        # 系列映射 { series_key -> SeriesInfo } 与下拉显示名映射 { display_title -> series_key }
        self._series_map: Dict[str, SeriesInfo] = {}
        self._series_display_to_key: Dict[str, str] = {}
        self._all_series_display_titles: List[str] = []

        # token 与配置状态
        self._token_valid = False
        self._token_user = ""
        self._api_completed_book_ids: Set[str] = set()
        self._suppress_log_trace = False

        # Treeview 勾选状态 { item_id -> bool }
        self._checked_map: Dict[str, bool] = {}

        self._build_ui()
        self._load_config()
        self._poll_ui_queue()

    def _mk_btn(
        self,
        parent,
        text,
        cmd,
        state="normal",
        bg=ACC_LIGHT,
        fg=FG,
        padx=10,
        pady=2,
        font=None,
    ):
        btn = tk.Button(
            parent,
            text=text,
            command=cmd,
            bg=bg,
            fg=fg,
            activebackground=BORDER,
            activeforeground=FG,
            relief="flat",
            bd=0,
            font=font or FONT_NORMAL,
            cursor="hand2",
            padx=padx,
            pady=pady,
        )
        if state == "disabled":
            btn.config(state="disabled", bg="#e2e8f0", fg="#8c959f")
        return btn

    def _build_ui(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=PANEL)
        style.configure("TLabel", background=PANEL, foreground=FG, font=FONT_NORMAL)
        style.configure(
            "TButton",
            background=ACC_LIGHT,
            foreground=FG,
            font=FONT_BOLD,
            borderwidth=1,
            bordercolor=BORDER,
            relief="flat",
        )
        style.map(
            "TButton",
            background=[("active", BORDER), ("disabled", "#f1f3f5")],
            foreground=[("disabled", "#adb5bd")],
        )
        style.configure("Horizontal.TProgressbar", troughcolor=ACC_LIGHT, background=ACC, thickness=8)

        # ── 标题栏 ──
        title_frame = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        title_frame.pack(fill="x")
        tk.Label(
            title_frame,
            text="  ▶  星次元下载器 v2.5.0",
            bg=PANEL,
            fg=ACC,
            font=FONT_TITLE,
        ).pack(side="left", padx=PAD, pady=8)

        self._verify_ssl_var = tk.BooleanVar(value=True)
        self._remember_token_var = tk.BooleanVar(value=True)
        opt_frame = tk.Frame(title_frame, bg=PANEL)
        opt_frame.pack(side="right", padx=PAD)
        tk.Checkbutton(
            opt_frame,
            text="校验证书",
            variable=self._verify_ssl_var,
            bg=PANEL,
            fg=SUB_FG,
            font=FONT_NORMAL,
        ).pack(side="left", padx=6)
        tk.Checkbutton(
            opt_frame,
            text="记住 Token",
            variable=self._remember_token_var,
            bg=PANEL,
            fg=SUB_FG,
            font=FONT_NORMAL,
        ).pack(side="left")

        # ── 主区域 ──
        main = ttk.Frame(self, padding=PAD)
        main.pack(fill="both", expand=True, padx=PAD, pady=PAD)
        main.columnconfigure(1, weight=1)

        # ========== 第 0~1 行: Token 输入 ==========
        tk.Label(main, text="🔑 Token (API直连):", bg=PANEL, fg=FG, font=FONT_BOLD).grid(
            row=0, column=0, sticky="w", pady=(0, 2)
        )
        self._token_var = tk.StringVar()
        self._token_entry = tk.Entry(
            main,
            textvariable=self._token_var,
            relief="solid",
            bd=1,
            font=("Consolas", 10),
            show="*",
        )
        self._token_entry.grid(row=0, column=1, sticky="ew", padx=8, ipady=3)

        token_btn_frame = tk.Frame(main, bg=PANEL)
        token_btn_frame.grid(row=0, column=2, sticky="ew")

        self._btn_verify_token = self._mk_btn(
            token_btn_frame, " 验证 ", self._verify_token_async, bg="#0d6efd", fg="#ffffff"
        )
        self._btn_verify_token.pack(side="left", padx=(0, 4))

        self._btn_fetch_books = self._mk_btn(
            token_btn_frame, " 发现书籍/补全 ", self._fetch_books_async, state="disabled"
        )
        self._btn_fetch_books.pack(side="left", padx=(0, 4))

        self._token_status_label = tk.Label(main, text="", bg=PANEL, fg=SUB_FG, font=FONT_NORMAL)
        self._token_status_label.grid(row=0, column=3, sticky="w", padx=4)

        adb_row = tk.Frame(main, bg=PANEL)
        adb_row.grid(row=1, column=1, columnspan=3, sticky="w", padx=8, pady=(0, 4))
        self._btn_adb_token = self._mk_btn(
            adb_row, " 📱 从ADB/虚拟机拉取Token与日志 ", self._adb_get_token_async
        )
        self._btn_adb_token.pack(side="left", padx=(0, 8))

        self._show_token_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            adb_row,
            text="显示Token",
            variable=self._show_token_var,
            command=lambda: self._token_entry.config(show="" if self._show_token_var.get() else "*"),
            bg=PANEL,
            fg=SUB_FG,
            font=FONT_NORMAL,
        ).pack(side="left")

        # ========== 第 2 行: 日志路径 ==========
        ttk.Label(main, text="📄 日志文件:").grid(row=2, column=0, sticky="w", pady=4)
        self._log_var = tk.StringVar()
        self._log_var.trace_add("write", lambda *a: self._on_log_path_change())
        log_entry = tk.Entry(main, textvariable=self._log_var, relief="solid", bd=1, font=("Consolas", 10))
        log_entry.grid(row=2, column=1, sticky="ew", padx=8, ipady=3)
        self._btn_browse_log = self._mk_btn(main, " 浏览... ", self._pick_log)
        self._btn_browse_log.grid(row=2, column=2, sticky="ew")

        # ========== 第 3 行: 输出目录 ==========
        ttk.Label(main, text="💾 输出目录:").grid(row=3, column=0, sticky="w", pady=4)
        self._out_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads", "media_output"))
        out_entry = tk.Entry(main, textvariable=self._out_var, relief="solid", bd=1, font=("Consolas", 10))
        out_entry.grid(row=3, column=1, sticky="ew", padx=8, ipady=3)
        self._btn_browse_out = self._mk_btn(main, " 浏览... ", self._pick_out)
        self._btn_browse_out.grid(row=3, column=2, sticky="ew")

        # ========== 第 4 行: 系列搜索与选择 ==========
        series_frame = tk.Frame(main, bg=PANEL, highlightthickness=1, highlightbackground=BORDER, padx=10, pady=8)
        series_frame.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(6, 2))
        series_frame.columnconfigure(2, weight=1)

        tk.Label(series_frame, text="🔍 搜索系列:", bg=PANEL, fg=ACC, font=FONT_BOLD).grid(
            row=0, column=0, sticky="w", padx=(0, 4)
        )
        self._series_search_var = tk.StringVar()
        self._series_search_var.trace_add("write", lambda *a: self._on_series_search())
        self._series_search_entry = tk.Entry(
            series_frame,
            textvariable=self._series_search_var,
            relief="solid",
            bd=1,
            font=FONT_NORMAL,
            width=45,
        )
        self._series_search_entry.grid(row=0, column=1, sticky="w", padx=4, ipady=2)
        self._series_search_entry.bind("<Down>", lambda e: self._series_cb.focus_set())

        self._series_search_count_label = tk.Label(series_frame, text="", bg=PANEL, fg=SUB_FG, font=FONT_NORMAL)
        self._series_search_count_label.grid(row=0, column=2, sticky="w", padx=8)

        tk.Label(series_frame, text="🎬 选择系列:", bg=PANEL, fg=ACC, font=FONT_BOLD).grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(4, 0)
        )
        self._series_cb_var = tk.StringVar()
        self._series_cb = ttk.Combobox(
            series_frame,
            textvariable=self._series_cb_var,
            state="disabled",
            width=52,
            font=FONT_NORMAL,
        )
        self._series_cb.grid(row=1, column=1, sticky="w", padx=4, pady=(4, 0))
        self._series_cb.bind("<<ComboboxSelected>>", lambda e: self._on_series_selected())

        self._series_info_label = tk.Label(
            series_frame,
            text="（导入日志或通过 Token 发现书籍后可用）",
            bg=PANEL,
            fg="#adb5bd",
            font=FONT_NORMAL,
        )
        self._series_info_label.grid(row=1, column=2, sticky="w", padx=12, pady=(4, 0))

        range_row = tk.Frame(series_frame, bg=PANEL)
        range_row.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        tk.Label(range_row, text="从第", bg=PANEL, fg=FG, font=FONT_NORMAL).pack(side="left")
        self._ep_from_var = tk.StringVar()
        self._ep_from_entry = tk.Entry(
            range_row, textvariable=self._ep_from_var, width=7, relief="solid", bd=1, font=("Consolas", 10)
        )
        self._ep_from_entry.pack(side="left", padx=4)
        tk.Label(range_row, text="集  至 第", bg=PANEL, fg=FG, font=FONT_NORMAL).pack(side="left")
        self._ep_to_var = tk.StringVar()
        self._ep_to_entry = tk.Entry(
            range_row, textvariable=self._ep_to_var, width=7, relief="solid", bd=1, font=("Consolas", 10)
        )
        self._ep_to_entry.pack(side="left", padx=4)
        tk.Label(range_row, text="集", bg=PANEL, fg=FG, font=FONT_NORMAL).pack(side="left", padx=(0, 8))

        self._btn_add_range = self._mk_btn(
            range_row, " 📥 添加范围到列表 ", self._add_episode_range, bg="#0d6efd", fg="#ffffff"
        )
        self._btn_add_range.pack(side="left", padx=(0, 8))

        self._range_status_label = tk.Label(range_row, text="", bg=PANEL, fg=SUB_FG, font=FONT_NORMAL)
        self._range_status_label.pack(side="left")

        # ========== 第 5 行: 列表操作栏 ==========
        helper_frame = ttk.Frame(main)
        helper_frame.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(6, 2))

        self._count_label = ttk.Label(helper_frame, text="待下载资源列表: (空)", font=FONT_BOLD)
        self._count_label.pack(side="left")

        for text, cmd in [
            ("导出链接", self._export_links),
            ("清空列表", self._clear_list),
            ("仅选图片", self._select_images),
            ("仅选音视频", self._select_media),
            ("反选", self._invert_selection),
            ("全不选", self._select_none),
            ("全选", self._select_all),
        ]:
            btn = self._mk_btn(helper_frame, f" {text} ", cmd)
            btn.pack(side="right", padx=2)

        # ========== 第 6 行: 资源表格（带复选框列） ==========
        tree_frame = ttk.Frame(main)
        tree_frame.grid(row=6, column=0, columnspan=4, sticky="nsew", pady=2)
        main.rowconfigure(6, weight=3)

        style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff", foreground=FG, font=FONT_NORMAL, rowheight=24)
        style.configure("Treeview.Heading", font=FONT_BOLD, background=ACC_LIGHT)

        self._tree = ttk.Treeview(
            tree_frame,
            columns=("checked", "filename", "type", "url"),
            show="headings",
            selectmode="browse",
        )
        self._tree.heading("checked", text="选")
        self._tree.heading("filename", text="展示/保存文件名（单击行切换勾选）")
        self._tree.heading("type", text="资源类型")
        self._tree.heading("url", text="远程资源链接")
        self._tree.column("checked", width=40, minwidth=36, stretch=False, anchor="center")
        self._tree.column("filename", width=360, anchor="w")
        self._tree.column("type", width=80, anchor="center")
        self._tree.column("url", width=340, anchor="w")

        self._tree.bind("<Button-1>", self._on_tree_click)
        self._tree.bind("<space>", self._on_tree_space)

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        self._tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # ========== 第 7~8 行: 进度条 ==========
        self._progress = ttk.Progressbar(main, orient="horizontal", mode="determinate", style="Horizontal.TProgressbar")
        self._progress.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        self._progress_label = ttk.Label(main, text="等待开始…", foreground=SUB_FG)
        self._progress_label.grid(row=8, column=0, columnspan=4, sticky="w", pady=2)

        # ========== 第 9 行: 运行控制台输出框 ==========
        log_box_frame = ttk.Frame(main)
        log_box_frame.grid(row=9, column=0, columnspan=4, sticky="nsew", pady=(4, 4))
        main.rowconfigure(9, weight=2)

        log_header = ttk.Frame(log_box_frame)
        log_header.pack(fill="x")
        ttk.Label(log_header, text="📟 运行日志控制台:", font=FONT_BOLD).pack(side="left")
        self._mk_btn(log_header, " 清空日志 ", self._clear_console_log, padx=8, pady=1).pack(side="right")

        self._console_text = scrolledtext.ScrolledText(
            log_box_frame,
            height=8,
            bg="#0d1117",
            fg="#7ee787",
            insertbackground="#ffffff",
            font=("Consolas", 9),
            relief="solid",
            bd=1,
            state="disabled",
            wrap="word",
        )
        self._console_text.pack(fill="both", expand=True, pady=(2, 0))

        # ========== 第 10 行: 操作按钮栏 ==========
        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=10, column=0, columnspan=4, sticky="w", pady=(6, 0))

        self._start_btn = self._mk_btn(
            btn_frame, " ▶  开始下载已勾选项 ", self._start, bg="#0d6efd", fg="#ffffff", padx=12, pady=5
        )
        self._start_btn.pack(side="left", padx=(0, 10))

        self._stop_btn = self._mk_btn(btn_frame, " ■  停止 ", self._stop, state="disabled", padx=12, pady=5)
        self._stop_btn.pack(side="left", padx=(0, 10))

        self._open_btn = self._mk_btn(btn_frame, " 🗂  打开输出目录 ", self._open_out, padx=12, pady=5)
        self._open_btn.pack(side="left")

        self._refresh_action_states()

    # ── 配置持久化 ──

    def _load_config(self):
        if not os.path.isfile(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg.get("out_dir"), str) and cfg["out_dir"].strip():
                self._out_var.set(cfg["out_dir"].strip())
            if isinstance(cfg.get("verify_ssl"), bool):
                self._verify_ssl_var.set(cfg["verify_ssl"])
            if isinstance(cfg.get("remember_token"), bool):
                self._remember_token_var.set(cfg["remember_token"])
            if cfg.get("remember_token", True) and isinstance(cfg.get("token"), str) and cfg["token"].strip():
                self._token_var.set(cfg["token"].strip())
                self._verify_token_async(silent=True)
        except Exception:
            pass

    def _save_config(self):
        try:
            cfg = {
                "out_dir": self._out_var.get().strip(),
                "verify_ssl": bool(self._verify_ssl_var.get()),
                "remember_token": bool(self._remember_token_var.get()),
                "token": self._token_var.get().strip() if self._remember_token_var.get() else "",
            }
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ── 状态控制 ──

    def _refresh_action_states(self):
        has_series = bool(self._series_map)
        disabled_bg = "#e2e8f0"
        disabled_fg = "#8c959f"
        normal_bg = "#ffffff"
        normal_fg = "#212529"

        self._series_cb.config(state="readonly" if has_series else "disabled")
        self._ep_from_entry.config(
            state="normal" if has_series else "disabled",
            bg=normal_bg if has_series else disabled_bg,
            fg=normal_fg if has_series else disabled_fg,
        )
        self._ep_to_entry.config(
            state="normal" if has_series else "disabled",
            bg=normal_bg if has_series else disabled_bg,
            fg=normal_fg if has_series else disabled_fg,
        )
        self._series_search_entry.config(
            state="normal" if has_series else "disabled",
            bg=normal_bg if has_series else disabled_bg,
            fg=normal_fg if has_series else disabled_fg,
        )
        if has_series:
            self._btn_add_range.config(state="normal", bg="#0d6efd", fg="#ffffff")
        else:
            self._btn_add_range.config(state="disabled", bg=disabled_bg, fg=disabled_fg)
            self._series_info_label.config(text="（导入日志或通过 Token 发现书籍后可用）", fg="#adb5bd")

        can_fetch = self._token_valid and not self._busy_task
        if can_fetch:
            self._btn_fetch_books.config(state="normal", bg="#0d6efd", fg="#ffffff")
        else:
            self._btn_fetch_books.config(state="disabled", bg=disabled_bg, fg=disabled_fg)

        if self._busy_task:
            self._btn_verify_token.config(state="disabled", bg=disabled_bg, fg=disabled_fg)
            self._btn_adb_token.config(state="disabled", bg=disabled_bg, fg=disabled_fg)
        else:
            self._btn_verify_token.config(state="normal", bg="#0d6efd", fg="#ffffff")
            self._btn_adb_token.config(state="normal", bg=ACC_LIGHT, fg=FG)

    # ── 控制台日志输出 ──

    def _append_console_log(self, msg: str):
        self._console_text.config(state="normal")
        self._console_text.insert("end", msg + "\n")
        line_count = int(self._console_text.index("end-1c").split(".")[0])
        if line_count > 2000:
            self._console_text.delete("1.0", f"{line_count - 1500}.0")
        self._console_text.see("end")
        self._console_text.config(state="disabled")

    def _clear_console_log(self):
        self._console_text.config(state="normal")
        self._console_text.delete("1.0", "end")
        self._console_text.config(state="disabled")

    # ── 系列搜索与展示 ──

    def _build_series_display_titles(self) -> None:
        title_counts: Dict[str, int] = {}
        for si in self._series_map.values():
            title_counts[si.book_title] = title_counts.get(si.book_title, 0) + 1

        self._series_display_to_key.clear()
        sorted_items = sorted(self._series_map.items(), key=lambda kv: (-kv[1].count, kv[1].book_title))
        display_titles: List[str] = []
        for skey, si in sorted_items:
            if title_counts.get(si.book_title, 0) > 1 and si.book_id:
                disp = f"{si.book_title} [ID:{si.book_id}]"
            else:
                disp = si.book_title
            self._series_display_to_key[disp] = skey
            display_titles.append(disp)
        self._all_series_display_titles = display_titles

    def _refresh_series_dropdown(self):
        self._build_series_display_titles()
        self._on_series_search()
        self._refresh_action_states()

    def _on_series_search(self):
        keyword = self._series_search_var.get().strip().lower()
        if not keyword or not self._all_series_display_titles:
            filtered = self._all_series_display_titles[:]
        else:
            filtered = [t for t in self._all_series_display_titles if keyword in t.lower()]

        self._series_cb["values"] = filtered
        if filtered:
            current = self._series_cb_var.get()
            if current not in filtered:
                self._series_cb_var.set(filtered[0])
            self._on_series_selected()
            self._series_search_count_label.config(
                text=f"共 {len(filtered)} 个" if len(filtered) < len(self._all_series_display_titles) else "",
                fg=SUB_FG,
            )
        else:
            self._series_cb_var.set("")
            self._series_cb.set("")
            self._series_info_label.config(
                text="无匹配结果" if self._all_series_display_titles else "（导入日志或通过 Token 发现书籍后可用）",
                fg="#adb5bd",
            )
            self._series_search_count_label.config(
                text="无匹配" if self._all_series_display_titles else "", fg="red"
            )

    def _on_series_selected(self):
        disp = self._series_cb_var.get()
        skey = self._series_display_to_key.get(disp)
        si = self._series_map.get(skey or "")
        if si:
            self._series_info_label.config(text=si.episode_range, fg=SUB_FG)
            self._ep_from_var.set(str(si.min_episode) if si.min_episode else "")
            self._ep_to_var.set(str(si.max_episode) if si.max_episode else "")
        else:
            self._series_info_label.config(text="")

    def _merge_series_data(self, incoming: Dict[str, SeriesInfo]) -> int:
        added_count = 0
        for skey, si in incoming.items():
            target_key = skey
            # 如果有相同 book_title 且有一方缺少 book_id，尝试合并
            if target_key not in self._series_map and si.book_title:
                for exist_key, exist_si in self._series_map.items():
                    if exist_si.book_title == si.book_title and (not exist_si.book_id or not si.book_id):
                        target_key = exist_key
                        if not exist_si.book_id and si.book_id:
                            exist_si.book_id = si.book_id
                        break

            if target_key not in self._series_map:
                self._series_map[target_key] = si
                added_count += len(si.chapters)
            else:
                existing_keys = {
                    (c.episode_id or c.oss_audio_url) for c in self._series_map[target_key].chapters
                }
                for ch in si.chapters:
                    ep_key = ch.episode_id or ch.oss_audio_url
                    if ep_key and ep_key not in existing_keys:
                        self._series_map[target_key].chapters.append(ch)
                        existing_keys.add(ep_key)
                        added_count += 1
                self._series_map[target_key].chapters.sort(key=_episode_sort_key)
        return added_count

    # ── Token 验证与发现书籍（异步线程） ──

    def _verify_token_async(self, silent: bool = False):
        token = self._token_var.get().strip()
        if len(token) < 20:
            if not silent:
                self._token_status_label.config(text="❌ 无效的 Token", fg="red")
            return
        if self._busy_task:
            return
        self._busy_task = True
        self._token_status_label.config(text="⏳ 验证 Token 中…", fg=SUB_FG)
        self._refresh_action_states()

        verify_ssl = bool(self._verify_ssl_var.get())

        def worker():
            user = verify_token(token, verify_ssl=verify_ssl)
            self._ui_queue.put(("__token_verified__", token, user, silent))

        threading.Thread(target=worker, daemon=True).start()

    def _fetch_books_async(self):
        if self._busy_task:
            return
        token = self._token_var.get().strip()
        if not self._token_valid or not token:
            self._token_status_label.config(text="❌ 请先验证有效的 Token", fg="red")
            return

        self._busy_task = True
        self._token_status_label.config(text="⏳ 正在通过 API 发现书籍与补全剧集…", fg=SUB_FG)
        self._refresh_action_states()

        verify_ssl = bool(self._verify_ssl_var.get())
        known_books = [
            (si.book_id, si.book_title)
            for si in self._series_map.values()
            if si.book_id and si.book_id not in self._api_completed_book_ids
        ]

        def worker():
            try:
                api_books = fetch_book_list(token, verify_ssl=verify_ssl)
                targets: List[Tuple[str, str]] = []
                seen_bids: Set[str] = set()
                for bid, btitle in known_books:
                    if bid not in seen_bids:
                        seen_bids.add(bid)
                        targets.append((bid, btitle))
                for b in api_books:
                    bid = b["id"]
                    if bid not in seen_bids and bid not in self._api_completed_book_ids:
                        seen_bids.add(bid)
                        targets.append((bid, b["title"]))

                if not targets and not self._series_map:
                    self._ui_queue.put(("__status__", "❌ 未发现可访问的书籍", "red"))
                    return

                fetched_series: Dict[str, SeriesInfo] = {}
                completed_bids: Set[str] = set()
                for idx, (bid, btitle) in enumerate(targets, 1):
                    self._ui_queue.put(("__status__", f"⏳ [{idx}/{len(targets)}] 拉取《{btitle[:18]}》…", SUB_FG))
                    eps = fetch_episodes_from_api(
                        token,
                        bid,
                        log_fn=self._enqueue_log,
                        verify_ssl=verify_ssl,
                    )
                    if eps:
                        completed_bids.add(bid)
                        fetched_series[bid] = SeriesInfo(book_title=btitle, book_id=bid, chapters=eps)

                self._ui_queue.put(("__books_fetched__", fetched_series, completed_bids))
            except Exception as e:
                self._enqueue_log(f"[✗] API 拉取书籍异常: {e}")
                self._ui_queue.put(("__status__", f"❌ 拉取失败: {e}", "red"))
            finally:
                self._ui_queue.put(("__busy_done__",))

        threading.Thread(target=worker, daemon=True).start()

    # ── ADB 查找与单次拉取 ──

    @staticmethod
    def _find_adb() -> Optional[str]:
        adb_name = "adb.exe" if sys.platform == "win32" else "adb"
        found = shutil.which(adb_name)
        if found:
            return found

        common_paths = [
            os.path.expanduser("~\\AppData\\Local\\Android\\Sdk\\platform-tools\\adb.exe"),
            os.path.expanduser("~\\AppData\\Local\\Android\\sdk\\platform-tools\\adb.exe"),
            "C:\\Program Files\\Android\\android-sdk\\platform-tools\\adb.exe",
            "C:\\Android\\platform-tools\\adb.exe",
            os.path.expanduser("~\\Android\\Sdk\\platform-tools\\adb.exe"),
        ]
        for p in common_paths:
            if os.path.isfile(p):
                return p

        if sys.platform == "win32":
            try:
                import winreg
                for key in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
                    for subkey in [r"SOFTWARE\Android\SDK", r"SOFTWARE\Wow6432Node\Android\SDK"]:
                        try:
                            with winreg.OpenKey(key, subkey) as k:
                                sdk_path = winreg.QueryValueEx(k, "Path")[0]
                                cand = os.path.join(sdk_path, "platform-tools", "adb.exe")
                                if os.path.isfile(cand):
                                    return cand
                        except OSError:
                            pass
            except ImportError:
                pass
        return None

    @staticmethod
    def _inspect_adb_devices(adb_cmd: str) -> Tuple[List[str], List[Tuple[str, str]]]:
        """返回 (在线device列表, 异常状态设备[(serial, state)])。"""
        try:
            res = subprocess.run(
                [adb_cmd, "devices"],
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=_subprocess_creation_flags(),
            )
            ready: List[str] = []
            issues: List[Tuple[str, str]] = []
            for line in res.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith("List of devices"):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    serial = parts[0].strip()
                    state = " ".join(parts[1:]).strip() if parts[1] == "no" else parts[1].strip()
                    if state == "device":
                        ready.append(serial)
                    else:
                        issues.append((serial, state))
            return ready, issues
        except Exception:
            return [], []

    @staticmethod
    def _adb_pull_log_once(adb_cmd: str, serial: str) -> Optional[str]:
        """优先通过 adb pull 单次拉取 app.log，失败时降级 exec-out / shell cat。"""
        tmp_dir = os.path.join(tempfile.gettempdir(), "xcy_adb_logs")
        os.makedirs(tmp_dir, exist_ok=True)
        safe_serial = sanitize_filename(serial, max_len=40)
        local_path = os.path.join(tmp_dir, f"app_{safe_serial}.log")

        # 1. adb pull（不经过 PTY，无换行篡改且最快）
        try:
            pull_res = subprocess.run(
                [adb_cmd, "-s", serial, "pull", REMOTE_LOG_PATH, local_path],
                capture_output=True,
                timeout=30,
                creationflags=_subprocess_creation_flags(),
            )
            if pull_res.returncode == 0 and os.path.isfile(local_path) and os.path.getsize(local_path) > 50:
                return local_path
        except Exception:
            pass

        # 2. 降级 exec-out / shell cat
        for subcmd in (["exec-out", "cat", REMOTE_LOG_PATH], ["shell", "cat", REMOTE_LOG_PATH]):
            try:
                res = subprocess.run(
                    [adb_cmd, "-s", serial] + subcmd,
                    capture_output=True,
                    timeout=30,
                    creationflags=_subprocess_creation_flags(),
                )
                if res.returncode == 0 and res.stdout and len(res.stdout) > 50:
                    with open(local_path, "wb") as f:
                        f.write(res.stdout)
                    return local_path
            except Exception:
                continue
        return None

    def _adb_get_token_async(self):
        if self._busy_task:
            return
        self._busy_task = True
        self._token_status_label.config(text="⏳ 正在检查 ADB 与设备连接…", fg=SUB_FG)
        self._refresh_action_states()
        verify_ssl = bool(self._verify_ssl_var.get())

        def worker():
            try:
                adb_cmd = self._find_adb()
                if not adb_cmd:
                    self._ui_queue.put(("__status__", "❌ 未找到 adb.exe，请安装 Android platform-tools", "red"))
                    return

                ready, issues = self._inspect_adb_devices(adb_cmd)
                if not ready:
                    if issues:
                        desc = ", ".join(f"{s}({st})" for s, st in issues)
                        self._ui_queue.put(("__status__", f"⚠️ 设备未就绪: {desc}（请确认虚拟机已允许USB调试）", "orange"))
                    else:
                        self._ui_queue.put(("__status__", "❌ 未检测到已连接的设备或虚拟机", "red"))
                    return

                self._ui_queue.put(("__status__", f"📱 检测到 {len(ready)} 台设备，拉取日志中…", SUB_FG))
                self._enqueue_log(f"[ADB] 检测到在线设备: {', '.join(ready)}")

                for serial in ready:
                    self._ui_queue.put(("__status__", f"⏳ 正在从 {serial} 拉取 app.log…", SUB_FG))
                    pulled_path = self._adb_pull_log_once(adb_cmd, serial)
                    if not pulled_path:
                        self._enqueue_log(f"[ADB] {serial}: 未找到或无法读取 {REMOTE_LOG_PATH}")
                        continue

                    with open(pulled_path, "rb") as f:
                        raw_bytes = f.read()
                    token = extract_token_from_bytes(raw_bytes)
                    content = raw_bytes.decode("utf-8", errors="replace")

                    series_map = parse_episodes_from_text(content)
                    m3u8_items, image_items = parse_log_legacy_from_text(content)

                    user: Optional[str] = None
                    if token:
                        self._ui_queue.put(("__status__", f"⏳ 已提取 Token，正在验证({serial})…", SUB_FG))
                        user = verify_token(token, verify_ssl=verify_ssl)
                    else:
                        self._enqueue_log(f"[ADB] {serial}: 日志中未包含 access_token（请先在星次元 App 内登录/浏览）")

                    self._ui_queue.put((
                        "__adb_result__",
                        serial,
                        pulled_path,
                        token,
                        user,
                        series_map,
                        m3u8_items,
                        image_items,
                    ))
                    return

                self._ui_queue.put(("__status__", "❌ 所有在线设备均未能拉取到有效日志", "red"))
            except Exception as e:
                self._enqueue_log(f"[ADB] 异常: {e}")
                self._ui_queue.put(("__status__", f"❌ ADB 拉取异常: {e}", "red"))
            finally:
                self._ui_queue.put(("__busy_done__",))

        threading.Thread(target=worker, daemon=True).start()

    # ── 列表勾选与资源操作 ──

    def _insert_tree_row(self, filename: str, itype: str, url: str, checked: bool = True):
        mark = CHECK_ON if checked else CHECK_OFF
        item_id = self._tree.insert("", "end", values=(mark, filename, itype, url))
        self._checked_map[item_id] = checked
        return item_id

    def _set_row_checked(self, item_id: str, checked: bool):
        vals = list(self._tree.item(item_id, "values"))
        vals[0] = CHECK_ON if checked else CHECK_OFF
        self._tree.item(item_id, values=vals)
        self._checked_map[item_id] = checked

    def _on_tree_click(self, event):
        region = self._tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        row_id = self._tree.identify_row(event.y)
        if row_id:
            self._set_row_checked(row_id, not self._checked_map.get(row_id, False))
            self._update_count_label()

    def _on_tree_space(self, _event):
        for row_id in self._tree.selection():
            self._set_row_checked(row_id, not self._checked_map.get(row_id, False))
        self._update_count_label()

    def _select_all(self):
        for ch in self._tree.get_children():
            self._set_row_checked(ch, True)
        self._update_count_label()

    def _select_none(self):
        for ch in self._tree.get_children():
            self._set_row_checked(ch, False)
        self._update_count_label()

    def _invert_selection(self):
        for ch in self._tree.get_children():
            self._set_row_checked(ch, not self._checked_map.get(ch, False))
        self._update_count_label()

    def _select_media(self):
        for ch in self._tree.get_children():
            is_media = self._tree.item(ch, "values")[2] == "音视频"
            self._set_row_checked(ch, is_media)
        self._update_count_label()

    def _select_images(self):
        for ch in self._tree.get_children():
            is_img = self._tree.item(ch, "values")[2] == "图片"
            self._set_row_checked(ch, is_img)
        self._update_count_label()

    def _clear_list(self):
        for ch in self._tree.get_children():
            self._tree.delete(ch)
        self._checked_map.clear()
        self._update_count_label()

    def _get_checked_items(self) -> List[dict]:
        items: List[dict] = []
        for ch_id in self._tree.get_children():
            if self._checked_map.get(ch_id, False):
                _, fn, tp, url = self._tree.item(ch_id, "values")
                items.append({"filename": fn, "type": tp, "url": url})
        return items

    def _export_links(self):
        items = self._get_checked_items()
        if not items:
            messagebox.showinfo("提示", "请先勾选需要导出的资源。")
            return
        save_path = filedialog.asksaveasfilename(
            title="导出已勾选资源链接",
            defaultextension=".txt",
            filetypes=[("文本链接清单", "*.txt"), ("JSON 清单", "*.json")],
        )
        if not save_path:
            return
        try:
            if save_path.lower().endswith(".json"):
                with open(save_path, "w", encoding="utf-8") as f:
                    json.dump(items, f, ensure_ascii=False, indent=2)
            else:
                with open(save_path, "w", encoding="utf-8") as f:
                    for it in items:
                        f.write(f"{it['type']}\t{it['filename']}\t{it['url']}\n")
            self._append_console_log(f"[✓] 已导出 {len(items)} 条链接至: {save_path}")
            messagebox.showinfo("导出成功", f"已导出 {len(items)} 条链接。")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def _add_series_to_tree(self, series_list: List[SeriesInfo], checked: bool = True):
        existing_urls = {self._tree.item(ch, "values")[3] for ch in self._tree.get_children()}
        added = 0
        for si in series_list:
            for ch in si.chapters:
                if ch.oss_audio_url in existing_urls:
                    continue
                self._insert_tree_row(ch.save_filename, "音视频", ch.oss_audio_url, checked=checked)
                existing_urls.add(ch.oss_audio_url)
                added += 1
        self._update_count_label()
        if added > 0:
            self._range_status_label.config(text=f"✅ 已添加 {added} 集")
            self.after(3000, lambda: self._range_status_label.config(text=""))

    def _add_episode_range(self):
        disp = self._series_cb_var.get()
        skey = self._series_display_to_key.get(disp)
        si = self._series_map.get(skey or "")
        if not si or not si.chapters:
            messagebox.showwarning("提示", "请先选择一个有效的系列。")
            return

        from_str = self._ep_from_var.get().strip()
        to_str = self._ep_to_var.get().strip()
        if not from_str.isdigit() or not to_str.isdigit():
            messagebox.showwarning("提示", "请输入有效的数字范围。")
            return

        ep_from, ep_to = int(from_str), int(to_str)
        if ep_from > ep_to:
            ep_from, ep_to = ep_to, ep_from
            self._ep_from_var.set(str(ep_from))
            self._ep_to_var.set(str(ep_to))

        matched = [ch for ch in si.chapters if ep_from <= ch.episode_num <= ep_to]
        if not matched:
            messagebox.showinfo("结果", f"在范围内未找到剧集。\n本系列范围为 {si.min_episode} ~ {si.max_episode} 集。")
            return

        existing_urls = {self._tree.item(ch, "values")[3] for ch in self._tree.get_children()}
        added = 0
        for ch in matched:
            if ch.oss_audio_url in existing_urls:
                continue
            self._insert_tree_row(ch.save_filename, "音视频", ch.oss_audio_url, checked=True)
            existing_urls.add(ch.oss_audio_url)
            added += 1

        self._update_count_label()
        self._range_status_label.config(text=f"✅ 已添加 {added} 集" if added else "⏭ 均已在列表中")
        self.after(3000, lambda: self._range_status_label.config(text=""))

    def _update_count_label(self):
        children = self._tree.get_children()
        total = len(children)
        checked = sum(1 for ch in children if self._checked_map.get(ch, False))
        media = sum(1 for ch in children if self._tree.item(ch, "values")[2] == "音视频")
        images = total - media
        self._count_label.config(
            text=f"待下载资源列表: 已选 {checked}/{total} (音视频 {media} | 图片 {images})"
        )

    # ── 文件选择与本地日志解析 ──

    def _pick_log(self):
        p = filedialog.askopenfilename(title="选择 app.log", filetypes=[("日志文件", "*.log"), ("所有文件", "*.*")])
        if p:
            self._log_var.set(p)

    def _pick_out(self):
        p = filedialog.askdirectory(title="选择输出目录")
        if p:
            self._out_var.set(p)
            self._save_config()

    def _open_out(self):
        path = self._out_var.get().strip()
        if not path or not os.path.exists(path):
            messagebox.showinfo("提示", "输出目录不存在，请先开始下载。")
            return
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.run(["open", path])
        else:
            subprocess.run(["xdg-open", path])

    def _on_log_path_change(self):
        if self._suppress_log_trace:
            return
        path = self._log_var.get().strip()
        if not path or not os.path.isfile(path):
            return
        self._parse_local_log_async(path)

    def _parse_local_log_async(self, log_path: str):
        def worker():
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                token = extract_token_from_text(content)
                log_series = parse_episodes_from_text(content)
                m3u8_items, image_items = parse_log_legacy_from_text(content)
                self._ui_queue.put(("__local_log_parsed__", log_path, token, log_series, m3u8_items, image_items))
            except Exception as e:
                self._enqueue_log(f"[!] 解析日志失败: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _populate_legacy_items(self, m3u8_items: List[dict], image_items: List[dict]):
        existing_urls = {self._tree.item(ch, "values")[3] for ch in self._tree.get_children()}
        for item in m3u8_items:
            if item["url"] not in existing_urls:
                self._insert_tree_row(item["name"], "音视频", item["url"], checked=True)
                existing_urls.add(item["url"])
        for item in image_items:
            if item["url"] not in existing_urls:
                self._insert_tree_row(item["name"], "图片", item["url"], checked=True)
                existing_urls.add(item["url"])
        self._update_count_label()

    # ── 下载控制 ──

    def _start(self):
        out_dir = self._out_var.get().strip()
        if not out_dir:
            messagebox.showerror("错误", "请指定输出目录。")
            return

        selected_items = self._get_checked_items()
        if not selected_items:
            messagebox.showerror("错误", "请先勾选需要下载的资源。")
            return

        os.makedirs(out_dir, exist_ok=True)
        self._save_config()

        self._stop_event.clear()
        self._start_btn.config(state="disabled", bg="#e2e8f0", fg="#8c959f")
        self._stop_btn.config(state="normal", bg="#dc3545", fg="#ffffff")
        self._progress["value"] = 0
        self._progress_label.config(text="正在运行…")

        verify_ssl = bool(self._verify_ssl_var.get())
        self._worker = threading.Thread(
            target=run_download_task,
            args=(
                selected_items,
                out_dir,
                self._enqueue_log,
                self._update_progress,
                self._on_done,
                self._stop_event,
                verify_ssl,
            ),
            daemon=True,
        )
        self._worker.start()

    def _stop(self):
        self._stop_event.set()
        self._enqueue_log("\n[!] 正在发出中止信号，等待当前活跃请求停止…")

    # ── UI 事件队列 ──

    def _enqueue_log(self, msg: str):
        self._ui_queue.put(("__log__", msg))

    def _update_progress(self, done: int, total: int):
        if total > 0:
            pct = int(done / total * 100)
            self._ui_queue.put(("__progress__", done, total, pct))

    def _on_done(self, stats: Optional[dict] = None):
        self._ui_queue.put(("__done__", stats or {}))

    def _poll_ui_queue(self):
        while not self._ui_queue.empty():
            item = self._ui_queue.get_nowait()
            tag = item[0] if isinstance(item, tuple) else item

            if tag == "__log__":
                self._append_console_log(str(item[1]))
            elif tag == "__status__":
                _, text, fg = item
                self._token_status_label.config(text=text, fg=fg)
            elif tag == "__busy_done__":
                self._busy_task = False
                self._refresh_action_states()
            elif tag == "__progress__":
                _, done, total, pct = item
                self._progress["value"] = pct
                self._progress_label.config(text=f"进度: {done}/{total}  ({pct}%)")
            elif tag == "__done__":
                stats = item[1] if isinstance(item, tuple) else {}
                self._start_btn.config(state="normal", bg="#0d6efd", fg="#ffffff")
                self._stop_btn.config(state="disabled", bg="#e2e8f0", fg="#8c959f")
                if stats.get("cancelled"):
                    self._progress_label.config(text="已中止！")
                else:
                    self._progress_label.config(
                        text=f"完成！成功 {stats.get('downloaded', 0)} | 跳过 {stats.get('skipped', 0)} | 失败 {stats.get('failed', 0)}"
                    )
            elif tag == "__token_verified__":
                _, token, user, silent = item
                self._busy_task = False
                if user:
                    self._token_valid = True
                    self._token_user = user
                    self._token_status_label.config(text=f"✅ 已认证: {user}", fg="green")
                    self._append_console_log(f"[Token] 认证成功，当前用户: {user}")
                    self._save_config()
                else:
                    self._token_valid = False
                    if not silent:
                        self._token_status_label.config(text="❌ Token 无效或已过期", fg="red")
                self._refresh_action_states()
            elif tag == "__books_fetched__":
                _, fetched_series, completed_bids = item
                self._api_completed_book_ids.update(completed_bids)
                self._merge_series_data(fetched_series)
                self._refresh_series_dropdown()
                self._add_series_to_tree(list(self._series_map.values()), checked=True)
                total_eps = sum(s.count for s in self._series_map.values())
                self._token_status_label.config(
                    text=f"✅ 已加载 {len(self._series_map)} 个系列，共 {total_eps} 集", fg="green"
                )
            elif tag == "__local_log_parsed__":
                _, log_path, token, log_series, m3u8_items, image_items = item
                added_eps = self._merge_series_data(log_series)
                self._refresh_series_dropdown()
                self._populate_legacy_items(m3u8_items, image_items)
                self._append_console_log(
                    f"[日志] 已解析 {os.path.basename(log_path)}: {len(log_series)} 个系列(新增{added_eps}集), "
                    f"音视频 {len(m3u8_items)} | 图片 {len(image_items)}"
                )
                if token and not self._token_var.get().strip():
                    self._token_var.set(token)
                    self._append_console_log("[日志] 自动提取到 access_token，正在验证…")
                    self._verify_token_async(silent=True)
            elif tag == "__adb_result__":
                _, serial, pulled_path, token, user, series_map, m3u8_items, image_items = item
                self._suppress_log_trace = True
                try:
                    self._log_var.set(pulled_path)
                finally:
                    self._suppress_log_trace = False
                self._merge_series_data(series_map)
                self._refresh_series_dropdown()
                self._populate_legacy_items(m3u8_items, image_items)
                if token:
                    self._token_var.set(token)
                if user:
                    self._token_valid = True
                    self._token_user = user
                    self._token_status_label.config(
                        text=f"✅ {serial} · {user} · 已导入 {len(series_map)} 系列", fg="green"
                    )
                    self._save_config()
                    self._append_console_log(
                        f"[ADB] 已拉取 {serial} 日志({pulled_path})，认证用户 {user}，可点击「发现书籍/补全」继续拉取全集。"
                    )
                else:
                    self._token_valid = False
                    self._token_status_label.config(
                        text=f"⚠️ {serial} 日志已导入，但 Token 无效或缺失", fg="orange"
                    )
                self._refresh_action_states()

        self.after(100, self._poll_ui_queue)


if __name__ == "__main__":
    app = App()
    app.mainloop()
