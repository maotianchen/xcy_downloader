# -*- coding: utf-8 -*-
"""
星次元下载器

核心策略（同 v1.x）：
  从 app.log 中提取 m3u8 / 图片链接及其附带名称，
  通过 JSON 上下文建立「剧集号 ↔ 书名 ↔ 链接」映射，
  再批量拉取 M3U8 并自动解密 AES-128 流。

新增功能：
  • API 直连模式 — 填入 Token 直接从服务器拉取全部剧集（3715集）
  • 书籍搜索 — 自动发现用户可访问的有声书
  • 双模式并行 — 日志解析 + API 直连数据自动合并
"""

from __future__ import annotations

import json
import os
import queue
import re
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from tkinter import ttk, filedialog, messagebox
from typing import Dict, List, Optional, Tuple
import tkinter as tk

# ── API 基础 ──
API_BASE = "https://service.meother.com/qmyz-server-provider"
CTX = ssl._create_unverified_context()

# ──────────────────────────────────────────────
#  数据结构
# ──────────────────────────────────────────────

@dataclass
class EpisodeInfo:
    """单个剧集的完整信息"""
    book_title: str = ""
    book_id: str = ""
    episode_id: str = ""
    episode_num: int = 0          # 从标题中提取的数字
    title: str = ""               # 完整标题（含篇名）
    oss_audio_url: str = ""       # m3u8 下载链接
    duration: int = 0
    free_flag: int = 1            # 1=免费  2=会员
    weight: int = 0               # 排序权重
    line_no: int = 0              # 在日志中的行号（仅作溯源）

    @property
    def display_name(self) -> str:
        """在列表中显示的名称"""
        return f"{self.title}"

    @property
    def save_filename(self) -> str:
        """实际保存的文件名（不含目录）"""
        raw = f"{self.book_title} - {self.title}"
        return sanitize_filename(raw) + ".m4a"


@dataclass
class SeriesInfo:
    """一个系列/一本书的汇总"""
    book_title: str = ""
    book_id: str = ""
    chapters: List[EpisodeInfo] = field(default_factory=list)

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
        from collections import OrderedDict
        arcs: OrderedDict = OrderedDict()
        for ch in self.chapters:
            title = ch.title or ""
            # 提取篇名：匹配 "某某篇" 或 "某某篇第" 前面的部分
            m = re.match(r'(.+?篇)\s*第', title)
            arc_name = m.group(1) if m else "其他"
            if arc_name not in arcs:
                arcs[arc_name] = []
            arcs[arc_name].append(ch.episode_num)
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
        # 如果太长，只显示第一篇 + … + 最后一篇 + 总数
        display = " | ".join(parts)
        total = f" 共{self.count}集"
        if len(display) > 100:
            first = parts[0]
            last = parts[-1]
            display = f"{first} | … | {last}"
        return display + total


# ──────────────────────────────────────────────
#  工具函数
# ──────────────────────────────────────────────

def sanitize_filename(name: str, max_len: int = 120) -> str:
    """将非法文件名字符替换为下划线。"""
    return re.sub(r'[\\/:*?"<>|]', '_', name)[:max_len]


def fetch_url(url: str, ctx: ssl.SSLContext = CTX, timeout: int = 8) -> bytes:
    """带 UA 头的 HTTP 获取。"""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    )
    return urllib.request.urlopen(req, context=ctx, timeout=timeout).read()


def api_post(path: str, body: dict, token: str = "") -> Optional[dict]:
    """向 API 发起 POST 请求，返回 JSON。"""
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Linux; Android 9; OnePlus) AppleWebKit/537.36",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        resp = urllib.request.urlopen(req, context=CTX, timeout=15)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"code": e.code, "msg": str(e)}
    except Exception as e:
        return {"code": -1, "msg": str(e)}


def api_get(path: str, token: str = "") -> Optional[dict]:
    """向 API 发起 GET 请求，返回 JSON。"""
    url = f"{API_BASE}{path}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 9; OnePlus) AppleWebKit/537.36",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, context=CTX, timeout=15)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"code": e.code, "msg": str(e)}
    except Exception as e:
        return {"code": -1, "msg": str(e)}


# ══════════════════════════════════════════════
#  API 直连模块
# ══════════════════════════════════════════════

def verify_token(token: str) -> Optional[str]:
    """验证 token 有效性，返回用户名或 None。"""
    if not token or len(token) < 30:
        return None
    data = api_get("/sysUser/getUserInfo", token=token)
    if data and data.get("code") == 200:
        return data.get("data", {}).get("username", "unknown")
    return None


def fetch_book_list(token: str) -> List[dict]:
    """从广场推荐接口获取用户可访问的书籍列表。"""
    books = []
    # 从 square/recommend/books 获取
    data = api_get("/business/square/recommend/books", token=token)
    if data and data.get("code") == 200:
        for section in data.get("data", []):
            for book in section.get("books", []):
                bid = book.get("id", "")
                title = book.get("title", "")
                if bid and title:
                    books.append({"id": bid, "title": title, "source": "recommend"})
    # 同时也从题材分类获取
    data2 = api_get("/business/square/recommend/books/type", token=token)
    if data2 and data2.get("code") == 200:
        dd = data2.get("data", {})
        if isinstance(dd, dict):
            for region in dd.get("relaxRegionList", []):
                for book in region.get("books", []):
                    bid = book.get("id", "")
                    title = book.get("title", "")
                    if bid and title and not any(b["id"] == bid for b in books):
                        books.append({"id": bid, "title": title, "source": "type"})
    return books


def fetch_episodes_from_api(token: str, book_id: str,
                            log_fn=lambda msg: None) -> List[EpisodeInfo]:
    """
    通过 API 直连拉取某本书的全部剧集。
    自动分页，去重，排序。
    """
    all_records = []
    page = 1
    total_pages = None
    log_fn(f"  [API] 开始拉取 book_id={book_id} 的全部剧集…")

    while True:
        resp = api_post(
            f"/business/book/{book_id}/chapter/page",
            {"pageObject": {"pageNum": page, "pageSize": 100}},
            token=token,
        )
        if not resp or resp.get("code") != 200:
            log_fn(f"  [✗] 第{page}页失败: {resp.get('msg', 'unknown')}")
            break
        d = resp.get("data", {})
        records = d.get("records", [])
        if total_pages is None:
            total_pages = d.get("pages", 0)
            total = d.get("total", 0)
            log_fn(f"  [API] 共 {total} 集, {total_pages} 页")

        for rec in records:
            url = rec.get("ossAudioUrl") or ""
            if url and ".m3u8" in url:
                all_records.append(rec)

        if page % 10 == 0:
            log_fn(f"  [API] 已获取 {len(all_records)} 集…")

        if page >= (total_pages or 1):
            break
        page += 1
        time.sleep(0.25)

    # 去重
    seen = set()
    deduped = []
    for r in all_records:
        eid = r.get("id", "")
        if eid not in seen:
            seen.add(eid)
            deduped.append(r)

    deduped.sort(key=lambda x: x.get("weight", 0) or 0)

    episodes = []
    for r in deduped:
        title = r.get("title", "")
        episodes.append(EpisodeInfo(
            book_title=r.get("bookTitle", "") or "",
            book_id=str(r.get("bookId", "") or ""),
            episode_id=str(r.get("id", "") or ""),
            episode_num=_extract_episode_number(title),
            title=title,
            oss_audio_url=r.get("ossAudioUrl", "") or "",
            duration=r.get("duration", 0) or 0,
            free_flag=r.get("freeFlag", 1) or 1,
            weight=r.get("weight", 0) or 0,
        ))

    log_fn(f"  [API] ✓ 完成！共获取 {len(episodes)} 集")
    return episodes


# ══════════════════════════════════════════════
#  日志解析（增强版）
# ══════════════════════════════════════════════

def _extract_episode_number(title: str) -> int:
    """从『第xxx集』『第xxx话』『第xxx章』中提取数字。"""
    m = re.search(r'第(\d+)', title)
    return int(m.group(1)) if m else 0


def _find_json_objects(text: str) -> List[dict]:
    """在文本中尽可能找出完整的 JSON 对象（容忍截断）。"""
    results: List[dict] = []
    start = 0
    while True:
        brace = text.find("{", start)
        if brace < 0:
            break
        depth = 0
        for i in range(brace, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[brace : i + 1])
                        if isinstance(obj, dict):
                            results.append(obj)
                    except json.JSONDecodeError:
                        pass
                    start = i + 1
                    break
        else:
            try:
                decoder = json.JSONDecoder()
                obj, _ = decoder.raw_decode(text, brace)
                if isinstance(obj, dict):
                    results.append(obj)
            except (json.JSONDecodeError, ValueError):
                pass
            start = brace + 1
    return results


def _episodes_from_chapter_page(data: dict) -> List[EpisodeInfo]:
    """从 /chapter/page 的响应 data 中提取剧集列表。"""
    episodes: List[EpisodeInfo] = []
    records = []
    if isinstance(data, dict):
        records = data.get("records") or []
    elif isinstance(data, list):
        records = data
    for rec in records:
        if not isinstance(rec, dict):
            continue
        url = rec.get("ossAudioUrl") or rec.get("ossVideoUrl") or ""
        if ".m3u8" not in url:
            continue
        title = rec.get("title") or ""
        if not title:
            continue
        episodes.append(EpisodeInfo(
            book_title=rec.get("bookTitle", "") or "",
            book_id=str(rec.get("bookId", "") or ""),
            episode_id=str(rec.get("id", "") or ""),
            episode_num=_extract_episode_number(title),
            title=title,
            oss_audio_url=url,
            duration=rec.get("duration", 0) or 0,
            free_flag=rec.get("freeFlag", 1) or 1,
            weight=rec.get("weight", 0) or 0,
        ))
    return episodes


def _episodes_from_single_chapter(data: dict) -> List[EpisodeInfo]:
    """从 /chapter/{id} 的响应 data 中提取单集。"""
    if not isinstance(data, dict):
        return []
    url = data.get("ossAudioUrl") or data.get("ossVideoUrl") or ""
    if ".m3u8" not in url:
        return []
    title = data.get("title") or ""
    if not title:
        return []
    return [EpisodeInfo(
        book_title=data.get("bookTitle", "") or "",
        book_id=str(data.get("bookId", "") or ""),
        episode_id=str(data.get("id", "") or ""),
        episode_num=_extract_episode_number(title),
        title=title,
        oss_audio_url=url,
        duration=data.get("duration", 0) or 0,
        free_flag=data.get("freeFlag", 1) or 1,
        weight=data.get("weight", 0) or 0,
    )]


def parse_episodes(log_path: str) -> Dict[str, SeriesInfo]:
    """
    解析日志，返回 { book_title -> SeriesInfo } 映射。
    """
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    lines = content.splitlines()
    series_map: Dict[str, SeriesInfo] = {}
    dedup_key = set()

    for lineno, line in enumerate(lines, 1):
        if "ossAudioUrl" not in line and "ossVideoUrl" not in line:
            continue
        jsons = _find_json_objects(line)
        for obj in jsons:
            data = obj.get("data")
            if not data:
                continue

            is_page = any(kw in line for kw in ["chapter/page", "episode/page", "episodePage"])
            is_single = bool(re.search(r'/chapter/\d+', line)) and not is_page

            episodes: List[EpisodeInfo] = []
            if is_page and isinstance(data, dict) and "records" in data:
                episodes = _episodes_from_chapter_page(data)
            elif is_single:
                episodes = _episodes_from_single_chapter(data)
            else:
                episodes = _episodes_from_single_chapter(data)

            for ep in episodes:
                key = (ep.book_id, ep.episode_id)
                if not ep.episode_id:
                    key = (ep.book_title, ep.title)
                if key in dedup_key:
                    continue
                dedup_key.add(key)
                ep.line_no = lineno

                bt = ep.book_title or "未分类"
                if bt not in series_map:
                    series_map[bt] = SeriesInfo(book_title=bt, book_id=ep.book_id)
                series_map[bt].chapters.append(ep)

    for s in series_map.values():
        s.chapters.sort(key=lambda c: (c.episode_num, c.weight))
    return series_map


def parse_log_legacy(log_path: str):
    """
    原来的 parse_log，保留向后兼容。
    返回 (m3u8_items, image_items)。
    """
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    m3u8_urls = sorted(set(re.findall(r'https?://[^\s"\'<>]+?\.m3u8', content)))
    image_urls = sorted(set(re.findall(
        r'https?://[^\s"\'<>]+?\.(?:png|jpg|jpeg|gif|webp)', content)))

    url_to_name: Dict[str, str] = {}

    series_map = parse_episodes(log_path)
    for s in series_map.values():
        for ch in s.chapters:
            url_to_name[ch.oss_audio_url] = ch.save_filename

    for url in m3u8_urls:
        if url in url_to_name:
            continue
        base = url.split("/")[-1].split(".m3u8")[0]
        url_to_name[url] = sanitize_filename(base) + (".m4a" if any(
            kw in url.lower() for kw in ["story", "audio", "music", "voice", "sound"]
        ) else ".mp4")

    for url in image_urls:
        if url in url_to_name:
            continue
        base = url.split("/")[-1].split("?")[0]
        ext = base.split(".")[-1]
        if len(ext) > 4 or not ext:
            ext = "jpg"
        url_to_name[url] = sanitize_filename(base) + "." + ext

    m3u8_items = [{"url": u, "name": url_to_name[u]} for u in m3u8_urls]
    image_items = [{"url": u, "name": url_to_name[u]} for u in image_urls]
    m3u8_items.sort(key=lambda x: x["name"])
    image_items.sort(key=lambda x: x["name"])
    return m3u8_items, image_items


# ══════════════════════════════════════════════
#  M3U8 下载引擎（完全保留 v1.x 逻辑）
# ══════════════════════════════════════════════

def resolve_m3u8(url: str, ctx: ssl.SSLContext = CTX) -> tuple:
    """解析 M3U8 主/子清单，返回 (final_url, key_method, key_url, iv, media_seq, [segments])。"""
    raw = fetch_url(url, ctx).decode("utf-8", errors="replace")
    base = url.rsplit("/", 1)[0]

    if "#EXT-X-STREAM-INF" in raw:
        variants = re.findall(r'#EXT-X-STREAM-INF[^\n]*\n([^\n]+)', raw)
        if variants:
            child = variants[-1].strip()
            return resolve_m3u8(child if child.startswith("http") else f"{base}/{child}", ctx)

    key_method = key_url = iv = None
    key_match = re.search(r'#EXT-X-KEY:([^\n]+)', raw)
    if key_match:
        info = key_match.group(1)
        m = re.search(r'METHOD=([^,\s]+)', info)
        if m:
            key_method = m.group(1)
        u = re.search(r'URI="([^"]+)"', info)
        if u:
            ku = u.group(1)
            key_url = ku if ku.startswith("http") else f"{base}/{ku}"
        v = re.search(r'IV=0[xX]([0-9a-fA-F]+)', info)
        if v:
            iv = bytes.fromhex(v.group(1))

    media_seq = 0
    seq_m = re.search(r'#EXT-X-MEDIA-SEQUENCE:(\d+)', raw)
    if seq_m:
        media_seq = int(seq_m.group(1))

    segments = []
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            segments.append(line if line.startswith("http") else f"{base}/{line}")

    return url, key_method, key_url, iv, media_seq, segments


def download_m3u8_as_ts(url: str, base_out_dir: str, save_filename: str,
                        ctx: ssl.SSLContext = CTX,
                        log_fn=print, stop_event: threading.Event = None) -> bool:
    """下载 M3U8 流，自动 AES-128 解密，转封装为 .mp4/.m4a。"""
    if stop_event is None:
        stop_event = threading.Event()
    base_name = save_filename.rsplit(".", 1)[0]
    temp_ts_path = os.path.join(base_out_dir, f"temp_{base_name}.ts")

    try:
        _, key_method, key_url, iv, media_sequence, segments = resolve_m3u8(url, ctx)
        if not segments:
            log_fn(f"  [!] {base_name}: 没有 TS 片段，跳过。")
            return False

        key_bytes = None
        if key_method == "AES-128" and key_url:
            for attempt in range(1, 4):
                try:
                    from Crypto.Cipher import AES  # type: ignore
                    log_fn(f"  [🔓] AES-128 密钥获取中 (尝试 {attempt}/3)…")
                    key_bytes = fetch_url(key_url, ctx, timeout=20)
                    if len(key_bytes) == 16:
                        break
                    log_fn(f"  [!] 密钥长度异常 ({len(key_bytes)}B)，重试…")
                except Exception as e:
                    log_fn(f"  [!] 密钥失败 (尝试 {attempt}/3): {e}")
                    if attempt < 3:
                        time.sleep(2)
            if not key_bytes or len(key_bytes) != 16:
                log_fn("  [✗] 无法获取 AES 密钥，分片解密将失败！")

        total = len(segments)
        log_fn(f"  [→] 下载 {total} 个分片…")

        with open(temp_ts_path, "wb") as out_f:
            for idx, seg_url in enumerate(segments):
                if stop_event.is_set():
                    log_fn("  [×] 用户中止")
                    if os.path.exists(temp_ts_path):
                        os.remove(temp_ts_path)
                    return False
                try:
                    data = fetch_url(seg_url, ctx)
                    if key_bytes:
                        from Crypto.Cipher import AES
                        seg_iv = iv if iv else (media_sequence + idx).to_bytes(16, "big")
                        cipher = AES.new(key_bytes, AES.MODE_CBC, seg_iv)
                        data = cipher.decrypt(data)
                        if len(data) > 0:
                            pad_len = data[-1]
                            if 0 < pad_len <= 16 and data[-pad_len:] == bytes([pad_len] * pad_len):
                                data = data[:-pad_len]
                    out_f.write(data)
                    i = idx + 1
                    if i % max(1, total // 10) == 0 or i == total:
                        log_fn(f"      分片 {i}/{total}")
                except Exception as e:
                    log_fn(f"      [!] 分片 {idx+1} 失败: {e}")

        is_video = True
        try:
            r = subprocess.run(
                ["ffmpeg", "-i", temp_ts_path],
                capture_output=True, text=True, errors="ignore", timeout=10,
            )
            if "Video:" not in r.stderr and "Audio:" in r.stderr:
                is_video = False
        except FileNotFoundError:
            if any(k in url.lower() for k in ["story", "audio", "music", "voice", "sound"]):
                is_video = False

        target_dir = os.path.join(base_out_dir, "video" if is_video else "music")
        target_ext = ".mp4" if is_video else ".m4a"
        type_icon = "🎥" if is_video else "🎵"
        log_fn(f"  [{type_icon}] 检测为 {'视频' if is_video else '音频'} → {os.path.basename(target_dir)}/")

        os.makedirs(target_dir, exist_ok=True)
        final_path = os.path.join(target_dir, f"{base_name}{target_ext}")

        converted = False
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", temp_ts_path, "-c", "copy", final_path],
                capture_output=True, timeout=120, check=True,
            )
            converted = True
            log_fn(f"  [✓] 转封装完成: {os.path.join(os.path.basename(target_dir), f'{base_name}{target_ext}')}")
        except Exception:
            pass

        if os.path.exists(temp_ts_path):
            if converted:
                os.remove(temp_ts_path)
            else:
                fallback = os.path.join(target_dir, f"{base_name}.ts")
                os.rename(temp_ts_path, fallback)
                log_fn(f"  [✓] 原始流保存: {os.path.join(os.path.basename(target_dir), f'{base_name}.ts')}")
        return True

    except Exception as e:
        log_fn(f"  [✗] {base_name} 失败: {e}")
        if os.path.exists(temp_ts_path):
            os.remove(temp_ts_path)
        return False


def download_image(url: str, out_dir: str, save_filename: str,
                   ctx: ssl.SSLContext = CTX,
                   log_fn=print, stop_event: threading.Event = None) -> bool:
    """下载单张图片。"""
    if stop_event is None:
        stop_event = threading.Event()
    if stop_event.is_set():
        return False
    save_path = os.path.join(out_dir, save_filename)
    if os.path.exists(save_path):
        return True
    try:
        data = fetch_url(url, ctx, timeout=10)
        with open(save_path, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        log_fn(f"  [!] 图片失败: {save_filename} → {e}")
        return False


# ──────────────────────────────────────────────
#  下载任务运行器
# ──────────────────────────────────────────────

def run_download_task(selected_items: list, out_dir: str,
                      log_fn, progress_fn, done_fn, stop_event: threading.Event):
    """后台下载线程入口。"""
    ctx = ssl._create_unverified_context()
    log_fn("=" * 60)
    log_fn(f"[{datetime.now().strftime('%H:%M:%S')}] 开始下载任务")
    log_fn(f"输出目录: {out_dir}")
    log_fn(f"总数: {len(selected_items)} 个资源")
    log_fn("=" * 60)

    total = len(selected_items)
    completed = 0

    for idx, item in enumerate(selected_items, 1):
        if stop_event.is_set():
            break
        fn = item["filename"]
        tp = item["type"]
        url = item["url"]

        if tp == "音视频":
            log_fn(f"\n[{idx}/{total}] 正在分析/下载: {fn}")
            download_m3u8_as_ts(url, out_dir, fn, ctx, log_fn, stop_event)
            if idx < total and not stop_event.is_set():
                time.sleep(1.5)
        elif tp == "图片":
            log_fn(f"\n[{idx}/{total}] 正在下载图片: {fn}")
            img_dir = os.path.join(out_dir, "picture")
            os.makedirs(img_dir, exist_ok=True)
            download_image(url, img_dir, fn, ctx, log_fn, stop_event)

        completed += 1
        progress_fn(completed, total)

    if stop_event.is_set():
        log_fn("\n[!] 任务已被用户中止。")
    else:
        log_fn(f"\n[{datetime.now().strftime('%H:%M:%S')}] 全部下载完成！")
    done_fn()


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


class App(tk.Tk):
    """主窗口"""

    def __init__(self):
        super().__init__()
        self.title("星次元下载器")
        self.resizable(True, True)
        self.minsize(860, 680)
        self.configure(bg=BG)

        self._stop_event = threading.Event()
        self._log_queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None

        # 系列映射 { book_title -> SeriesInfo }
        self._series_map: Dict[str, SeriesInfo] = {}
        # token 验证状态
        self._token_valid = False
        self._token_user = ""
        # ADB 连接状态（系列下载功能依赖 ADB）
        self._adb_connected = False
        # 系列完整列表（搜索时用于过滤）
        self._all_series_titles: List[str] = []
        # 已通过 API 补全全集的 book_id（避免重复请求）
        self._api_completed_book_ids: set = set()

        self._build_ui()
        self._poll_log_queue()

    def _mk_btn(self, parent, text, cmd, state="normal",
                bg=ACC_LIGHT, fg=FG, padx=10, pady=2, font=None):
        btn = tk.Button(parent, text=text, command=cmd,
                        bg=bg, fg=fg, activebackground=BORDER, activeforeground=FG,
                        relief="flat", bd=0, font=font or FONT_NORMAL, cursor="hand2",
                        padx=padx, pady=pady)
        if state == "disabled":
            btn.config(state="disabled", bg="#e2e8f0", fg="#8c959f")
        return btn

    def _build_ui(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=PANEL)
        style.configure("TLabel", background=PANEL, foreground=FG, font=FONT_NORMAL)
        style.configure("TButton", background=ACC_LIGHT, foreground=FG,
                        font=FONT_BOLD, borderwidth=1, bordercolor=BORDER, relief="flat")
        style.map("TButton",
                  background=[("active", BORDER), ("disabled", "#f1f3f5")],
                  foreground=[("disabled", "#adb5bd")])
        style.configure("Horizontal.TProgressbar", troughcolor=ACC_LIGHT,
                        background=ACC, thickness=8)

        # ── 标题 ──
        title_frame = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        title_frame.pack(fill="x")
        tk.Label(title_frame, text="  ▶  星次元下载器",
                 bg=PANEL, fg=ACC, font=FONT_TITLE).pack(side="left", padx=PAD, pady=10)

        # ── 主区域(可滚动) ──
        main = ttk.Frame(self, padding=PAD)
        main.pack(fill="both", expand=True, padx=PAD, pady=PAD)
        main.columnconfigure(1, weight=1)

        # ========== 第 0~1 行: Token 输入（API直连） ==========
        token_label = tk.Label(main, text="🔑 Token (API直连):", bg=PANEL, fg=FG, font=FONT_BOLD)
        token_label.grid(row=0, column=0, sticky="w", pady=(0, 2))

        self._token_var = tk.StringVar()
        token_entry = tk.Entry(main, textvariable=self._token_var, relief="solid", bd=1,
                               font=("Consolas", 10), show="*")
        token_entry.grid(row=0, column=1, sticky="ew", padx=8, ipady=3)

        token_btn_frame = tk.Frame(main, bg=PANEL)
        token_btn_frame.grid(row=0, column=2, sticky="ew")

        self._btn_verify_token = self._mk_btn(token_btn_frame, " 验证 ", self._verify_token,
                                              bg="#0d6efd", fg="#ffffff")
        self._btn_verify_token.pack(side="left", padx=(0, 4))

        self._btn_fetch_books = self._mk_btn(token_btn_frame, " 发现书籍 ", self._fetch_books,
                                             state="disabled")
        self._btn_fetch_books.pack(side="left", padx=(0, 4))

        self._token_status_label = tk.Label(main, text="", bg=PANEL, fg=SUB_FG, font=FONT_NORMAL)
        self._token_status_label.grid(row=0, column=3, sticky="w", padx=4)

        # 从 ADB 获取 token 按钮
        self._btn_adb_token = self._mk_btn(main, " 📱 ADB获取Token ", self._adb_get_token)
        self._btn_adb_token.grid(row=1, column=1, sticky="w", padx=8, pady=(0, 4))

        # ========== 第 2 行: 日志路径 ==========
        ttk.Label(main, text="📄 日志文件:").grid(row=2, column=0, sticky="w", pady=4)
        self._log_var = tk.StringVar()
        self._log_var.trace_add("write", lambda *a: self._on_log_path_change())
        log_entry = tk.Entry(main, textvariable=self._log_var, relief="solid", bd=1,
                             font=("Consolas", 10))
        log_entry.grid(row=2, column=1, sticky="ew", padx=8, ipady=3)
        self._btn_browse_log = self._mk_btn(main, " 浏览... ", self._pick_log)
        self._btn_browse_log.grid(row=2, column=2, sticky="ew")

        # ========== 第 3 行: 输出目录 ==========
        ttk.Label(main, text="💾 输出目录:").grid(row=3, column=0, sticky="w", pady=4)
        self._out_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads", "media_output"))
        out_entry = tk.Entry(main, textvariable=self._out_var, relief="solid", bd=1,
                             font=("Consolas", 10))
        out_entry.grid(row=3, column=1, sticky="ew", padx=8, ipady=3)
        self._btn_browse_out = self._mk_btn(main, " 浏览... ", self._pick_out)
        self._btn_browse_out.grid(row=3, column=2, sticky="ew")

        # ========== 第 4 行: 系列搜索与选择 ==========
        series_frame = tk.Frame(main, bg=PANEL, highlightthickness=1, highlightbackground=BORDER,
                                padx=10, pady=8)
        series_frame.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(6, 2))
        series_frame.columnconfigure(3, weight=1)

        # 搜索框（第0行）
        tk.Label(series_frame, text="🔍 搜索:",
                 bg=PANEL, fg=ACC, font=FONT_BOLD).grid(row=0, column=0, sticky="w", padx=(0, 4))

        self._series_search_var = tk.StringVar()
        self._series_search_var.trace_add("write", lambda *a: self._on_series_search())
        self._series_search_entry = tk.Entry(series_frame, textvariable=self._series_search_var,
                                              relief="solid", bd=1,
                                              font=("Microsoft YaHei", 9),
                                              width=47)
        self._series_search_entry.grid(row=0, column=1, sticky="w", padx=4, ipady=2)
        self._series_search_entry.bind("<KeyRelease>", lambda e: self._on_series_search())
        self._series_search_entry.bind("<Down>", lambda e: self._series_cb.focus_set())

        self._series_search_count_label = tk.Label(series_frame, text="", bg=PANEL, fg=SUB_FG, font=FONT_NORMAL)
        self._series_search_count_label.grid(row=0, column=2, sticky="w", padx=8)

        # 下拉选择（第1行）
        tk.Label(series_frame, text="🎬 系列:",
                 bg=PANEL, fg=ACC, font=FONT_BOLD).grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(4, 0))

        self._series_cb_var = tk.StringVar()
        self._series_cb = ttk.Combobox(series_frame, textvariable=self._series_cb_var,
                                       state="readonly", width=55, font=FONT_NORMAL)
        self._series_cb.grid(row=1, column=1, sticky="w", padx=4, pady=(4, 0))
        self._series_cb.bind("<<ComboboxSelected>>", lambda e: self._on_series_selected())

        self._series_info_label = tk.Label(series_frame, text="（需通过 ADB 连接后可用）", bg=PANEL, fg="#adb5bd", font=FONT_NORMAL)
        self._series_info_label.grid(row=1, column=2, sticky="w", padx=12, pady=(4, 0))

        # 起止集数（第2行）
        tk.Label(series_frame, text="从第",
                 bg=PANEL, fg=FG, font=FONT_NORMAL).grid(row=2, column=0, sticky="w", pady=(6, 0))
        self._ep_from_var = tk.StringVar()
        self._ep_from_entry = tk.Entry(series_frame, textvariable=self._ep_from_var,
                                       width=8, relief="solid", bd=1, font=("Consolas", 10))
        self._ep_from_entry.grid(row=2, column=1, sticky="w", padx=4, pady=(6, 0))

        tk.Label(series_frame, text="集  至 第",
                 bg=PANEL, fg=FG, font=FONT_NORMAL).grid(row=2, column=1, sticky="w", padx=(130, 0), pady=(6, 0))
        self._ep_to_var = tk.StringVar()
        self._ep_to_entry = tk.Entry(series_frame, textvariable=self._ep_to_var,
                                     width=8, relief="solid", bd=1, font=("Consolas", 10))
        self._ep_to_entry.grid(row=2, column=1, sticky="w", padx=(240, 0), pady=(6, 0))

        tk.Label(series_frame, text="集",
                 bg=PANEL, fg=FG, font=FONT_NORMAL).grid(row=2, column=1, sticky="w", padx=(340, 0), pady=(6, 0))

        self._btn_add_range = self._mk_btn(series_frame, " 📥 添加到下载列表 ", self._add_episode_range,
                                           bg="#0d6efd", fg="#ffffff")
        self._btn_add_range.grid(row=2, column=1, sticky="w", padx=(380, 0), pady=(6, 0))

        self._range_status_label = tk.Label(series_frame, text="", bg=PANEL, fg=SUB_FG, font=FONT_NORMAL)
        self._range_status_label.grid(row=2, column=2, sticky="w", padx=8, pady=(6, 0))

        # ========== 第 5 行: 列表操作按钮 ==========
        helper_frame = ttk.Frame(main)
        helper_frame.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(6, 2))

        self._count_label = ttk.Label(helper_frame, text="待下载资源列表: (空)", font=FONT_BOLD)
        self._count_label.pack(side="left")

        for text, cmd in [("全选", self._select_all), ("全不选", self._select_none),
                          ("仅选音视频", self._select_media), ("仅选图片", self._select_images),
                          ("清空列表", self._clear_list)]:
            btn = self._mk_btn(helper_frame, f" {text} ", cmd)
            btn.pack(side="right", padx=2)

        # ========== 第 6 行: 表格 ==========
        tree_frame = ttk.Frame(main)
        tree_frame.grid(row=6, column=0, columnspan=4, sticky="nsew", pady=2)
        main.rowconfigure(6, weight=1)

        style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff",
                        foreground=FG, font=FONT_NORMAL, rowheight=24)
        style.configure("Treeview.Heading", font=FONT_BOLD, background=ACC_LIGHT)

        self._tree = ttk.Treeview(tree_frame, columns=("filename", "type", "url"),
                                  show="headings", selectmode="extended")
        self._tree.heading("filename", text="展示/保存文件名")
        self._tree.heading("type", text="资源类型")
        self._tree.heading("url", text="远程资源链接")
        self._tree.column("filename", width=380, anchor="w")
        self._tree.column("type", width=90, anchor="center")
        self._tree.column("url", width=300, anchor="w")

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        self._tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # ========== 第 7 行: 进度条 ==========
        self._progress = ttk.Progressbar(main, orient="horizontal", mode="determinate",
                                         style="Horizontal.TProgressbar")
        self._progress.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        self._progress_label = ttk.Label(main, text="等待开始…", foreground=SUB_FG)
        self._progress_label.grid(row=8, column=0, columnspan=4, sticky="w", pady=2)

        # ========== 第 9 行: 按钮栏 ==========
        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=9, column=0, columnspan=4, sticky="w", pady=(6, 0))

        self._start_btn = self._mk_btn(btn_frame, " ▶  开始下载已选项 ", self._start,
                                       bg="#0d6efd", fg="#ffffff", padx=12, pady=5)
        self._start_btn.pack(side="left", padx=(0, 10))

        self._stop_btn = self._mk_btn(btn_frame, " ■  停止 ", self._stop,
                                      state="disabled")
        self._stop_btn.pack(side="left", padx=(0, 10))

        self._open_btn = self._mk_btn(btn_frame, " 🗂  打开输出目录 ", self._open_out)
        self._open_btn.pack(side="left")

        # 系列下载功能 → 初始灰色（需 ADB 连接后才启用）
        self._set_series_ui_state(False)

    # ── 系列 UI 状态控制 ──

    def _set_series_ui_state(self, enabled: bool):
        """系列下载功能 → 只有 ADB 连接后才可用，其余时间保持灰色。"""
        state = "normal" if enabled else "disabled"
        disabled_bg = "#e2e8f0"
        disabled_fg = "#8c959f"
        normal_bg = "#ffffff"
        normal_fg = "#212529"

        # 系列下拉框
        self._series_cb.config(state="readonly" if enabled else "disabled")

        # 集数输入框
        self._ep_from_entry.config(state=state,
                                   bg=normal_bg if enabled else disabled_bg,
                                   fg=normal_fg if enabled else disabled_fg)
        self._ep_to_entry.config(state=state,
                                 bg=normal_bg if enabled else disabled_bg,
                                 fg=normal_fg if enabled else disabled_fg)

        # 按钮
        if enabled:
            self._btn_add_range.config(state="normal", bg="#0d6efd", fg="#ffffff")
            self._btn_fetch_books.config(state="normal", bg="#0d6efd", fg="#ffffff")
        else:
            self._btn_add_range.config(state="disabled", bg=disabled_bg, fg=disabled_fg)
            self._btn_fetch_books.config(state="disabled", bg=disabled_bg, fg=disabled_fg)

        # 系列信息标签
        if not enabled:
            self._series_info_label.config(text="（需通过 ADB 连接后可用）", fg="#adb5bd")

        # 搜索框
        if enabled:
            self._series_search_entry.config(state="normal",
                                            bg=normal_bg, fg=normal_fg,
                                            disabledbackground=normal_bg)
        else:
            self._series_search_entry.config(state="disabled",
                                            bg=disabled_bg, fg=disabled_fg,
                                            disabledbackground=disabled_bg)

    # ── 系列搜索 ──

    def _on_series_search(self):
        """根据搜索框内容过滤系列下拉列表。"""
        keyword = self._series_search_var.get().strip().lower()
        if not keyword or not self._all_series_titles:
            filtered = self._all_series_titles[:]
        else:
            filtered = [t for t in self._all_series_titles if keyword in t.lower()]

        self._series_cb["values"] = filtered
        if filtered:
            self._series_cb_var.set(filtered[0])
            self._on_series_selected()
            self._series_search_count_label.config(
                text=f"共 {len(filtered)} 个" if len(filtered) < len(self._all_series_titles) else "",
                fg=SUB_FG)
        else:
            self._series_cb_var.set("")
            self._series_cb.set("")
            self._series_info_label.config(text="无匹配结果", fg="#adb5bd")
            self._series_search_count_label.config(text="无匹配", fg="red")

    def _refresh_series_dropdown(self):
        """用 self._series_map 的当前内容刷新系列下拉列表。"""
        titles = sorted(self._series_map.keys(), key=lambda t: -self._series_map[t].count)
        self._all_series_titles = titles[:]
        self._series_search_var.set("")
        self._series_search_count_label.config(text="")
        self._on_series_search()

    # ── Token 相关 ──

    def _verify_token(self):
        token = self._token_var.get().strip()
        if not token or len(token) < 30:
            self._token_status_label.config(text="❌ 无效的 Token", fg="red")
            return
        self._token_status_label.config(text="⏳ 验证中…", fg=SUB_FG)
        self.update()

        user = verify_token(token)
        if user:
            self._token_valid = True
            self._token_user = user
            self._token_status_label.config(text=f"✅ 已认证: {user}", fg="green")
        else:
            self._token_valid = False
            self._token_status_label.config(text="❌ Token 无效或已过期", fg="red")

    @staticmethod
    def _find_adb() -> Optional[str]:
        """在 PATH 和常见安装路径下查找 adb 可执行文件。"""
        # 1. 检查 PATH
        adb_name = "adb.exe" if sys.platform == "win32" else "adb"
        adb_path = subprocess.run(["where" if sys.platform == "win32" else "which", adb_name],
                                  capture_output=True, text=True, timeout=5)
        if adb_path.returncode == 0:
            return adb_name  # 在 PATH 中，直接用名字

        # 2. 常见安装路径
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

        # 3. 尝试从注册表查找 Android SDK 路径（Windows）
        if sys.platform == "win32":
            try:
                import winreg
                for key in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
                    for subkey in [r"SOFTWARE\Android\SDK",
                                   r"SOFTWARE\Wow6432Node\Android\SDK"]:
                        try:
                            with winreg.OpenKey(key, subkey) as k:
                                sdk_path = winreg.QueryValueEx(k, "Path")[0]
                                adb_candidate = os.path.join(sdk_path, "platform-tools", "adb.exe")
                                if os.path.isfile(adb_candidate):
                                    return adb_candidate
                        except OSError:
                            pass
            except ImportError:
                pass

        return None

    @staticmethod
    def _get_adb_devices(adb_cmd: str) -> List[str]:
        """返回已连接（device 状态）的 ADB 设备序列号列表。"""
        try:
            result = subprocess.run([adb_cmd, "devices"],
                                    capture_output=True, text=True, timeout=5)
            devices = []
            for line in result.stdout.splitlines():
                if "\tdevice" in line and "devices" not in line:
                    serial = line.split("\t")[0].strip()
                    if serial:
                        devices.append(serial)
            return devices
        except Exception:
            return []

    @staticmethod
    def _adb_shell_grep_token(adb_cmd: str, serial: str) -> Optional[str]:
        """在指定设备上搜索 app.log 中的最新 access_token。

        用 adb shell "cat ..." 将日志拉到本地 + Python 正则提取，
        注意 adb shell 后的整条命令必须作为单个字符串参数传递。
        """
        try:
            log_path = "/storage/emulated/0/Android/data/com.meother.qmyz/files/logs/app.log"

            # 1) 检查文件存在
            check = subprocess.run(
                [adb_cmd, "-s", serial, "shell", f"ls {log_path}"],
                capture_output=True, text=True, timeout=5
            )
            if check.returncode != 0:
                return None

            # 2) adb shell "cat /path" → 单个字符串参数，避免 Windows 引号问题
            result = subprocess.run(
                [adb_cmd, "-s", serial, "shell", f"cat {log_path}"],
                capture_output=True, timeout=30
            )
            raw = result.stdout
            if not raw or len(raw) < 50:
                return None

            # 3) Python 正则提取 access_token
            tokens = re.findall(rb'"access_token":"([^"]+)"', raw)
            if tokens:
                return tokens[-1].decode()

            return None
        except subprocess.TimeoutExpired:
            return None
        except Exception:
            return None

    @staticmethod
    def _adb_pull_log(adb_cmd: str, serial: str) -> Optional[str]:
        """从设备下载 app.log 到本地临时文件，返回路径或 None。"""
        tmp_path = None
        try:
            tmp = tempfile.NamedTemporaryFile(mode="wb", suffix=".log", delete=False)
            tmp_path = tmp.name
            tmp.close()

            result = subprocess.run(
                [adb_cmd, "-s", serial, "shell",
                 "cat /storage/emulated/0/Android/data/com.meother.qmyz/files/logs/app.log"],
                capture_output=True, timeout=30
            )
            if not result.stdout or len(result.stdout) < 100:
                if tmp_path:
                    os.unlink(tmp_path)
                return None

            with open(tmp_path, "wb") as f:
                f.write(result.stdout)
            return tmp_path
        except Exception:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
            return None

    def _adb_parse_and_merge_log(self, adb_cmd: str, serial: str):
        """从 ADB 设备拉取 app.log → 解析系列/剧集 → 合并到 series_map → API 补全全集。"""
        log_path = self._adb_pull_log(adb_cmd, serial)
        if not log_path:
            self._token_status_label.config(text="⏳ 日志拉取失败，跳过日志解析", fg="orange")
            self.update()
            return

        self._token_status_label.config(text="⏳ 解析设备日志中的剧集…", fg=SUB_FG)
        self.update()

        try:
            log_series = parse_episodes(log_path)
            new_chapters = 0
            log_book_ids = {}  # {book_id: book_title}
            for title, si in log_series.items():
                if title not in self._series_map:
                    self._series_map[title] = si
                    new_chapters += len(si.chapters)
                else:
                    existing_ids = {c.episode_id for c in self._series_map[title].chapters
                                    if c.episode_id}
                    for ch in si.chapters:
                        if ch.episode_id and ch.episode_id not in existing_ids:
                            self._series_map[title].chapters.append(ch)
                            new_chapters += 1
                    self._series_map[title].chapters.sort(
                        key=lambda c: (c.episode_num, c.weight))
                if si.book_id:
                    log_book_ids[si.book_id] = title

            # 刷新下拉
            self._refresh_series_dropdown()

            if new_chapters > 0:
                self._token_status_label.config(
                    text=f"⏳ 日志解析完成，添加了 {new_chapters} 个剧集",
                    fg=SUB_FG)
            else:
                self._token_status_label.config(
                    text="⏳ 日志无新增剧集", fg=SUB_FG)
            self.update()

            # ===== 关键：用 API 补全日志中出现的书籍的全集 =====
            token = self._token_var.get().strip()
            new_book_ids = {bid for bid in log_book_ids
                            if bid not in self._api_completed_book_ids}
            if self._token_valid and new_book_ids:
                self._token_status_label.config(
                    text=f"⏳ API 补全 {len(new_book_ids)} 个日志书籍的全集…", fg=SUB_FG)
                self.update()
                for bid, btitle in log_book_ids.items():
                    if bid in self._api_completed_book_ids:
                        continue
                    self._api_completed_book_ids.add(bid)
                    self._token_status_label.config(
                        text=f"⏳ 补全《{btitle[:20]}》…", fg=SUB_FG)
                    self.update()
                    try:
                        api_eps = fetch_episodes_from_api(token, bid, log_fn=lambda m: None)
                    except Exception:
                        api_eps = []
                    if api_eps:
                        if btitle in self._series_map:
                            existing_ids = {c.episode_id for c in self._series_map[btitle].chapters
                                            if c.episode_id}
                            for ch in api_eps:
                                if ch.episode_id and ch.episode_id not in existing_ids:
                                    self._series_map[btitle].chapters.append(ch)
                            self._series_map[btitle].chapters.sort(
                                key=lambda c: (c.episode_num, c.weight))
                        else:
                            si = SeriesInfo(book_title=btitle, book_id=bid)
                            si.chapters = api_eps
                            self._series_map[btitle] = si
                self._refresh_series_dropdown()
                api_total = sum(s.count for s in self._series_map.values())
                self._token_status_label.config(
                    text=f"✅ 日志解析+API补全完成，共 {api_total} 集",
                    fg="green")
                self.update()

        except Exception as e:
            self._token_status_label.config(
                text=f"⏳ 日志解析跳过: {e}", fg="orange")
            self.update()
        finally:
            try:
                os.unlink(log_path)
            except Exception:
                pass

    def _adb_get_token(self):
        """从已连接的 ADB 设备获取最新 token → 成功即解锁系列下载功能。"""
        adb_cmd = self._find_adb()
        if not adb_cmd:
            self._token_status_label.config(
                text="❌ 未找到 adb.exe，请安装 Android platform-tools", fg="red")
            return

        devices = self._get_adb_devices(adb_cmd)
        if not devices:
            self._token_status_label.config(text="❌ 未检测到已连接的设备", fg="red")
            return

        self._token_status_label.config(
            text=f"📱 检测到 {len(devices)} 台设备: {', '.join(d[:8]+'…' for d in devices)}",
            fg=SUB_FG)
        self.update()

        # 遍历所有设备，取第一个找到 token 的
        for serial in devices:
            self._token_status_label.config(text=f"⏳ 读取 {serial[:10]}… 的 Token", fg=SUB_FG)
            self.update()
            token = self._adb_shell_grep_token(adb_cmd, serial)
            if token:
                self._token_var.set(token)
                self._token_status_label.config(text=f"✅ Token 已获取，验证中…", fg="green")
                self.update()
                user = verify_token(token)
                if user:
                    self._token_valid = True
                    self._token_user = user
                    self._adb_connected = True
                    self._set_series_ui_state(True)
                    self._token_status_label.config(
                        text=f"✅ {serial[:15]} · {user} · 解析日志中…", fg="green")
                    self.update()
                    # 先解析设备日志中的剧集
                    self._adb_parse_and_merge_log(adb_cmd, serial)
                    # 再通过 API 发现书籍（补充更多集）
                    self._fetch_books()
                    return
                else:
                    self._token_status_label.config(
                        text=f"⚠️ {serial[:15]} Token 无效，尝试下一台…", fg="orange")
                    continue

            self._token_status_label.config(
                text=f"⚠️ {serial[:15]} 未找到 Token（可能需要先打开 App）", fg="orange")
            self.update()

        self._token_status_label.config(text="❌ 所有设备均未找到有效 Token", fg="red")

    def _fetch_books(self):
        """通过 API 发现书籍并填充到系列列表。"""
        if not self._adb_connected:
            return
        token = self._token_var.get().strip()
        if not self._token_valid:
            self._token_status_label.config(text="❌ 请先验证 Token", fg="red")
            return

        self._token_status_label.config(text="⏳ 发现书籍中…", fg=SUB_FG)
        self.update()

        books = fetch_book_list(token)
        if not books:
            # 即便 API 无数据，日志解析可能已有系列，仍刷新下拉
            self._refresh_series_dropdown()
            self._add_series_to_tree(list(self._series_map.values()))
            self._token_status_label.config(
                text=f"✅ 已加载 {len(self._series_map)} 个系列（来自日志）"
                     if self._series_map else "❌ 未找到书籍", fg="green" if self._series_map else "red")
            return

        # 查询每本书的完整信息并填充到 series_map
        new_series = 0
        for book in books:
            bid = book["id"]
            title = book["title"]

            # 跳过已在日志解析阶段完成 API 补全的书籍
            if bid in self._api_completed_book_ids:
                continue

            # 先从 API 拉取这本书的剧集
            self._token_status_label.config(text=f"⏳ 拉取《{title[:20]}》…", fg=SUB_FG)
            self.update()

            episodes = []
            try:
                episodes = fetch_episodes_from_api(token, bid, log_fn=lambda m: None)
            except Exception as e:
                episodes = []

            if episodes:
                self._api_completed_book_ids.add(bid)
                # 合并 API 数据到 series_map（不覆盖已有的日志数据）
                if title in self._series_map:
                    existing_ids = {c.episode_id for c in self._series_map[title].chapters
                                    if c.episode_id}
                    for ch in episodes:
                        if ch.episode_id and ch.episode_id not in existing_ids:
                            self._series_map[title].chapters.append(ch)
                    self._series_map[title].chapters.sort(
                        key=lambda c: (c.episode_num, c.weight))
                else:
                    si = SeriesInfo(book_title=title, book_id=bid)
                    si.chapters = episodes
                    self._series_map[title] = si
                new_series += 1

        # 刷新下拉列表 + 加入下载列表
        self._refresh_series_dropdown()
        self._add_series_to_tree(list(self._series_map.values()))

        self._token_status_label.config(
            text=f"✅ 已加载 {len(self._series_map)} 个系列, 共 {sum(s.count for s in self._series_map.values())} 集",
            fg="green")

    def _add_series_to_tree(self, series_list: List[SeriesInfo]):
        """将系列的所有剧集加入下载列表。"""
        existing_urls = set()
        for ch in self._tree.get_children():
            vals = self._tree.item(ch, "values")
            existing_urls.add(vals[2])

        added = 0
        for si in series_list:
            for ch in si.chapters:
                if ch.oss_audio_url in existing_urls:
                    continue
                fn = ch.save_filename
                self._tree.insert("", "end", values=(fn, "音视频", ch.oss_audio_url))
                self._tree.selection_add(self._tree.get_children()[-1])
                existing_urls.add(ch.oss_audio_url)
                added += 1

        self._update_count_label()
        if added > 0:
            self._range_status_label.config(text=f"✅ 已添加 {added} 集")
            self.after(3000, lambda: self._range_status_label.config(text=""))

    # ── 列表操作 ──

    def _select_all(self):
        for ch in self._tree.get_children():
            self._tree.selection_add(ch)

    def _select_none(self):
        for ch in self._tree.get_children():
            self._tree.selection_remove(ch)

    def _select_media(self):
        self._select_none()
        for ch in self._tree.get_children():
            if self._tree.item(ch, "values")[1] == "音视频":
                self._tree.selection_add(ch)

    def _select_images(self):
        self._select_none()
        for ch in self._tree.get_children():
            if self._tree.item(ch, "values")[1] == "图片":
                self._tree.selection_add(ch)

    def _clear_list(self):
        for ch in self._tree.get_children():
            self._tree.delete(ch)
        self._count_label.config(text="待下载资源列表: (空)")

    # ── 文件选择 ──

    def _pick_log(self):
        p = filedialog.askopenfilename(title="选择 app.log", filetypes=[("日志文件", "*.log"), ("所有文件", "*.*")])
        if p:
            self._log_var.set(p)

    def _pick_out(self):
        p = filedialog.askdirectory(title="选择输出目录")
        if p:
            self._out_var.set(p)

    def _open_out(self):
        path = self._out_var.get()
        if not path or not os.path.exists(path):
            messagebox.showinfo("提示", "输出目录不存在，请先开始下载。")
            return
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.run(["open", path])
        else:
            subprocess.run(["xdg-open", path])

    # ── 日志变更 → 解析 ──

    def _on_log_path_change(self):
        path = self._log_var.get().strip()
        if not path or not os.path.isfile(path):
            return
        self._parse_and_populate_list()

    def _parse_and_populate_list(self):
        """选择日志后的总入口：更新系列下拉 + 填充旧版列表。"""
        log_path = self._log_var.get().strip()
        if not log_path or not os.path.isfile(log_path):
            return

        # 解析系列
        try:
            log_series = parse_episodes(log_path)
            for title, si in log_series.items():
                if title not in self._series_map:
                    self._series_map[title] = si
                else:
                    # 合并：日志中有更多/更新数据的话补充
                    existing_ids = {c.episode_id for c in self._series_map[title].chapters}
                    for ch in si.chapters:
                        if ch.episode_id not in existing_ids:
                            self._series_map[title].chapters.append(ch)
                    self._series_map[title].chapters.sort(key=lambda c: (c.episode_num, c.weight))
        except Exception as e:
            pass  # 日志解析失败不阻塞

        # 填充下拉
        self._refresh_series_dropdown()

        # 旧版解析（兜底）
        try:
            m3u8_items, image_items = parse_log_legacy(log_path)
            existing_urls = set()
            for ch in self._tree.get_children():
                existing_urls.add(self._tree.item(ch, "values")[2])

            for item in m3u8_items:
                if item["url"] not in existing_urls:
                    self._tree.insert("", "end", values=(item["name"], "音视频", item["url"]))
                    existing_urls.add(item["url"])
            for item in image_items:
                if item["url"] not in existing_urls:
                    self._tree.insert("", "end", values=(item["name"], "图片", item["url"]))
        except Exception as e:
            pass

        self._update_count_label()

    def _update_count_label(self):
        children = self._tree.get_children()
        total = len(children)
        media = sum(1 for ch in children if self._tree.item(ch, "values")[1] == "音视频")
        images = total - media
        self._count_label.config(text=f"待下载资源列表: 共 {total} 个 (音视频 {media} | 图片 {images})")

    # ── 系列选择 ──

    def _on_series_selected(self):
        if not self._adb_connected:
            return
        title = self._series_cb_var.get()
        si = self._series_map.get(title)
        if si:
            self._series_info_label.config(text=si.episode_range, fg=SUB_FG)
            self._ep_from_var.set(str(si.min_episode) if si.min_episode else "")
            self._ep_to_var.set(str(si.max_episode) if si.max_episode else "")
        else:
            self._series_info_label.config(text="")

    # ── 添加剧集范围 ──

    def _add_episode_range(self):
        if not self._adb_connected:
            messagebox.showwarning("提示", "请先通过「ADB获取Token」连接设备。")
            return
        series_title = self._series_cb_var.get()
        si = self._series_map.get(series_title)
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

        matched = [
            ch for ch in si.chapters
            if ep_from <= ch.episode_num <= ep_to
        ]
        if not matched:
            messagebox.showinfo("结果", f"在范围内未找到剧集。\n本系列范围为 {si.min_episode} ~ {si.max_episode} 集。")
            return

        existing_urls = set()
        for ch in self._tree.get_children():
            existing_urls.add(self._tree.item(ch, "values")[2])

        added = 0
        for ch in matched:
            if ch.oss_audio_url in existing_urls:
                continue
            fn = ch.save_filename
            self._tree.insert("", "end", values=(fn, "音视频", ch.oss_audio_url))
            self._tree.selection_add(self._tree.get_children()[-1])
            existing_urls.add(ch.oss_audio_url)
            added += 1

        self._update_count_label()
        self._range_status_label.config(text=f"✅ 已添加 {added} 集" if added else "⏭ 无新增")
        self.after(3000, lambda: self._range_status_label.config(text=""))

    # ── 下载控制 ──

    def _start(self):
        log_path = self._log_var.get().strip()
        out_dir = self._out_var.get().strip()

        if not out_dir:
            messagebox.showerror("错误", "请指定输出目录。")
            return

        selected_ids = self._tree.selection()
        if not selected_ids:
            messagebox.showerror("错误", "请从列表中选择要下载的资源。")
            return

        items = []
        for ch_id in selected_ids:
            fn, tp, url = self._tree.item(ch_id, "values")
            items.append({"filename": fn, "type": tp, "url": url})

        os.makedirs(out_dir, exist_ok=True)
        self._stop_event.clear()
        self._start_btn.config(state="disabled", bg="#e2e8f0", fg="#8c959f")
        self._stop_btn.config(state="normal", bg="#dc3545", fg="#ffffff")
        self._progress["value"] = 0
        self._progress_label.config(text="正在运行…")

        self._worker = threading.Thread(
            target=run_download_task,
            args=(items, out_dir, self._enqueue_log,
                  self._update_progress, self._on_done, self._stop_event),
            daemon=True,
        )
        self._worker.start()

    def _stop(self):
        self._stop_event.set()
        self._enqueue_log("\n[!] 正在等待当前任务完成后停止…")

    # ── 日志 / 进度回调 ──

    def _enqueue_log(self, msg: str):
        self._log_queue.put(msg)

    def _update_progress(self, done: int, total: int):
        if total > 0:
            pct = int(done / total * 100)
            self._log_queue.put(("__progress__", done, total, pct))

    def _on_done(self):
        self._log_queue.put("__done__")

    def _poll_log_queue(self):
        while not self._log_queue.empty():
            item = self._log_queue.get_nowait()
            if item == "__done__":
                self._start_btn.config(state="normal", bg="#0d6efd", fg="#ffffff")
                self._stop_btn.config(state="disabled", bg="#e2e8f0", fg="#8c959f")
                self._progress_label.config(text="完成！")
            elif isinstance(item, tuple) and item[0] == "__progress__":
                _, done, total, pct = item
                self._progress["value"] = pct
                self._progress_label.config(text=f"进度: {done}/{total}  ({pct}%)")
            else:
                print(str(item))
        self.after(120, self._poll_log_queue)


# ══════════════════════════════════════════════
#  启动
# ══════════════════════════════════════════════

if __name__ == "__main__":
    app = App()
    app.mainloop()
