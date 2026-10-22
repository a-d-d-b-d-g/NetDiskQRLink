#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
二维码 → 网盘分享链接  |  NetDisk QR Link Extractor  v1.0

纯本地离线运行：不联网、不上传、不调用任何网盘接口。
原理：网盘分享二维码里存的就是分享链接文本本身，解码即得链接。

输入方式：粘贴剪贴板截图 / 拖拽图片 / 选择图片(可多选) / 摄像头实时扫描 / 自动监听剪贴板
解码引擎：OpenCV QRCodeDetector(单码+多码) + pyzbar，配合 6 种图像预处理容错
解析能力：链接提取、网盘类型识别、提取码分离、规范化可复制链接
"""

import os
import re
import sys
import time
import hashlib
import traceback
import webbrowser
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# ----------------------------------------------------------------------------
# 依赖检测（缺失时给出可执行提示，而不是抛一堆 traceback）
# ----------------------------------------------------------------------------
_MISSING = []
try:
    from PIL import Image, ImageGrab, ImageOps, ImageTk
except Exception:
    _MISSING.append("pillow")

try:
    import numpy as np
except Exception:
    np = None
    _MISSING.append("numpy")

_HAS_CV2 = True
try:
    import cv2
except Exception:
    _HAS_CV2 = False

_HAS_ZBAR = True
try:
    from pyzbar.pyzbar import decode as zbar_decode
except Exception:
    _HAS_ZBAR = False

if _MISSING:
    sys.stderr.write(
        "缺少依赖：" + ", ".join(_MISSING) + "\n请先运行： pip install pillow numpy opencv-python pyzbar\n"
    )


# ============================================================================
# 1. 网盘识别规则
# ============================================================================
NETDISK_RULES: List[Tuple[str, str]] = [
    ("百度网盘", r"pan\.baidu\.com|baidu\.com/s/|eyun\.baidu\.com"),
    ("夸克网盘", r"pan\.quark\.cn|quark\.cn/s/"),
    ("阿里云盘", r"alipan\.com|aliyundrive\.com"),
    ("UC 网盘", r"drive\.uc\.cn"),
    ("123 云盘", r"123pan\.com|123684\.com|123865\.com|123912\.com|123pan\.cn"),
    ("迅雷云盘", r"pan\.xunlei\.com"),
    ("天翼云盘", r"cloud\.189\.cn"),
    ("移动云盘/和彩云", r"yun\.139\.com|caiyun\.139\.com"),
    ("115 网盘", r"115\.com|115cdn\.com"),
    ("腾讯微云", r"share\.weiyun\.com|weiyun\.com"),
    ("蓝奏云", r"lanzou[a-z]?\.(com|cn|in)|lanzn\.com|lanzoux\.com"),
    ("坚果云", r"jianguoyun\.com"),
    ("Google Drive", r"drive\.google\.com|docs\.google\.com"),
    ("OneDrive", r"1drv\.ms|onedrive\.live\.com"),
    ("Dropbox", r"dropbox\.com"),
    ("MEGA", r"mega\.nz"),
]

# 提取码关键字（注意：先匹配带关键字的形式，最后才吃裸 pwd 参数）
PWD_PATTERNS = [
    r"[?&]pwd=([0-9a-zA-Z]{3,8})",
    r"提取码[：:\s]*([0-9a-zA-Z]{3,8})",
    r"访问码[：:\s]*([0-9a-zA-Z]{3,8})",
    r"密码[：:\s]*([0-9a-zA-Z]{3,8})",
    r"驗證碼[：:\s]*([0-9a-zA-Z]{3,8})",
    r"口令[：:\s]*([0-9a-zA-Z]{3,8})",
    r"码[：:\s]*([0-9a-zA-Z]{3,8})",
]

URL_RE = re.compile(r"https?://[^\s\"'<>()（）〔〕【】\[\]，,、；;。]+", re.I)

# 链接末尾常见脏字符
_TAIL_JUNK = ".,;:!?)]}>\"'，。、；：！？）】」』"


# ============================================================================
# 2. 数据结构
# ============================================================================
@dataclass
class ParseResult:
    raw: str = ""                      # 二维码原始内容
    url: str = ""                      # 提取/规范化后的链接
    netdisk: str = "未识别"             # 网盘类型
    pwd: str = ""                      # 提取码
    extra: List[str] = field(default_factory=list)  # 其它附加信息
    source: str = ""                   # 来源文件名
    engine: str = ""                   # 命中引擎
    ts: float = field(default_factory=time.time)

    @property
    def is_netdisk(self) -> bool:
        return self.netdisk != "未识别"

    def copy_text(self) -> str:
        """用户最想要的一行：链接 + 提取码。"""
        if not self.url:
            return self.raw
        if self.pwd:
            return f"{self.url} 提取码: {self.pwd}"
        return self.url


# ============================================================================
# 3. 解析逻辑
# ============================================================================
def detect_netdisk(text: str) -> str:
    low = text.lower()
    for name, pat in NETDISK_RULES:
        if re.search(pat, low):
            return name
    return "未识别"


def _clean_url(u: str) -> str:
    return u.rstrip(_TAIL_JUNK)


def extract_url(text: str) -> str:
    m = URL_RE.search(text)
    if m:
        return _clean_url(m.group(0))
    # 无协议头的裸域名（如 pan.baidu.com/s/xxx）
    m = re.search(r"(?:[a-z0-9-]+\.)+(?:com|cn|net|me|ms|nz|io|xyz|top|cc)/[^\s\"'<>()，,、]+", text, re.I)
    if m:
        return "https://" + _clean_url(m.group(0))
    return ""


def extract_pwd(text: str) -> str:
    for pat in PWD_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1)
    return ""


def _append_pwd_param(url: str, pwd: str) -> str:
    """对支持 pwd 参数的网盘，把提取码拼进链接，做成点开即用的一体链接。"""
    if not pwd or not url:
        return url
    host_ok = re.search(r"pan\.baidu\.com|pan\.quark\.cn|drive\.uc\.cn|123pan|alipan|pan\.xunlei", url, re.I)
    if not host_ok:
        return url
    if re.search(r"[?&]pwd=", url, re.I):
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}pwd={pwd}"


def parse_content(text: str, source: str = "", engine: str = "") -> ParseResult:
    raw = (text or "").strip()
    res = ParseResult(raw=raw, source=source, engine=engine)
    if not raw:
        return res

    res.url = extract_url(raw)
    res.netdisk = detect_netdisk(raw if raw else "")
    res.pwd = extract_pwd(raw) or (extract_pwd(res.url) if res.url else "")

    # 链接里自带 pwd 但没被上面抓到（极小概率），补一次
    if not res.pwd and res.url:
        m = re.search(r"[?&]pwd=([0-9a-zA-Z]{3,8})", res.url, re.I)
        if m:
            res.pwd = m.group(1)

    if res.pwd:
        res.url = _append_pwd_param(res.url, res.pwd)

    # 附加信息：文件名提示
    name_hint = re.search(r"分享(?:的)?文件[：:\s]*([^\n]{1,60})", raw)
    if name_hint:
        res.extra.append("文件名：" + name_hint.group(1).strip())

    if not res.url and res.netdisk != "未识别":
        res.extra.append("识别到网盘关键词，但未解析出完整链接")

    return res


# ============================================================================
# 4. 解码引擎（多引擎 + 多预处理容错）
# ============================================================================
def _pil_variants(img: "Image.Image"):
    """生成多种预处理变体，提升反色/低清/小尺寸二维码的识别率。"""
    img = img.convert("RGB") if img.mode not in ("RGB", "L") else img
    yield "原图", img

    gray = img.convert("L")
    yield "灰度", gray

    w, h = gray.size
    if max(w, h) < 1000:
        scale = min(3, max(2, 1000 // max(w, h) + 1))
        try:
            yield f"放大{scale}x", gray.resize((w * scale, h * scale), Image.LANCZOS)
        except Exception:
            pass

    try:
        yield "反色", ImageOps.invert(gray)
    except Exception:
        pass

    if np is not None:
        try:
            arr = np.array(gray)
            _, bw = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            yield "OTSU二值化", Image.fromarray(bw)
            yield "OTSU反色", Image.fromarray(255 - bw)
        except Exception:
            pass
        try:
            arr = np.array(gray)
            ada = cv2.adaptiveThreshold(
                arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5
            )
            yield "自适应二值化", Image.fromarray(ada)
        except Exception:
            pass


def _decode_cv2(img: "Image.Image") -> List[str]:
    out: List[str] = []
    if not _HAS_CV2:
        return out
    try:
        arr = np.array(img.convert("L"))
        det = cv2.QRCodeDetector()
        # 多码优先
        try:
            ok, texts, _pts, _ = det.detectAndDecodeMulti(arr)
            if ok and texts:
                out.extend([t for t in texts if t])
                if out:
                    return out
        except Exception:
            pass
        try:
            txt, _pts, _ = det.detectAndDecode(arr)
            if txt:
                out.append(txt)
        except Exception:
            pass
    except Exception:
        pass
    return out


def _decode_zbar(img: "Image.Image") -> List[str]:
    out: List[str] = []
    if not _HAS_ZBAR:
        return out
    try:
        for obj in zbar_decode(img):
            try:
                val = obj.data.decode("utf-8", errors="replace")
            except Exception:
                val = str(obj.data)
            if val:
                out.append(val)
    except Exception:
        pass
    return out


def decode_image(img: "Image.Image", deep: bool = True) -> Tuple[List[str], str]:
    """返回 (解码出的文本列表, 命中引擎说明)。deep=True 时逐级加大预处理力度。"""
    seen: List[str] = []
    hit_engine = ""

    def _push(vals: List[str], engine: str):
        nonlocal hit_engine
        added = False
        for v in vals:
            v = v.strip()
            if v and v not in seen:
                seen.append(v)
                added = True
        if added and not hit_engine:
            hit_engine = engine

    for name, variant in _pil_variants(img):
        _push(_decode_zbar(variant), f"pyzbar/{name}")
        _push(_decode_cv2(variant), f"opencv/{name}")
        if seen and not deep:
            break
        if seen and name.startswith(("反色", "OTSU")):
            # 已经有结果，再补一轮即可，避免无谓耗时
            pass
    return seen, hit_engine


def decode_file(path: str, deep: bool = True) -> List[ParseResult]:
    results: List[ParseResult] = []
    try:
        img = Image.open(path)
        img.load()
    except Exception:
        return results
    texts, engine = decode_image(img, deep=deep)
    fname = os.path.basename(path)
    for t in texts:
        results.append(parse_content(t, source=fname, engine=engine))
    return results


# ============================================================================
# 5. GUI
# ============================================================================
BG = "#eef2f9"
CARD = "#ffffff"
INK = "#1e2733"
SUB = "#6b7a90"
ACCENT = "#2563eb"
ACCENT_DK = "#1d4ed8"
OK = "#0f9d58"
WARN = "#c2410c"
LINE = "#d8e0ec"

FONT = ("Microsoft YaHei UI", 10)
FONT_S = ("Microsoft YaHei UI", 9)
FONT_B = ("Microsoft YaHei UI", 10, "bold")
FONT_T = ("Microsoft YaHei UI", 17, "bold")
FONT_MONO = ("Consolas", 10)


DEFAULT_SETTINGS = {"watch": True, "auto_open": True}


def _settings_file():
    """设置文件路径：优先 exe 同目录（便携），不可写则退回 %LOCALAPPDATA%。"""
    import sys
    import os

    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(os.path.dirname(sys.executable), "settings.json"))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json"))
    for path in candidates:
        try:
            folder = os.path.dirname(path)
            if not os.path.isdir(folder):
                os.makedirs(folder, exist_ok=True)
            if os.path.exists(path) or os.access(folder, os.W_OK):
                return path
        except Exception:
            continue
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "NetDiskQRLink")
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        pass
    return os.path.join(folder, "settings.json")


SETTINGS_FILE = _settings_file()


def load_settings():
    """读取上次退出时的勾选状态，缺失/损坏则用默认值。"""
    import os
    import json

    cfg = dict(DEFAULT_SETTINGS)
    if not os.path.exists(SETTINGS_FILE):
        return cfg
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return cfg
    if isinstance(data, dict):
        for key in DEFAULT_SETTINGS:
            if isinstance(data.get(key), bool):
                cfg[key] = data[key]
    return cfg


def save_settings(values):
    """勾选一变就落盘，不等退出。"""
    import json

    try:
        def _bool_of(item):
            getter = getattr(item, "get", None)
            return bool(getter()) if callable(getter) else bool(item)

        data = {key: _bool_of(values[key]) for key in DEFAULT_SETTINGS if key in values}
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _apply_window_icon(root):
    """把 exe 内嵌图标套用到窗口/任务栏，避免 Tk 默认羽毛图标覆盖 exe 图标。"""
    import sys
    import os
    import ctypes
    from ctypes import wintypes

    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(sys.executable)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "NetDiskQRLink.ico"))

    try:
        root.update_idletasks()
        hwnd = int(root.wm_frame(), 16)
    except Exception:
        return

    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    set_class = getattr(user32, "SetClassLongPtrW", None) or user32.SetClassLongW

    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        try:
            large = (wintypes.HICON * 1)()
            small = (wintypes.HICON * 1)()
            got = shell32.ExtractIconExW(path, 0, large, small, 1)
            if not got or not large[0]:
                continue
            user32.SendMessageW(hwnd, 0x0080, 1, large[0])   # WM_SETICON, ICON_BIG
            set_class(hwnd, -14, large[0])                    # GCLP_HICON
            if small[0]:
                user32.SendMessageW(hwnd, 0x0080, 0, small[0])
                set_class(hwnd, -34, small[0])                # GCLP_HICONSM
            try:
                root.iconbitmap(default=path)
            except Exception:
                pass
            return
        except Exception:
            continue



def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    # 可选增强：拖拽
    has_dnd = False
    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES

        root = TkinterDnD.Tk()
        has_dnd = True
    except Exception:
        root = tk.Tk()

    root.title("网盘二维码 → 分享链接  v1.0")
    root.geometry("1000x680")
    root.minsize(880, 600)
    root.configure(bg=BG)

    _apply_window_icon(root)

    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("TButton", font=FONT, padding=(12, 7), background=CARD, foreground=INK,
                    borderwidth=0, focusthickness=0)
    style.map("TButton", background=[("active", "#dfe8f7")])
    style.configure("Accent.TButton", font=FONT_B, padding=(14, 9), background=ACCENT,
                    foreground="white", borderwidth=0)
    style.map("Accent.TButton", background=[("active", ACCENT_DK)])
    style.configure("Ghost.TButton", font=FONT, padding=(10, 6), background=BG, foreground=SUB)
    style.map("Ghost.TButton", background=[("active", "#e2e9f5")])
    style.configure("TCheckbutton", background=CARD, font=FONT_S, foreground=SUB)
    style.configure("Treeview", font=FONT_S, rowheight=26, background=CARD, fieldbackground=CARD,
                    borderwidth=0)
    style.configure("Treeview.Heading", font=FONT_B, background="#f0f4fa", foreground=SUB,
                    borderwidth=0)
    style.map("Treeview", background=[("selected", "#dbeafe")], foreground=[("selected", INK)])

    # ---------- 状态 ----------
    saved = load_settings()
    state = {
        "current": None,      # type: Optional[ParseResult]
        "history": [],        # type: List[ParseResult]
        "clip_hash": "",
        "watch": tk.BooleanVar(value=saved["watch"]),
        "auto_open": tk.BooleanVar(value=saved["auto_open"]),   # 识别成功后自动打开浏览器
        "opened": set(),                          # 已自动打开过的链接，避免重复弹窗
        "preview_img": None,
        "camera_on": False,
        "cap": None,
    }
    for _key in ("watch", "auto_open"):
        state[_key].trace_add("write", lambda *_: save_settings(state))
    # 启动即回写一次，保证 settings.json 始终与界面实际状态一致
    save_settings(state)

    # ---------- 顶部 ----------
    header = tk.Frame(root, bg=BG)
    header.pack(fill="x", padx=20, pady=(16, 6))
    tk.Label(header, text="网盘二维码 → 分享链接", font=FONT_T, bg=BG, fg=INK).pack(side="left")
    tk.Label(header, text="  纯本地离线解码 · 不上传 · 不联网", font=FONT_S, bg=BG, fg=SUB).pack(
        side="left", pady=(6, 0)
    )

    # ---------- 工具条 ----------
    bar = tk.Frame(root, bg=BG)
    bar.pack(fill="x", padx=20, pady=(2, 10))

    def mkbtn(parent, text, cmd, style_name="TButton", **kw):
        b = ttk.Button(parent, text=text, command=cmd, style=style_name, **kw)
        b.pack(side="left", padx=(0, 8))
        return b

    mkbtn(bar, "选择图片", lambda: on_pick_files(), "Accent.TButton")
    mkbtn(bar, "粘贴剪贴板 (Ctrl+V)", lambda: on_paste())
    mkbtn(bar, "摄像头扫描", lambda: on_camera())
    mkbtn(bar, "清空", lambda: on_clear(), "Ghost.TButton")
    ttk.Checkbutton(bar, text="识别后自动打开浏览器", variable=state["auto_open"],
                    style="TCheckbutton").pack(side="right", padx=(14, 0))
    ttk.Checkbutton(bar, text="自动监听剪贴板", variable=state["watch"],
                    style="TCheckbutton").pack(side="right")

    # ---------- 主体 ----------
    body = tk.Frame(root, bg=BG)
    body.columnconfigure(0, weight=0, minsize=300)
    body.columnconfigure(1, weight=1)
    body.rowconfigure(0, weight=1)

    # 左：预览卡片
    left = tk.Frame(body, bg=CARD, highlightbackground=LINE, highlightthickness=1)
    left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
    tk.Label(left, text="图片预览", font=FONT_B, bg=CARD, fg=SUB).pack(anchor="w", padx=14, pady=(12, 6))
    preview = tk.Canvas(left, bg="#f7f9fd", highlightthickness=0, width=280, height=280)
    preview.pack(fill="both", expand=True, padx=14, pady=(0, 8))
    drop_hint = preview.create_text(140, 140, text="把二维码图片拖到这里\n或 Ctrl+V 粘贴截图\n或点「选择图片」",
                                    fill="#9aa8bd", font=FONT_S, justify="center")
    tk.Label(left, text="支持 PNG / JPG / BMP / WEBP / GIF / TIFF，可一次多选批量解析",
             font=("Microsoft YaHei UI", 8), bg=CARD, fg="#a3b0c2", wraplength=270,
             justify="left").pack(anchor="w", padx=14, pady=(0, 12))

    # 右：结果卡片
    right = tk.Frame(body, bg=CARD, highlightbackground=LINE, highlightthickness=1)
    right.grid(row=0, column=1, sticky="nsew")

    tk.Label(right, text="识别结果", font=FONT_B, bg=CARD, fg=SUB).pack(anchor="w", padx=14, pady=(12, 4))

    info = tk.Frame(right, bg=CARD)
    info.pack(fill="x", padx=14, pady=(0, 6))
    lbl_netdisk = tk.Label(info, text="—", font=FONT_B, bg=CARD, fg=ACCENT)
    lbl_netdisk.pack(side="left")
    lbl_pwd = tk.Label(info, text="", font=FONT_B, bg=CARD, fg=WARN)
    lbl_pwd.pack(side="left", padx=(12, 0))
    lbl_src = tk.Label(info, text="", font=FONT_S, bg=CARD, fg=SUB)
    lbl_src.pack(side="right")

    tk.Label(right, text="分享链接", font=FONT_S, bg=CARD, fg=SUB).pack(anchor="w", padx=14)
    url_var = tk.StringVar()
    url_entry = tk.Entry(right, textvariable=url_var, font=FONT_MONO, bg="#f7f9fd", fg=INK,
                         relief="flat", insertbackground=INK)
    url_entry.pack(fill="x", padx=14, ipady=7, pady=(3, 8))

    tk.Label(right, text="二维码原始内容", font=FONT_S, bg=CARD, fg=SUB).pack(anchor="w", padx=14)
    raw_text = tk.Text(right, height=5, font=FONT_MONO, bg="#f7f9fd", fg="#465468",
                       relief="flat", wrap="word", padx=8, pady=6)
    raw_text.pack(fill="x", padx=14, pady=(3, 8))

    acts = tk.Frame(right, bg=CARD)
    acts.pack(fill="x", padx=14, pady=(0, 10))
    mkbtn(acts, "复制链接", lambda: on_copy_url(), "Accent.TButton")
    mkbtn(acts, "复制「链接+提取码」", lambda: on_copy_full())
    mkbtn(acts, "浏览器打开", lambda: on_open_url())
    mkbtn(acts, "复制原始内容", lambda: on_copy_raw(), "Ghost.TButton")

    # ---------- 历史 ----------
    hist_wrap = tk.Frame(root, bg=CARD, highlightbackground=LINE, highlightthickness=1)
    hist_wrap.pack(side="bottom", fill="both", padx=20, pady=(0, 8))
    head2 = tk.Frame(hist_wrap, bg=CARD)
    head2.pack(fill="x", padx=14, pady=(10, 4))
    tk.Label(head2, text="本次解析记录", font=FONT_B, bg=CARD, fg=SUB).pack(side="left")
    mkbtn(head2, "导出记录", lambda: on_export(), "Ghost.TButton")
    hist_btn = ttk.Button(head2, text="删除选中", command=lambda: on_del_hist(), style="Ghost.TButton")
    hist_btn.pack(side="right", padx=(0, 8))

    cols = ("netdisk", "url", "pwd", "source")
    tree = ttk.Treeview(hist_wrap, columns=cols, show="headings", height=6)
    for c, t, w in (("netdisk", "网盘", 110), ("url", "链接", 520), ("pwd", "提取码", 80),
                    ("source", "来源", 150)):
        tree.heading(c, text=t)
        tree.column(c, width=w, anchor="w", stretch=(c == "url"))
    tree.pack(fill="both", expand=True, padx=14, pady=(0, 12))
    tree.bind("<<TreeviewSelect>>", lambda e: on_hist_select())

    # ---------- 状态栏 ----------
    status = tk.Label(root, text="就绪 · 等待二维码", font=FONT_S, bg=BG, fg=SUB, anchor="w")
    status.pack(side="bottom", fill="x", padx=22, pady=(0, 10))
    # 主体最后 pack：先让底部固定区占位，中间区域再吃掉剩余空间
    body.pack(fill="both", expand=True, padx=20, pady=(0, 10))

    def say(msg, color=SUB):
        status.configure(text=msg, fg=color)

    # =====================================================================
    # 逻辑函数
    # =====================================================================
    def show_preview(img: "Image.Image"):
        try:
            w = max(preview.winfo_width(), 260)
            h = max(preview.winfo_height(), 260)
            im = img.copy()
            im.thumbnail((w - 10, h - 10))
            state["preview_img"] = ImageTk.PhotoImage(im)
            preview.delete("all")
            preview.create_image(w // 2, h // 2, image=state["preview_img"])
        except Exception:
            pass

    def reset_preview_hint():
        preview.delete("all")
        preview.create_text(140, 140, text="把二维码图片拖到这里\n或 Ctrl+V 粘贴截图\n或点「选择图片」",
                            fill="#9aa8bd", font=FONT_S, justify="center")

    def render(res: ParseResult):
        state["current"] = res
        url_var.set(res.url or "")
        raw_text.delete("1.0", "end")
        raw_text.insert("1.0", res.raw)
        lbl_netdisk.configure(text=res.netdisk if res.is_netdisk else "未识别到网盘")
        lbl_pwd.configure(text=f"提取码 {res.pwd}" if res.pwd else "")
        lbl_src.configure(text=res.source or "")
        if res.url:
            say(f"解码成功 · 引擎 {res.engine or '未知'} · 链接已就绪", OK)
        else:
            say("已解出内容，但其中没有可用的链接（可能是纯文本二维码）", WARN)

    def add_history(res_list: List[ParseResult]):
        for r in res_list:
            state["history"].insert(0, r)
            tree.insert("", 0, values=(r.netdisk, r.url or r.raw[:80], r.pwd, r.source))
        if res_list:
            render(res_list[0])
        # 识别成功即自动打开浏览器（每次只开第一个新链接，已开过的不重复弹）
        if state["auto_open"].get():
            for r in res_list:
                if r.url.lower().startswith(("http://", "https://")) and r.url not in state["opened"]:
                    state["opened"].add(r.url)
                    try:
                        webbrowser.open(r.url)
                        return r
                    except Exception:
                        pass
                    break
        return None

    def handle_image(img: "Image.Image", source: str, deep=True):
        show_preview(img)
        say("正在解码…")
        root.update_idletasks()
        t0 = time.time()
        texts, engine = decode_image(img, deep=deep)
        if not texts:
            say("未识别到二维码，试试更清晰的图片、放大二维码区域，或确认图片里确实有码", WARN)
            messagebox.showwarning(
                "没扫到码",
                "这张图里没有识别到二维码。\n\n建议：\n"
                "1) 截图时把二维码截完整，留一点白边\n"
                "2) 图片别太模糊，尽量原图\n"
                "3) 如果是手机屏幕截图，把二维码区域放大后重新截图",
            )
            return
        res_list = [parse_content(t, source=source, engine=engine) for t in texts]
        opened = add_history(res_list)
        cost = int((time.time() - t0) * 1000)
        if opened:
            say(f"识别成功 · 已在浏览器打开（{opened.netdisk}）· {cost} ms", OK)
        elif len(res_list) > 1:
            say(f"一次识别到 {len(res_list)} 个二维码 · 耗时 {cost} ms", OK)
        else:
            say(f"识别成功 · 耗时 {cost} ms · 链接已在下方", OK)

    def load_paths(paths):
        ok = 0
        for p in paths:
            try:
                img = Image.open(p)
                img.load()
                before = len(state["history"])
                handle_image(img, os.path.basename(p))
                if len(state["history"]) > before:
                    ok += 1
            except Exception:
                say(f"读取失败：{os.path.basename(p)}", WARN)
        if len(paths) > 1:
            say(f"批量完成：{ok}/{len(paths)} 张图片解析成功", OK if ok else WARN)
        return ok

    def on_pick_files():
        paths = filedialog.askopenfilenames(
            title="选择含二维码的图片（可多选）",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.webp *.gif *.tif *.tiff"),
                       ("全部文件", "*.*")],
        )
        if paths:
            load_paths(paths)

    def on_paste(event=None):
        try:
            data = ImageGrab.grabclipboard()
        except Exception:
            data = None
        if data is None:
            say("剪贴板里没有图片，先截图（Win+Shift+S）再来粘贴", WARN)
            return
        if isinstance(data, list):
            for p in data[:5]:
                if os.path.isfile(p):
                    try:
                        im = Image.open(p)
                        im.load()
                        handle_image(im, os.path.basename(p))
                    except Exception:
                        pass
            return
        handle_image(data, "剪贴板截图")

    def on_clear():
        state["current"] = None
        url_var.set("")
        raw_text.delete("1.0", "end")
        lbl_netdisk.configure(text="—")
        lbl_pwd.configure(text="")
        lbl_src.configure(text="")
        reset_preview_hint()
        say("已清空当前结果")

    def on_copy_url():
        u = url_var.get().strip()
        if not u:
            say("还没有可复制的链接", WARN)
            return
        root.clipboard_clear()
        root.clipboard_append(u)
        say("链接已复制到剪贴板", OK)

    def on_copy_full():
        cur = state["current"]
        if not cur:
            say("还没有结果", WARN)
            return
        root.clipboard_clear()
        root.clipboard_append(cur.copy_text())
        say("已复制：链接 + 提取码", OK)

    def on_copy_raw():
        cur = state["current"]
        if not cur:
            return
        root.clipboard_clear()
        root.clipboard_append(cur.raw)
        say("已复制二维码原始内容", OK)

    def on_open_url():
        u = url_var.get().strip()
        if not u.startswith("http"):
            say("没有可打开的链接", WARN)
            return
        import webbrowser

        webbrowser.open(u)
        state["opened"].add(u)
        say("已在默认浏览器打开（如提示提取码，见上方显示）", OK)

    def on_hist_select(_e=None):
        sel = tree.selection()
        if not sel:
            return
        idx = tree.index(sel[0])
        if 0 <= idx < len(state["history"]):
            render(state["history"][idx])

    def on_del_hist():
        for item in tree.selection():
            idx = tree.index(item)
            tree.delete(item)
            if 0 <= idx < len(state["history"]):
                state["history"].pop(idx)
        say("已删除选中记录")

    def on_export():
        if not state["history"]:
            say("还没有可导出的记录", WARN)
            return
        path = filedialog.asksaveasfilename(
            title="导出解析记录", defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("CSV 表格", "*.csv")],
            initialfile="网盘链接_解析记录.txt",
        )
        if not path:
            return
        try:
            if path.lower().endswith(".csv"):
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    f.write("网盘,链接,提取码,来源\n")
                    for r in state["history"]:
                        f.write(f'"{r.netdisk}","{r.url}","{r.pwd}","{r.source}"\n')
            else:
                with open(path, "w", encoding="utf-8") as f:
                    for r in state["history"]:
                        f.write(f"[{r.netdisk}] {r.copy_text()}\n")
            say(f"已导出到 {path}", OK)
        except Exception as e:
            say(f"导出失败：{e}", WARN)

    # ---------- 摄像头 ----------
    def on_camera():
        if not _HAS_CV2:
            messagebox.showerror("缺少组件", "摄像头扫描需要 opencv：\n\npip install opencv-python")
            return
        if state["camera_on"]:
            close_camera()
            return

        win = tk.Toplevel(root)
        win.title("摄像头扫描（Esc 关闭）")
        win.configure(bg=CARD)
        win.geometry("660x540")
        cam_canvas = tk.Canvas(win, bg="#000", highlightthickness=0)
        cam_canvas.pack(fill="both", expand=True, padx=10, pady=10)
        tip = tk.Label(win, text="把手机上的网盘二维码对准摄像头，识别到会自动停止", font=FONT_S,
                       bg=CARD, fg=SUB)
        tip.pack(pady=(0, 8))

        cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        if not cap or not cap.isOpened():
            state["camera_on"] = False
            win.destroy()
            messagebox.showerror("打不开摄像头", "没有检测到可用摄像头，或被其它程序占用。")
            return
        state["cap"] = cap
        state["camera_on"] = True
        say("摄像头已开启，对准二维码…")
        photo_holder = {"img": None}
        tick = {"n": 0}

        def loop():
            if not state["camera_on"]:
                return
            ok, frame = cap.read()
            if ok:
                tick["n"] += 1
                # 每 3 帧尝试解一次，降低 CPU 占用
                if tick["n"] % 3 == 0:
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    pil = Image.fromarray(rgb)
                    texts, engine = decode_image(pil, deep=False)
                    if texts:
                        res_list = [parse_content(t, source="摄像头", engine=engine) for t in texts]
                        opened = add_history(res_list)
                        close_camera()
                        win.destroy()
                        say("摄像头扫描成功，已在浏览器打开" if opened else "摄像头扫描成功", OK)
                        return
                disp = cv2.resize(frame, (640, 480))
                rgb = cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)
                photo_holder["img"] = ImageTk.PhotoImage(Image.fromarray(rgb))
                cam_canvas.delete("all")
                cam_canvas.create_image(320, 260, image=photo_holder["img"])
            win.after(30, loop)

        def close_camera():
            state["camera_on"] = False
            if state["cap"] is not None:
                try:
                    state["cap"].release()
                except Exception:
                    pass
                state["cap"] = None

        win.bind("<Escape>", lambda e: (close_camera(), win.destroy()))
        win.protocol("WM_DELETE_WINDOW", lambda: (close_camera(), win.destroy()))
        loop()

    # ---------- 剪贴板监听 ----------
    def watch_clip():
        if state["watch"].get():
            try:
                data = ImageGrab.grabclipboard()
                if isinstance(data, Image.Image):
                    buf = data.convert("L").resize((64, 64))
                    h = hashlib.md5(buf.tobytes()).hexdigest()
                    if h != state["clip_hash"]:
                        state["clip_hash"] = h
                        handle_image(data, "剪贴板截图")
            except Exception:
                pass
        root.after(1200, watch_clip)

    # ---------- 拖拽 ----------
    if has_dnd:
        def on_drop(event):
            paths = root.tk.splitlist(event.data)
            ok = 0
            for p in paths:
                if os.path.isfile(p) and p.lower().rsplit(".", 1)[-1] in (
                        "png", "jpg", "jpeg", "bmp", "webp", "gif", "tif", "tiff"):
                    try:
                        im = Image.open(p)
                        im.load()
                        handle_image(im, os.path.basename(p))
                        ok += 1
                    except Exception:
                        pass
            if ok:
                say(f"拖入 {ok} 张图片，解析完成", OK)

        for w in (root, preview, left, body):
            try:
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", on_drop)
            except Exception:
                pass

    # ---------- 快捷键 ----------
    root.bind("<Control-v>", on_paste)
    root.bind("<Control-V>", on_paste)
    root.bind("<Control-o>", lambda e: on_pick_files())
    root.bind("<Control-c>", lambda e: on_copy_url())
    root.bind("<Escape>", lambda e: on_clear())

    root.after(600, watch_clip)

    # 命令行传图：双击 / 右键"用此程序打开"也可直接解析
    _init = [a for a in sys.argv[1:] if os.path.isfile(a)]
    if _init:
        root.after(350, lambda: load_paths(_init))

    root.mainloop()


if __name__ == "__main__":
    if _MISSING:
        print("缺少依赖，请先执行： pip install pillow numpy opencv-python pyzbar")
        input("按回车退出…")
        sys.exit(1)
    try:
        run_gui()
    except Exception:
        traceback.print_exc()
        input("程序异常，按回车退出…")
