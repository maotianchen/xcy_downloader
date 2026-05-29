import re
import os
import ssl
import urllib.request
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from datetime import datetime
import queue
import time
import subprocess
import sys
import json



def sanitize_filename(name: str, max_len: int = 120) -> str:
    """将 URL 或文本中的非法字符替换为下划线，防止路径冲突。"""
    return re.sub(r'[\\/:*?"<>|]', '_', name)[:max_len]


def fetch_url(url: str, ctx, timeout: int = 8) -> bytes:
    """带 UA 头的通用 HTTP 获取，返回原始 bytes。"""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    )
    return urllib.request.urlopen(req, context=ctx, timeout=timeout).read()




def parse_log(log_path: str):
    """
    扫描 app.log，解析其中的 JSON 返回值以建立 URL 到“原文件名/精美名称”的映射关系。
    同时使用正则作为降级兜底。
    返回:
      m3u8_items: [{'url': url, 'name': rich_name}]
      image_items: [{'url': url, 'name': rich_name}]
    """
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    
    m3u8_links = sorted(set(re.findall(r'https?://[^\s"\'<>]+?\.m3u8', content)))
    image_links = sorted(set(re.findall(
        r'https?://[^\s"\'<>]+?\.(?:png|jpg|jpeg|gif|webp)', content
    )))

    
    url_to_info = {}
    for url in m3u8_links:
        base_name = url.split("/")[-1].split(".m3u8")[0]
        url_to_info[url] = {"name": sanitize_filename(base_name), "type": "media", "source": "fallback"}
    for url in image_links:
        base_name = url.split("/")[-1].split("?")[0]
        url_to_info[url] = {"name": sanitize_filename(base_name), "type": "image", "source": "fallback"}

    
    lines = content.splitlines()
    for line in lines:
        if "{" in line and "}" in line:
            start_idx = line.find("{")
            json_str = line[start_idx:]
            try:
                data = json.loads(json_str)
                _extract_from_json(data, url_to_info)
            except Exception:
                pass

    
    m3u8_items = []
    image_items = []

    for url, inf in url_to_info.items():
        if inf["type"] == "media":
            
            ext = ".ts"
            if any(k in url.lower() for k in ["story", "audio", "music", "voice", "sound"]) or inf.get("source") == "chapter":
                ext = ".m4a"
            else:
                ext = ".mp4"
            
            name = inf["name"]
            if not any(name.endswith(e) for e in [".mp4", ".m4a", ".ts"]):
                name = f"{name}{ext}"
            m3u8_items.append({"url": url, "name": name})
        else:
            name = inf["name"]
            
            if not any(name.endswith(e) for e in [".png", ".jpg", ".jpeg", ".gif", ".webp"]):
                ext = url.split("/")[-1].split("?")[0].split(".")[-1]
                if len(ext) > 4 or not ext:
                    ext = "jpg"
                name = f"{name}.{ext}"
            image_items.append({"url": url, "name": name})

    
    m3u8_items.sort(key=lambda x: x["name"])
    image_items.sort(key=lambda x: x["name"])

    return m3u8_items, image_items


def _extract_from_json(data, url_to_info):
    """递归提取 JSON 中的 URL 与名称关联关系"""
    if isinstance(data, dict):
        book_title = data.get("bookTitle", "").strip()
        title = data.get("title", "").strip()
        
        
        oss_url = data.get("ossAudioUrl") or data.get("ossVideoUrl") or data.get("url")
        if oss_url and isinstance(oss_url, str) and ("http" in oss_url):
            name = ""
            if book_title and title:
                name = f"{book_title}-{title}"
            elif title:
                name = title
            elif data.get("name"):
                name = data.get("name").strip()
            
            if name:
                
                name = name.replace(" | ", " - ").replace("|", "-")
                name = sanitize_filename(name)
                is_media = ".m3u8" in oss_url or ".ts" in oss_url or "media" in oss_url or "chapter" in oss_url or "audio" in oss_url
                url_to_info[oss_url] = {
                    "name": name,
                    "type": "media" if is_media else "image",
                    "source": "chapter"
                }

        
        cover = data.get("coverImage") or data.get("headerUrl") or data.get("image")
        if cover and isinstance(cover, str) and ("http" in cover) and (title or data.get("nickname") or data.get("name")):
            sibling_name = title or data.get("nickname") or data.get("name")
            if sibling_name:
                sibling_name = sibling_name.replace(" | ", " - ").replace("|", "-")
                sibling_name = sanitize_filename(sibling_name.strip())
                url_to_info[cover] = {
                    "name": sibling_name,
                    "type": "image",
                    "source": "cover"
                }

        
        for v in data.values():
            _extract_from_json(v, url_to_info)
            
    elif isinstance(data, list):
        for item in data:
            _extract_from_json(item, url_to_info)




def resolve_m3u8(url: str, ctx) -> tuple:
    """
    拉取 M3U8 文件，解析：
      • 若为 master playlist，找出最高画质子串流
      • 提取解密 Key 信息（方法、URI、IV、媒体基准序列号）
      • 返回 (final_m3u8_url, key_method, key_url, iv, media_sequence, ts_segments_list)
    """
    raw = fetch_url(url, ctx).decode("utf-8", errors="replace")
    base = url.rsplit("/", 1)[0]

    
    if "#EXT-X-STREAM-INF" in raw:
        
        variants = re.findall(r'#EXT-X-STREAM-INF[^\n]*\n([^\n]+)', raw)
        if variants:
            child = variants[-1].strip()
            child_url = child if child.startswith("http") else f"{base}/{child}"
            return resolve_m3u8(child_url, ctx)

    
    key_method = None
    key_url = None
    iv = None
    key_match = re.search(r'#EXT-X-KEY:([^\n]+)', raw)
    if key_match:
        key_info = key_match.group(1)
        method_m = re.search(r'METHOD=([^,\s]+)', key_info)
        if method_m:
            key_method = method_m.group(1)
        uri_m = re.search(r'URI="([^"]+)"', key_info)
        if uri_m:
            ku = uri_m.group(1)
            key_url = ku if ku.startswith("http") else f"{base}/{ku}"
        iv_m = re.search(r'IV=0[xX]([0-9a-fA-F]+)', key_info)
        if iv_m:
            iv = bytes.fromhex(iv_m.group(1))

    media_sequence = 0
    seq_match = re.search(r'#EXT-X-MEDIA-SEQUENCE:(\d+)', raw)
    if seq_match:
        media_sequence = int(seq_match.group(1))

    segments = []
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            seg = line if line.startswith("http") else f"{base}/{line}"
            segments.append(seg)

    return url, key_method, key_url, iv, media_sequence, segments


def download_m3u8_as_ts(url: str, base_out_dir: str, save_filename: str, ctx, log_fn, stop_event):
    """
    逐片段下载 TS 并拼合为单个临时 .ts 文件，自动支持 AES-128 加密流解密，
    然后自动探测该流是视频还是纯音频，并采用精确指定的富文本名保存：
      - 视频：保存至 video 目录，格式转为 .mp4
      - 纯音频：保存至 music 目录，格式转为 .m4a (无损)
    """
    base_name = save_filename.rsplit(".", 1)[0]
    temp_ts_path = os.path.join(base_out_dir, f"temp_{base_name}.ts")
    
    try:
        _, key_method, key_url, iv, media_sequence, segments = resolve_m3u8(url, ctx)
        if not segments:
            log_fn(f"  [!] {base_name}: 未找到 TS 片段，跳过。")
            return False

        
        key_bytes = None
        if key_method == "AES-128" and key_url:
            for attempt in range(1, 4):
                try:
                    from Crypto.Cipher import AES
                    log_fn(f"  [🔓] 检测到 AES-128 加密，正在获取密钥 (尝试 {attempt}/3)...")
                    key_bytes = fetch_url(key_url, ctx, timeout=20)
                    if len(key_bytes) == 16:
                        break
                    else:
                        log_fn(f"  [!] 警告: 密钥长度异常 ({len(key_bytes)} 字节)，重试中...")
                except Exception as e:
                    log_fn(f"  [!] 密钥获取失败 (尝试 {attempt}/3): {e}")
                    if attempt < 3:
                        time.sleep(2)
            
            if not key_bytes or len(key_bytes) != 16:
                log_fn("  [✗] 无法获取有效的 AES 密钥，分片将无法被正确解密！")

        total = len(segments)
        log_fn(f"  [→] 开始下载共 {total} 个分片...")

        with open(temp_ts_path, "wb") as out_f:
            for seg_idx, seg_url in enumerate(segments):
                if stop_event.is_set():
                    log_fn("  [×] 用户中止下载。")
                    if os.path.exists(temp_ts_path):
                        os.remove(temp_ts_path)
                    return False
                try:
                    data = fetch_url(seg_url, ctx)
                    
                    
                    if key_bytes:
                        from Crypto.Cipher import AES
                        seg_iv = iv if iv else (media_sequence + seg_idx).to_bytes(16, "big")
                        cipher = AES.new(key_bytes, AES.MODE_CBC, seg_iv)
                        data = cipher.decrypt(data)
                        
                        if len(data) > 0:
                            pad_len = data[-1]
                            if 0 < pad_len <= 16:
                                if data[-pad_len:] == bytes([pad_len] * pad_len):
                                    data = data[:-pad_len]

                    out_f.write(data)
                    
                    
                    i = seg_idx + 1
                    if i % max(1, total // 10) == 0 or i == total:
                        log_fn(f"      片段 {i}/{total}")
                except Exception as e:
                    log_fn(f"      [!] 片段 {seg_idx + 1} 失败: {e}")

        
        is_video = True
        try:
            result = subprocess.run(
                ["ffmpeg", "-i", temp_ts_path],
                capture_output=True, text=True, errors="ignore", timeout=10
            )
            if "Video:" not in result.stderr and "Audio:" in result.stderr:
                is_video = False
        except FileNotFoundError:
            if any(k in url.lower() for k in ["story", "audio", "music", "voice", "sound"]):
                is_video = False

        if is_video:
            target_dir = os.path.join(base_out_dir, "video")
            final_name = base_name
            target_ext = ".mp4"
            log_fn(f"  [🎥] 检测为视频流，分类至 {os.path.basename(target_dir)} 目录")
        else:
            target_dir = os.path.join(base_out_dir, "music")
            final_name = base_name
            target_ext = ".m4a"
            log_fn(f"  [🎵] 检测为音频流，分类至 {os.path.basename(target_dir)} 目录")

        os.makedirs(target_dir, exist_ok=True)
        final_path = os.path.join(target_dir, f"{final_name}{target_ext}")

        
        converted = False
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", temp_ts_path, "-c", "copy", final_path],
                capture_output=True, timeout=120, check=True
            )
            converted = True
            log_fn(f"  [✓] 已成功转封装: {os.path.join(os.path.basename(target_dir), f'{final_name}{target_ext}')}")
        except Exception:
            pass

        
        if os.path.exists(temp_ts_path):
            if converted:
                os.remove(temp_ts_path)
            else:
                fallback_path = os.path.join(target_dir, f"{final_name}.ts")
                os.rename(temp_ts_path, fallback_path)
                log_fn(f"  [✓] ffmpeg 转换失败，已直接保存原始流: {os.path.join(os.path.basename(target_dir), f'{final_name}.ts')}")

        return True

    except Exception as e:
        log_fn(f"  [✗] {base_name} 下载失败: {e}")
        if os.path.exists(temp_ts_path):
            os.remove(temp_ts_path)
        return False




def download_image(url: str, out_dir: str, save_filename: str, ctx, log_fn, stop_event):
    """下载单张图片，按指定文件名保存。"""
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




def run_download_task(selected_items: list, out_dir: str,
                      log_fn, progress_fn, done_fn, stop_event):
    """在后台线程中执行精选列表的下载流程。"""
    ctx = ssl._create_unverified_context()
    log_fn("=" * 60)
    log_fn(f"[{datetime.now().strftime('%H:%M:%S')}] 开始下载任务")
    log_fn(f"输出目录: {out_dir}")
    log_fn(f"选择下载 → 总数: {len(selected_items)} 个资源")
    log_fn("=" * 60)

    total_steps = len(selected_items)
    completed = 0

    for idx, item in enumerate(selected_items, 1):
        if stop_event.is_set():
            break
        filename = item["filename"]
        itype = item["type"]
        url = item["url"]
        
        if itype == "音视频":
            log_fn(f"\n[{idx}/{total_steps}] 正在分析/下载: {filename}")
            download_m3u8_as_ts(url, out_dir, filename, ctx, log_fn, stop_event)
            
            if idx < total_steps and not stop_event.is_set():
                time.sleep(1.5)
        elif itype == "图片":
            log_fn(f"\n[{idx}/{total_steps}] 正在下载图片: {filename}")
            image_dir = os.path.join(out_dir, "picture")
            os.makedirs(image_dir, exist_ok=True)
            download_image(url, image_dir, filename, ctx, log_fn, stop_event)
            
        completed += 1
        progress_fn(completed, total_steps)

    if stop_event.is_set():
        log_fn("\n[!] 任务已被用户中止。")
    else:
        log_fn(f"\n[{datetime.now().strftime('%H:%M:%S')}] 全部下载完成！")
    done_fn()




class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("星次元下载器 v1.1.0 by maotianchen")
        self.resizable(True, True)
        self.minsize(840, 580)
        self.configure(bg="#f8f9fa")

        self._stop_event = threading.Event()
        self._log_queue = queue.Queue()
        self._worker = None

        self._build_ui()
        self._poll_log_queue()

    def _build_ui(self):
        PAD = 12
        BG = "#f8f9fa"
        PANEL = "#ffffff"
        BORDER = "#dee2e6"
        ACC = "#495057"
        ACC_LIGHT = "#e9ecef"
        FG = "#212529"
        SUB_FG = "#495057"
        ENTRY_BG = "#ffffff"
        ENTRY_FG = "#212529"

        FONT_FAMILY = "Microsoft YaHei"
        FONT_NORMAL = (FONT_FAMILY, 9)
        FONT_BOLD = (FONT_FAMILY, 9, "bold")
        FONT_TITLE = (FONT_FAMILY, 12, "bold")

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

        
        title_frame = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        title_frame.pack(fill="x")
        tk.Label(title_frame, text="  ▶  星次元下载器 v1.1.0",
                 bg=PANEL, fg=ACC, font=FONT_TITLE).pack(side="left", padx=PAD, pady=10)

        
        main = ttk.Frame(self, padding=PAD)
        main.pack(fill="both", expand=True, padx=PAD, pady=PAD)
        main.columnconfigure(1, weight=1)

        
        ttk.Label(main, text="app.log 路径:").grid(row=0, column=0, sticky="w", pady=6)
        self._log_var = tk.StringVar()
        
        
        self._log_var.trace_add("write", lambda *a: self._parse_and_populate_list())

        log_entry = tk.Entry(main, textvariable=self._log_var, bg=ENTRY_BG, fg=ENTRY_FG,
                              insertbackground=ENTRY_FG, relief="solid", bd=1,
                              highlightcolor=ACC, highlightthickness=1, highlightbackground=BORDER,
                              font=("Consolas", 10))
        log_entry.grid(row=0, column=1, sticky="ew", padx=8, ipady=3)
        
        btn_browse_log = tk.Button(main, text=" 浏览... ", command=self._pick_log,
                                   bg=ACC_LIGHT, fg=FG, activebackground=BORDER, activeforeground=FG,
                                   relief="flat", bd=0, font=FONT_NORMAL, cursor="hand2", padx=10, pady=2)
        btn_browse_log.grid(row=0, column=2, sticky="ew")

        
        ttk.Label(main, text="输出目录:").grid(row=1, column=0, sticky="w", pady=6)
        self._out_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads", "media_output"))
        out_entry = tk.Entry(main, textvariable=self._out_var, bg=ENTRY_BG, fg=ENTRY_FG,
                              insertbackground=ENTRY_FG, relief="solid", bd=1,
                              highlightcolor=ACC, highlightthickness=1, highlightbackground=BORDER,
                              font=("Consolas", 10))
        out_entry.grid(row=1, column=1, sticky="ew", padx=8, ipady=3)
        
        btn_browse_out = tk.Button(main, text=" 浏览... ", command=self._pick_out,
                                   bg=ACC_LIGHT, fg=FG, activebackground=BORDER, activeforeground=FG,
                                   relief="flat", bd=0, font=FONT_NORMAL, cursor="hand2", padx=10, pady=2)
        btn_browse_out.grid(row=1, column=2, sticky="ew")

        
        helper_frame = ttk.Frame(main)
        helper_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        
        self._count_label = ttk.Label(helper_frame, text="待下载资源列表: (请先选择日志文件)", font=FONT_BOLD)
        self._count_label.pack(side="left")
        
        btn_sel_all = tk.Button(helper_frame, text=" 全选 ", command=self._select_all,
                                bg=ACC_LIGHT, fg=FG, activebackground=BORDER, relief="flat", bd=0,
                                font=FONT_NORMAL, cursor="hand2", padx=6)
        btn_sel_all.pack(side="right", padx=2)
        
        btn_sel_none = tk.Button(helper_frame, text=" 全不选 ", command=self._select_none,
                                 bg=ACC_LIGHT, fg=FG, activebackground=BORDER, relief="flat", bd=0,
                                 font=FONT_NORMAL, cursor="hand2", padx=6)
        btn_sel_none.pack(side="right", padx=2)

        btn_sel_media = tk.Button(helper_frame, text=" 仅选音视频 ", command=self._select_media,
                                  bg=ACC_LIGHT, fg=FG, activebackground=BORDER, relief="flat", bd=0,
                                  font=FONT_NORMAL, cursor="hand2", padx=6)
        btn_sel_media.pack(side="right", padx=2)

        btn_sel_image = tk.Button(helper_frame, text=" 仅选图片 ", command=self._select_images,
                                  bg=ACC_LIGHT, fg=FG, activebackground=BORDER, relief="flat", bd=0,
                                  font=FONT_NORMAL, cursor="hand2", padx=6)
        btn_sel_image.pack(side="right", padx=2)

        
        tree_frame = ttk.Frame(main)
        tree_frame.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=4)
        main.rowconfigure(3, weight=1)

        style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff", foreground=FG,
                        font=FONT_NORMAL, rowheight=24)
        style.configure("Treeview.Heading", font=FONT_BOLD, background=ACC_LIGHT)

        self._tree = ttk.Treeview(tree_frame, columns=("filename", "type", "url"), show="headings", selectmode="extended")
        self._tree.heading("filename", text="展示/保存文件名")
        self._tree.heading("type", text="资源类型")
        self._tree.heading("url", text="远程资源链接")

        self._tree.column("filename", width=360, anchor="w")
        self._tree.column("type", width=90, anchor="center")
        self._tree.column("url", width=320, anchor="w")

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        
        self._tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        
        self._progress = ttk.Progressbar(main, orient="horizontal", mode="determinate",
                                          style="Horizontal.TProgressbar")
        self._progress.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self._progress_label = ttk.Label(main, text="等待开始…", foreground=SUB_FG)
        self._progress_label.grid(row=5, column=0, columnspan=3, sticky="w", pady=2)

        
        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))
        
        self._start_btn = tk.Button(btn_frame, text=" ▶  开始下载已选项 ", command=self._start,
                                    bg="#0d6efd", fg="#ffffff", activebackground="#0b5ed7", activeforeground="#ffffff",
                                    relief="flat", bd=0, font=FONT_BOLD, cursor="hand2", padx=12, pady=5)
        self._start_btn.pack(side="left", padx=(0, 10))
        
        self._stop_btn = tk.Button(btn_frame, text=" ■  停止 ", command=self._stop,
                                   bg="#e2e8f0", fg="#8c959f", activebackground="#bb2d3b", activeforeground="#ffffff",
                                   relief="flat", bd=0, font=FONT_BOLD, cursor="hand2", padx=12, pady=5, state="disabled")
        self._stop_btn.pack(side="left", padx=(0, 10))
        
        self._open_btn = tk.Button(btn_frame, text=" 🗂  打开输出目录 ", command=self._open_out,
                                   bg=ACC_LIGHT, fg=FG, activebackground=BORDER, activeforeground=FG,
                                   relief="flat", bd=0, font=FONT_BOLD, cursor="hand2", padx=12, pady=5)
        self._open_btn.pack(side="left")

    def _set_btn_state(self, btn, state):
        if btn == self._start_btn:
            if state == "disabled":
                btn.config(state="disabled", bg="#e2e8f0", fg="#8c959f")
            else:
                btn.config(state="normal", bg="#0d6efd", fg="#ffffff")
        elif btn == self._stop_btn:
            if state == "disabled":
                btn.config(state="disabled", bg="#e2e8f0", fg="#8c959f")
            else:
                btn.config(state="normal", bg="#dc3545", fg="#ffffff")

    
    def _select_all(self):
        for child in self._tree.get_children():
            self._tree.selection_add(child)

    def _select_none(self):
        for child in self._tree.get_children():
            self._tree.selection_remove(child)

    def _select_media(self):
        self._select_none()
        for child in self._tree.get_children():
            itype = self._tree.item(child, "values")[1]
            if itype == "音视频":
                self._tree.selection_add(child)

    def _select_images(self):
        self._select_none()
        for child in self._tree.get_children():
            itype = self._tree.item(child, "values")[1]
            if itype == "图片":
                self._tree.selection_add(child)

    def _parse_and_populate_list(self):
        log_path = self._log_var.get().strip()
        if not log_path or not os.path.isfile(log_path):
            return
        
        try:
            for child in self._tree.get_children():
                self._tree.delete(child)
            
            m3u8_items, image_items = parse_log(log_path)
            
            
            for item in m3u8_items:
                item_id = self._tree.insert("", "end", values=(item["name"], "音视频", item["url"]))
                self._tree.selection_add(item_id)  

            
            for item in image_items:
                item_id = self._tree.insert("", "end", values=(item["name"], "图片", item["url"]))
                self._tree.selection_add(item_id)  

            self._count_label.config(text=f"待下载资源列表: 共 {len(m3u8_items) + len(image_items)} 个 (音视频 {len(m3u8_items)} | 图片 {len(image_items)})")
        except Exception as e:
            messagebox.showerror("解析错误", f"解析日志文件失败: {e}")

    
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

    
    def _start(self):
        log_path = self._log_var.get().strip()
        out_dir  = self._out_var.get().strip()

        if not log_path or not os.path.isfile(log_path):
            messagebox.showerror("错误", "请选择有效的 app.log 文件。")
            return
        if not out_dir:
            messagebox.showerror("错误", "请指定输出目录。")
            return

        
        selected_ids = self._tree.selection()
        if not selected_ids:
            messagebox.showerror("错误", "列表为空或您没有勾选（选择）任何资源，请在列表中选择需要下载的行。")
            return

        selected_items = []
        for child_id in selected_ids:
            values = self._tree.item(child_id, "values")
            filename, itype, url = values
            selected_items.append({
                "filename": filename,
                "type": itype,
                "url": url
            })

        os.makedirs(out_dir, exist_ok=True)
        self._stop_event.clear()
        self._set_btn_state(self._start_btn, "disabled")
        self._set_btn_state(self._stop_btn, "normal")
        self._progress["value"] = 0
        self._progress_label.config(text="正在运行…")

        self._worker = threading.Thread(
            target=run_download_task,
            args=(
                selected_items, out_dir,
                self._enqueue_log,
                self._update_progress,
                self._on_done,
                self._stop_event,
            ),
            daemon=True
        )
        self._worker.start()

    def _stop(self):
        self._stop_event.set()
        self._enqueue_log("\n[!] 正在等待当前任务完成后停止…")

    
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
                self._set_btn_state(self._start_btn, "normal")
                self._set_btn_state(self._stop_btn, "disabled")
                self._progress_label.config(text="完成！")
            elif isinstance(item, tuple) and item[0] == "__progress__":
                _, done, total, pct = item
                self._progress["value"] = pct
                self._progress_label.config(text=f"进度: {done}/{total}  ({pct}%)")
            else:
                print(str(item))
        self.after(120, self._poll_log_queue)


if __name__ == "__main__":
    app = App()
    app.mainloop()
