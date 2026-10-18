#!/usr/bin/env pythonw
"""手机看视频 —— 把电脑上指定的视频，共享给同一个 WiFi 下的手机播放。

双击运行（无控制台窗口）。选择文件夹或单个视频，设好密码，点“启动服务”，
手机浏览器打开窗口里显示的链接即可。
"""

import hashlib
import html
import glob
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import uuid
from base64 import b64decode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tkinter import filedialog, messagebox, ttk
from urllib.parse import parse_qs, quote, urlparse

APP_TITLE = "手机看视频"
if getattr(sys, "frozen", False):
    # PyInstaller 打包后，资源在临时解包目录里
    HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    EXE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
    EXE_DIR = HERE
ICON_PATH = os.path.join(HERE, "icon.ico")
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "PhoneVideo")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_PORT = 8000

VIDEO_EXT = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".flv",
    ".webm", ".m4v", ".rmvb", ".rm", ".mpg", ".mpeg", ".3gp",
}
IMAGE_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
    ".tif", ".tiff", ".avif", ".jfif",
}
PDF_EXT = {".pdf"}
BROWSER_OK = {".mp4", ".webm", ".m4v", ".mov"}
MIME = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/mp4",
    ".webm": "video/webm", ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo", ".ts": "video/mp2t", ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv", ".rmvb": "application/vnd.rn-realmedia-vbr",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".jfif": "image/jpeg",
    ".png": "image/png", ".gif": "image/gif", ".bmp": "image/bmp",
    ".webp": "image/webp", ".avif": "image/avif",
    ".tif": "image/tiff", ".tiff": "image/tiff",
}

_fails = {}
_fails_lock = threading.Lock()

try:
    from PIL import Image as _PILImage
    HAS_PIL = True
except Exception:
    HAS_PIL = False

_thumb_cache = {}
_thumb_lock = threading.Lock()

_ff = {"checked": False, "path": None, "encoder": None}
_ff_lock = threading.Lock()
_probe_cache = {}
_jobs = {}
_jobs_lock = threading.Lock()

DEFAULT_CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                                 "PhoneVideo", "converted")
CACHE_DIR = DEFAULT_CACHE_DIR
CACHE_DAYS = 7
CACHE_MAX_GB = 40
_cache_ready = {"ok": False}


def ensure_cache_dir():
    """缓存目录优先放 %LOCALAPPDATA%，不可写时退到 %TEMP%。"""
    global CACHE_DIR
    if _cache_ready["ok"]:
        return CACHE_DIR
    candidates = [CACHE_DIR,
                  os.path.join(os.environ.get("TEMP", ""), "PhoneVideo", "converted")]
    for d in candidates:
        if not d:
            continue
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".probe")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("1")
            os.remove(probe)
        except OSError:
            continue
        CACHE_DIR = d
        _cache_ready["ok"] = True
        return d
    return None


def set_cache_dir(path):
    global CACHE_DIR
    CACHE_DIR = path or DEFAULT_CACHE_DIR
    _cache_ready["ok"] = False
    return ensure_cache_dir()


def _winget_ffmpeg(name):
    """winget 装的 ffmpeg 不一定在 PATH 里，直接去包目录找。"""
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages")
    try:
        for entry in os.listdir(base):
            if "ffmpeg" not in entry.lower():
                continue
            for root, _dirs, files in os.walk(os.path.join(base, entry)):
                if name in files:
                    return os.path.join(root, name)
    except OSError:
        pass
    return None


def find_tool(name):
    for cand in (os.path.join(EXE_DIR, name), os.path.join(HERE, name),
                 shutil.which(name), _winget_ffmpeg(name)):
        if cand and os.path.isfile(cand):
            return cand
    return None


def ffmpeg_info():
    """找 ffmpeg，并探测有没有 NVIDIA 硬件编码器。结果只算一次。"""
    with _ff_lock:
        if _ff["checked"]:
            return _ff["path"], _ff["encoder"]
        _ff["checked"] = True
        path = find_tool("ffmpeg.exe")
        if not path:
            return None, None
        enc = "libx264"
        try:
            r = subprocess.run(
                [path, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                 "-i", "color=c=black:s=320x240:d=0.2", "-c:v", "h264_nvenc",
                 "-f", "null", "-"],
                capture_output=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0:
                enc = "h264_nvenc"
        except Exception:
            pass
        _ff["path"], _ff["encoder"] = path, enc
        return path, enc


def probe_media(full):
    """返回 {vcodec, acodec, height, duration}，结果缓存。"""
    key = (full, os.path.getmtime(full))
    with _ff_lock:
        hit = _probe_cache.get(key)
    if hit:
        return hit
    info = {"vcodec": "", "acodec": "", "height": 0, "duration": 0.0}
    ffprobe = find_tool("ffprobe.exe")
    if not ffprobe:
        return info
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,height:format=duration",
             "-of", "json", full],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = json.loads(r.stdout or "{}")
        for st in data.get("streams", []):
            kind = st.get("codec_type")
            if kind == "video" and not info["vcodec"]:
                info["vcodec"] = st.get("codec_name", "") or ""
                try:
                    info["height"] = int(st.get("height") or 0)
                except (TypeError, ValueError):
                    pass
            elif kind == "audio" and not info["acodec"]:
                info["acodec"] = st.get("codec_name", "") or ""
        try:
            info["duration"] = float((data.get("format") or {}).get("duration") or 0)
        except (TypeError, ValueError):
            pass
    except Exception:
        pass
    with _ff_lock:
        _probe_cache[key] = info
    return info


def cache_path(full, maxh=0):
    key = hashlib.sha1(
        f"{full}|{os.path.getmtime(full)}|{os.path.getsize(full)}|{maxh}".encode("utf-8")
    ).hexdigest()[:20]
    return os.path.join(CACHE_DIR, key + ".mp4")


def cleanup_cache():
    ensure_cache_dir()
    try:
        now = time.time()
        files = []
        for name in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            # 转码中断留下的临时文件，超过一天就清掉
            if name.endswith((".part", ".log")):
                if now - st.st_mtime > 86400:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                continue
            if now - st.st_mtime > CACHE_DAYS * 86400:
                os.remove(p)
            else:
                files.append((st.st_mtime, st.st_size, p))
        total = sum(f[1] for f in files)
        for _mtime, size, p in sorted(files):
            if total <= CACHE_MAX_GB * 1024 ** 3:
                break
            try:
                os.remove(p)
                total -= size
            except OSError:
                pass
    except OSError:
        pass


def cache_size():
    total = 0
    try:
        for name in os.listdir(CACHE_DIR):
            if name.endswith(".mp4"):
                try:
                    total += os.path.getsize(os.path.join(CACHE_DIR, name))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def components_text():
    def mark(ok):
        return "✓" if ok else "—"
    return ("照片缩略图 " + mark(HAS_PIL) +
            "   视频转码 " + mark(bool(find_tool("ffmpeg.exe"))) +
            "   PDF " + mark(bool(find_poppler("pdftoppm.exe"))))


_pdf_cache = {}
_pdf_lock = threading.Lock()
_pdf_pages_cache = {}


_poppler_cache = {}


def find_poppler(name):
    if name in _poppler_cache:
        return _poppler_cache[name]
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    direct = [os.path.join(EXE_DIR, name), os.path.join(HERE, name), shutil.which(name),
              os.path.join(home, ".cache", "codex-runtimes", "codex-primary-runtime",
                           "dependencies", "native", "poppler", "Library", "bin", name)]
    # 别人机器上可能用 winget / Scoop / 绿色版装的 poppler
    patterns = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     "Microsoft", "WinGet", "Packages", "*poppler*", "**", "bin", name),
        os.path.join(home, "scoop", "apps", "poppler", "**", "bin", name),
        "C:\\Program Files\\poppler*\\Library\\bin\\" + name,
        "C:\\Program Files\\poppler*\\bin\\" + name,
    ]
    for cand in direct:
        if cand and os.path.isfile(cand):
            _poppler_cache[name] = cand
            return cand
    for pat in patterns:
        try:
            for cand in glob.glob(pat, recursive=True):
                if os.path.isfile(cand):
                    _poppler_cache[name] = cand
                    return cand
        except Exception:
            pass
    _poppler_cache[name] = None
    return None


def pdf_page_count(full):
    key = (full, os.path.getmtime(full))
    with _pdf_lock:
        hit = _pdf_pages_cache.get(key)
    if hit is not None:
        return hit
    count = 0
    pdfinfo = find_poppler("pdfinfo.exe")
    if pdfinfo:
        try:
            r = subprocess.run([pdfinfo, full], capture_output=True, timeout=30,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            m = re.search(rb"Pages:\s+(\d+)", r.stdout)
            if m:
                count = int(m.group(1))
        except Exception:
            pass
    with _pdf_lock:
        _pdf_pages_cache[key] = count
    return count


def render_pdf_page(full, page, width=1400):
    """把 PDF 的某一页渲染成 JPEG，带内存缓存。"""
    tool = find_poppler("pdftoppm.exe")
    if not tool:
        return None
    key = (full, os.path.getmtime(full), page, width)
    with _pdf_lock:
        hit = _pdf_cache.get(key)
    if hit:
        return hit
    tmpdir = os.path.join(tempfile.gettempdir(), "PhoneVideo_pdf", uuid.uuid4().hex[:10])
    try:
        os.makedirs(tmpdir, exist_ok=True)
    except OSError:
        return None
    base = os.path.join(tmpdir, "page")
    cmd = [tool, "-jpeg", "-jpegopt", "quality=82", "-scale-to", str(width),
           "-f", str(page), "-l", str(page), "-singlefile", full, base]
    data = None
    try:
        subprocess.run(cmd, capture_output=True, timeout=90,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        path = base + ".jpg"
        if os.path.isfile(path):
            with open(path, "rb") as f:
                data = f.read()
    except Exception:
        data = None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if data:
        with _pdf_lock:
            if len(_pdf_cache) > 24:
                _pdf_cache.clear()
            _pdf_cache[key] = data
    return data


def convert_status(cache_file):
    with _jobs_lock:
        return _jobs.get(cache_file)


def start_convert(full, maxh=0):
    if not ensure_cache_dir():
        return {"state": "error", "pct": 0.0, "speed": "", "eta": "",
                "err": "缓存目录不可写：%LOCALAPPDATA% 和 %TEMP% 都创建失败。",
                "label": ""}
    out = cache_path(full, maxh)
    with _jobs_lock:
        job = _jobs.get(out)
        if job and job["state"] == "running":
            return job
        job = {"state": "running", "pct": 0.0, "speed": "", "eta": "",
               "err": "", "label": ""}
        _jobs[out] = job
    threading.Thread(target=_convert_worker, args=(full, out, maxh, job),
                     daemon=True).start()
    return job


def _convert_worker(full, out, maxh, job):
    ff, enc = ffmpeg_info()
    info = probe_media(full)
    dur = info["duration"]
    if info["vcodec"] == "h264":
        vargs = ["-c:v", "copy"]
        aargs = (["-c:a", "copy"] if info["acodec"] in ("aac", "mp3")
                 else ["-c:a", "aac", "-b:a", "192k", "-ac", "2"])
        job["label"] = "只换容器，画质无损"
    else:
        if enc == "h264_nvenc":
            vargs = ["-c:v", "h264_nvenc", "-preset", "medium",
                     "-rc", "vbr", "-cq", "23", "-b:v", "0"]
        else:
            vargs = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
        vargs += ["-pix_fmt", "yuv420p"]
        if maxh:
            vargs += ["-vf", f"scale=-2:{maxh}"]
        aargs = ["-c:a", "aac", "-b:a", "160k", "-ac", "2"]
        job["label"] = "重新编码"

    if not ensure_cache_dir():
        job["state"] = "error"
        job["err"] = "缓存目录不可写：%LOCALAPPDATA% 和 %TEMP% 都创建失败。"
        return
    tmp = out + ".part"
    errpath = out + ".log"
    cmd = ([ff, "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-progress", "pipe:1", "-i", full,
            "-map", "0:v:0", "-map", "0:a:0?"] + vargs + aargs +
           ["-movflags", "+faststart", "-f", "mp4", tmp])
    last_us = 0
    try:
        with open(errpath, "w", encoding="utf-8", errors="replace") as errf:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errf,
                text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_us="):
                    try:
                        last_us = int(line.split("=", 1)[1])
                    except ValueError:
                        continue
                    if dur > 0:
                        job["pct"] = max(0.0, min(99.0, last_us / 1e6 / dur * 100))
                elif line.startswith("speed="):
                    sp = line.split("=", 1)[1]
                    job["speed"] = sp
                    try:
                        mult = float(sp.rstrip("x"))
                        if mult > 0 and dur > 0:
                            left = max(0.0, dur - last_us / 1e6) / mult
                            job["eta"] = (f"{int(left // 60)} 分 {int(left % 60)} 秒"
                                          if left >= 60 else f"{int(left)} 秒")
                    except ValueError:
                        pass
            proc.wait()
        if proc.returncode == 0 and os.path.exists(tmp):
            os.replace(tmp, out)
            job["state"] = "done"
            job["pct"] = 100.0
        else:
            job["state"] = "error"
            try:
                with open(errpath, encoding="utf-8", errors="replace") as f:
                    job["err"] = f.read()[-400:] or "ffmpeg 返回了错误但没有输出"
            except OSError:
                job["err"] = "转换失败"
    except Exception as exc:
        job["state"] = "error"
        job["err"] = str(exc)


def render_convert(full, p, maxh):
    out = cache_path(full, maxh)
    job = convert_status(out)
    if job is None:
        job = start_convert(full, maxh)
    info = probe_media(full)
    back = "/play?p=" + quote(p)

    if job["state"] == "done":
        return page("准备完成", (
            '<meta http-equiv="refresh" content="0;url=/converted?p=' + quote(p) +
            ('&h=%d' % maxh if maxh else '') + '">'
            '<div class="empty">准备完成，正在打开…</div>'))

    if job["state"] == "error":
        body = (
            '<header><h1>转换失败</h1><div class="crumb">'
            f'<a href="{back}">返回</a></div></header>'
            '<div style="padding:16px">'
            '<div style="color:#ff8f8f;font-size:14px;white-space:pre-wrap;'
            'background:#1a1214;border:1px solid #43242a;border-radius:12px;padding:14px">'
            f'{html.escape(job["err"])}</div>'
            f'<form method="get" action="/convert" style="margin-top:14px">'
            f'<input type="hidden" name="p" value="{html.escape(p)}">'
            '<button style="background:#2f6df6;border:0;border-radius:10px;color:#fff;'
            'padding:12px 18px;font-size:15px">重试</button></form></div>'
        )
        return page("转换失败", body)

    pct = job["pct"]
    note = ("这个文件是 H.264，只需要换容器，几秒钟就好。"
            if info["vcodec"] == "h264" else
            "需要重新编码，时间取决于片长和显卡。")
    body = (
        '<meta http-equiv="refresh" content="2">'
        '<header><h1>电脑正在准备</h1>'
        f'<div class="crumb"><a href="{back}">返回</a></div></header>'
        '<div style="padding:18px 16px">'
        '<div style="height:10px;background:#1a1f29;border-radius:6px;overflow:hidden">'
        f'<div style="height:100%;width:{pct:.1f}%;background:#2f6df6;transition:width .4s"></div>'
        "</div>"
        f'<div style="margin-top:12px;font-size:15px">{pct:.0f}%'
        f'{" · " + job["speed"] if job["speed"] else ""}'
        f'{" · 还剩约 " + job["eta"] if job["eta"] else ""}</div>'
        f'<div style="color:#8b93a1;font-size:13px;margin-top:8px">{html.escape(note)}'
        f'{" · " + job["label"] if job["label"] else ""}</div>'
        '<div style="color:#5f6673;font-size:12px;margin-top:18px;line-height:1.8">'
        "转好的文件存在电脑上，手机不占空间。完成后可以像本地文件一样随意拖动进度条。<br>"
        "这页每 2 秒自动刷新。</div></div>"
    )
    return page("准备中", body)


def render_converted(p, name, maxh):
    src = "/converted/stream?p=" + quote(p) + (f"&h={maxh}" if maxh else "")
    return page(name, (
        '<div style="padding:10px 10px 8px">'
        f'<video controls autoplay playsinline preload="metadata" '
        f'style="width:100%;max-height:70vh;background:#000" src="{src}"></video></div>'
        '<div style="padding:0 16px 30px;color:#8b93a1;font-size:12.5px;line-height:1.8">'
        "这是电脑转好的版本，缓存在电脑上，可以随意拖动。"
        f'<br><a href="/play?p={quote(p)}" style="color:#7aa2ff">返回其他播放方式</a></div>'
    ))


# ---------------------------------------------------------------- 配置

def load_config():
    cfg = {"roots": [], "port": DEFAULT_PORT, "cache_dir": "", "password": "".join(
        secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(6))}
    data = None
    for path in (CONFIG_PATH, os.path.join(EXE_DIR, "config.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            break
        except (OSError, ValueError):
            continue
    try:
        if not isinstance(data, dict):
            return cfg
        if isinstance(data.get("roots"), list):
            cfg["roots"] = [str(p) for p in data["roots"]]
        if isinstance(data.get("port"), int):
            cfg["port"] = data["port"]
        if isinstance(data.get("password"), str):
            cfg["password"] = data["password"]
        if isinstance(data.get("cache_dir"), str):
            cfg["cache_dir"] = data["cache_dir"]
    except Exception:
        pass
    return cfg


def save_config(cfg):
    """优先写 %APPDATA%，写不进去就退到程序目录（便携模式）。"""
    for path in (CONFIG_PATH, os.path.join(EXE_DIR, "config.json")):
        try:
            folder = os.path.dirname(path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            return path
        except OSError:
            continue
    return None


def lan_ips():
    """列出本机可能被手机访问到的 IPv4 地址，常用的排前面。"""
    ips = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.1.1", 80))
        ips.append(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass

    def rank(ip):
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        if ip.startswith("172."):
            return 2
        return 3

    out = []
    for ip in ips:
        if ip and not ip.startswith("127.") and ip not in out:
            out.append(ip)
    out.sort(key=rank)
    return out or ["127.0.0.1"]


def lan_ip():
    return lan_ips()[0]


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


# ---------------------------------------------------------------- 页面

PAGE_HEAD = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0e1014">
<title>{title}</title><style>
:root{{color-scheme:dark}}
*{{box-sizing:border-box;-webkit-tap-highlight-color:transparent}}
body{{margin:0;background:#0e1014;color:#e9ecf1;
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
header{{position:sticky;top:0;background:rgba(14,16,20,.93);backdrop-filter:blur(12px);
padding:14px 16px;border-bottom:1px solid #22262e;z-index:5}}
h1{{font-size:17px;margin:0;font-weight:600}}
.crumb{{font-size:13px;color:#8b93a1;margin-top:6px;word-break:break-all}}
a{{color:inherit;text-decoration:none}}
.list{{padding:8px 10px 30px}}
.row{{display:flex;align-items:center;gap:12px;padding:13px 12px;border-radius:12px;
border-bottom:1px solid #1a1e25}}
.row:active{{background:#1b1f27}}
.ico{{width:34px;height:34px;flex:0 0 34px;border-radius:9px;background:#232833;
display:flex;align-items:center;justify-content:center;font-size:15px}}
.nm{{flex:1;min-width:0;font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.meta{{font-size:12px;color:#79808d;margin-top:2px;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}}
.badge{{font-size:11px;padding:2px 7px;border-radius:20px;background:#2a3a2f;color:#7bd88f;white-space:nowrap}}
.badge.warn{{background:#3a3122;color:#e0b070}}
.search{{display:flex;gap:8px;margin-top:10px}}
.search input{{flex:1;background:#191d24;border:1px solid #2a2f3a;border-radius:10px;
padding:11px 13px;color:#e9ecf1;font-size:15px;outline:none}}
.search button{{background:#2f6df6;border:0;border-radius:10px;color:#fff;padding:0 16px;font-size:15px}}
.empty{{padding:40px 20px;text-align:center;color:#79808d}}
footer{{padding:20px 16px 36px;color:#5f6673;font-size:12px;text-align:center;line-height:1.9}}
.sect{{margin:24px 8px 10px;font-size:12.5px;color:#8b93a1;letter-spacing:.5px}}
.gal{{display:grid;grid-template-columns:repeat(auto-fill,minmax(102px,1fr));gap:6px;padding:0 4px}}
.cell{{position:relative;aspect-ratio:1;border-radius:9px;overflow:hidden;background:#1a1f29;display:block}}
.cell img{{width:100%;height:100%;object-fit:cover;display:block}}
.cell:active{{opacity:.75}}
.cell span{{position:absolute;left:0;right:0;bottom:0;padding:14px 6px 4px;font-size:10px;
color:#dfe5ef;background:linear-gradient(transparent,rgba(0,0,0,.72));overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}}
</style></head><body>"""


def page(title, body):
    return (PAGE_HEAD.format(title=html.escape(title)) + body + "</body></html>").encode("utf-8")


def badge_for(ext):
    if ext in IMAGE_EXT:
        return ""
    return ('<span class="badge">可直接播</span>' if ext in BROWSER_OK
            else '<span class="badge warn">需播放器</span>')


def root_label(path):
    name = os.path.basename(path.rstrip("\\/")) or path
    return name


def render_root(roots):
    rows = []
    for i, path in enumerate(roots):
        if os.path.isdir(path):
            rows.append(
                f'<a class="row" href="/browse?p={i}">'
                f'<div class="ico">📁</div><div class="nm">{html.escape(root_label(path))}'
                f'<div class="meta">{html.escape(path)}</div></div></a>'
            )
        elif os.path.isfile(path):
            ext = os.path.splitext(path)[1].lower()
            is_pic = ext in IMAGE_EXT
            is_pdf = ext in PDF_EXT
            kind = "photo" if is_pic else ("pdf" if is_pdf else "play")
            icon = "🖼" if is_pic else ("📖" if is_pdf else "▶")
            rows.append(
                f'<a class="row" href="/{kind}?p={i}">'
                f'<div class="ico">{icon}</div><div class="nm">'
                f'{html.escape(os.path.splitext(os.path.basename(path))[0])}'
                f'<div class="meta">{ext[1:].upper()} · {human(os.path.getsize(path))}</div>'
                f'</div>{badge_for(ext)}</a>'
            )
    if not rows:
        rows.append('<div class="empty">还没有添加任何文件夹或视频。</div>')
    body = (
        f'<header><h1>我的视频</h1><div class="crumb">共 {len(roots)} 个位置</div>'
        '<form class="search" action="/search" method="get">'
        '<input name="q" placeholder="搜索文件名…" autocomplete="off">'
        "<button>搜</button></form></header>"
        f'<div class="list">{"".join(rows)}</div>'
        '<footer>电脑需保持开机 · 同一 WiFi 下使用<br><a href="/logout">退出登录</a></footer>'
    )
    return page("我的视频", body)


def render_dir(i, rel_dir, roots):
    base = roots[i]
    full = os.path.normpath(os.path.join(base, rel_dir.replace("/", os.sep))) if rel_dir else base
    if not os.path.isdir(full):
        return page("未找到", '<div class="empty">这个文件夹不存在。</div>')

    dirs, files, photos, docs = [], [], [], []
    try:
        with os.scandir(full) as it:
            for e in it:
                if e.name.startswith(".") or e.name in ("$RECYCLE.BIN", "System Volume Information"):
                    continue
                try:
                    if e.is_dir():
                        if e.name.startswith("$"):
                            continue
                        dirs.append(e.name)
                    else:
                        ext = os.path.splitext(e.name)[1].lower()
                        if ext in VIDEO_EXT:
                            files.append((e.name, e.stat().st_size))
                        elif ext in IMAGE_EXT:
                            photos.append((e.name, e.stat().st_size))
                        elif ext in PDF_EXT:
                            docs.append((e.name, e.stat().st_size))
                except OSError:
                    continue
    except OSError as exc:
        return page("打不开", f'<div class="empty">读取失败：{html.escape(str(exc))}</div>')

    dirs.sort(key=natural_key)
    files.sort(key=lambda x: natural_key(x[0]))
    photos.sort(key=lambda x: natural_key(x[0]))
    docs.sort(key=lambda x: natural_key(x[0]))

    root_url = f"/browse?p={i}"
    crumbs = [f'<a href="/">首页</a>', f'<a href="{root_url}">{html.escape(root_label(base))}</a>']
    acc = []
    for part in [p for p in rel_dir.split("/") if p]:
        acc.append(part)
        crumbs.append(f'<a href="/browse?p={i}/{quote("/".join(acc))}">{html.escape(part)}</a>')

    rows = []
    parent = "/".join([p for p in rel_dir.split("/") if p][:-1])
    if rel_dir:
        rows.append(
            f'<a class="row" href="/browse?p={i}/{quote(parent)}">'
            '<div class="ico">↩</div><div class="nm">返回上一级</div></a>'
        )
    for d in dirs:
        sub = f"{rel_dir}/{d}" if rel_dir else d
        rows.append(
            f'<a class="row" href="/browse?p={i}/{quote(sub)}">'
            f'<div class="ico">📁</div><div class="nm">{html.escape(d)}</div></a>'
        )
    for name, size in files:
        sub = f"{rel_dir}/{name}" if rel_dir else name
        ext = os.path.splitext(name)[1].lower()
        rows.append(
            f'<a class="row" href="/play?p={i}/{quote(sub)}">'
            f'<div class="ico">▶</div><div class="nm">{html.escape(os.path.splitext(name)[0])}'
            f'<div class="meta">{ext[1:].upper()} · {human(size)}</div></div>'
            f'{badge_for(ext)}</a>'
        )
    gal = []
    if docs:
        gal.append('<div class="list">')
        gal.append(f'<div class="sect">PDF 文档 · {len(docs)} 个</div>')
        for name, size in docs:
            sub = f"{rel_dir}/{name}" if rel_dir else name
            gal.append(
                f'<a class="row" href="/pdf?p={i}/{quote(sub)}">'
                f'<div class="ico">📖</div><div class="nm">'
                f'{html.escape(os.path.splitext(name)[0])}'
                f'<div class="meta">PDF · {human(size)}</div></div></a>'
            )
        gal.append("</div>")
    if photos:
        gal.append(f'<div class="sect">照片 · {len(photos)} 张</div><div class="gal">')
        for name, size in photos:
            sub = f"{rel_dir}/{name}" if rel_dir else name
            rel = f"{i}/{sub}"
            if HAS_PIL:
                thumb = f'<img loading="lazy" decoding="async" src="/thumb?p={quote(rel)}" alt="">'
            else:
                thumb = ""
            gal.append(
                f'<a class="cell" href="/photo?p={quote(rel)}">{thumb}'
                f'<span>{html.escape(name)}</span></a>'
            )
        gal.append("</div>")

    if not rows and not photos and not docs:
        rows.append('<div class="empty">这里没有视频、照片、PDF 或子文件夹。</div>')

    body = (
        f'<header><h1>{html.escape(os.path.basename(full) or root_label(base))}</h1>'
        f'<div class="crumb">{" / ".join(crumbs)}</div></header>'
        f'<div class="list">{"".join(rows)}</div>'
        f'{"".join(gal)}'
        '<footer>电脑需保持开机 · 同一 WiFi 下使用<br><a href="/logout">退出登录</a></footer>'
    )
    return page("浏览", body)


def direct_url(p, host, password):
    host = host or f"{lan_ip()}:{DEFAULT_PORT}"
    if password:
        return f"http://video:{quote(password)}@{host}/stream?p={quote(p)}"
    return f"http://{host}/stream?p={quote(p)}"


def m3u_bytes(p, host, password, name):
    return ("#EXTM3U\n#EXTINF:-1," + name + "\n"
            + direct_url(p, host, password) + "\n").encode("utf-8")


def render_play(full, p, host, password):
    name = os.path.basename(full)
    ext = os.path.splitext(name)[1].lower()
    size = human(os.path.getsize(full))
    stream = "/stream?p=" + quote(p)
    download = "/download?p=" + quote(p)
    back = "/browse?p=" + quote(p.rsplit("/", 1)[0]) if "/" in p else "/"
    ok = ext in BROWSER_OK

    direct = direct_url(p, host, password)

    if ok:
        player = (f'<video controls autoplay playsinline preload="metadata" '
                  f'style="width:100%;max-height:70vh;background:#000" src="{stream}"></video>')
        explain = ""
    else:
        player = (
            '<div style="padding:20px 18px;background:#151a22;border-radius:14px">'
            '<div style="font-size:17px;font-weight:600;margin-bottom:8px">这个格式浏览器解不了</div>'
            '<div style="color:#98a0ae;font-size:14px;line-height:1.75">'
            f"{html.escape(ext[1:].upper())} 容器或 HEVC 编码，手机浏览器通常只有声音、黑屏或直接报错。"
            "手机自己的硬件解码器其实是能放的，只是浏览器不负责这件事。两条路："
            "</div></div>"
        )
        ff_path, _ff_enc = ffmpeg_info()
        if ff_path:
            _info = probe_media(full)
            if _info["vcodec"] == "h264":
                lead = "这个文件本身是 H.264，电脑只需换个容器，几秒钟就好，画质完全不变。"
            else:
                lead = "电脑在后台重新编码一次（走显卡），转完就能像本地文件一样拖。"
            trans_card = (
                '<div style="background:#151a22;border:1px solid #232a38;border-radius:14px;'
                'padding:18px;margin-top:12px">'
                '<div style="font-weight:600;font-size:15px;margin-bottom:6px">'
                "③ 做成能在浏览器里拖动的版本</div>"
                '<div style="color:#98a0ae;font-size:13.5px;line-height:1.75">'
                + lead +
                " 转好的文件缓存在电脑上，手机不占空间，下次打开秒开。</div>"
                f'<a href="/convert?p={quote(p)}" style="display:block;text-align:center;'
                'margin-top:12px;background:#2f6df6;border-radius:10px;color:#fff;'
                'padding:13px;font-size:15px">开始转换</a></div>'
            )
        else:
            trans_card = (
                '<div style="background:#151a22;border:1px dashed #2f3a4d;border-radius:14px;'
                'padding:18px;margin-top:12px">'
                '<div style="font-weight:600;font-size:15px;margin-bottom:6px;color:#98a0ae">'
                "③ 在浏览器里直接看 · 需要 ffmpeg</div>"
                '<div style="color:#6f7889;font-size:13px;line-height:1.75">'
                "装上 ffmpeg 后这里会多一个按钮，由电脑实时转码。在 PowerShell 里执行 "
                '<code style="background:#0e131b;padding:2px 6px;border-radius:5px">'
                "winget install Gyan.FFmpeg</code>，然后重启本程序。</div></div>"
            )
        explain = (
            '<div style="padding:14px 2px 0">'
            '<div style="background:#151a22;border:1px solid #232a38;border-radius:14px;'
            'padding:18px;margin-bottom:12px">'
            '<div style="font-weight:600;font-size:15px;margin-bottom:6px">① 交给播放器串流 · 推荐</div>'
            '<div style="color:#98a0ae;font-size:13.5px;line-height:1.75">'
            '不占手机空间，边下边播，<b>拖进度条正常</b>，画质无损。手机装 <b>VLC</b>（免费），'
            '点下面的一键按钮，会下载一个播放列表，点它选 VLC 打开即可。</div>'
            f'<a href="/open.m3u?p={quote(p)}" style="display:block;text-align:center;'
            'margin-top:12px;background:#2f6df6;border-radius:10px;color:#fff;'
            'padding:13px;font-size:15px">用播放器打开（一键）</a>'
            f'<input id="d" readonly value="{html.escape(direct)}" onclick="this.select()" '
            'style="width:100%;margin-top:10px;background:#0e131b;border:1px solid #2a2f3a;'
            'border-radius:10px;padding:10px 12px;color:#8b95a8;font-size:11.5px">'
            '<button id="cp" onclick="copyDirect()" style="width:100%;margin-top:8px;'
            'background:#232833;border:0;border-radius:10px;color:#c9d1e0;padding:11px;font-size:13.5px">'
            "复制直链</button></div>"
            '<div style="background:#151a22;border:1px solid #232a38;border-radius:14px;padding:18px">'
            '<div style="font-weight:600;font-size:15px;margin-bottom:6px">② 下载到手机本地</div>'
            '<div style="color:#98a0ae;font-size:13.5px;line-height:1.75">'
            f'存进手机后由系统相册或播放器硬解，最稳。代价是占 {size} 空间，'
            "WiFi 传输要几分钟，看完还得自己删。</div>"
            f'<a href="{download}" style="display:block;text-align:center;margin-top:12px;'
            'background:#232833;border:1px solid #333c4d;border-radius:10px;color:#e9ecf1;'
            f'padding:13px;font-size:15px">下载到手机（{size}）</a></div>'
            + trans_card +
            "</div>"
            "<script>function copyDirect(){var i=document.getElementById('d');i.select();"
            "i.setSelectionRange(0,99999);var done=false;"
            "if(navigator.clipboard){navigator.clipboard.writeText(i.value);done=true;}"
            "else{try{done=document.execCommand('copy');}catch(e){}}"
            "var b=document.getElementById('cp');"
            "if(done){b.textContent='已复制，去 VLC 粘贴';b.style.background='#1f7a45';}}</script>"
        )

    body = (
        f'<header><h1>{html.escape(name)}</h1>'
        f'<div class="crumb">{html.escape(ext[1:].upper())} · {size} · '
        f'<a href="{back}">返回</a> · <a href="{download}">下载</a></div></header>'
        f'<div style="padding:12px 10px">{player}</div>'
        f'<div style="padding:0 10px 40px">{explain}</div>'
    )
    return page(name, body)


def make_thumb(full, size=200):
    key = (full, os.path.getmtime(full), os.path.getsize(full))
    with _thumb_lock:
        hit = _thumb_cache.get(key)
    if hit:
        return hit
    im = _PILImage.open(full)
    try:
        im.draft("RGB", (size * 2, size * 2))
    except Exception:
        pass
    im = im.convert("RGB")
    w, h = im.size
    side = min(w, h)
    im = im.crop(((w - side) // 2, (h - side) // 2,
                  (w + side) // 2, (h + side) // 2)).resize((size, size), _PILImage.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=72, optimize=True)
    data = buf.getvalue()
    with _thumb_lock:
        if len(_thumb_cache) > 400:
            _thumb_cache.clear()
        _thumb_cache[key] = data
    return data


PHOTO_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#000000"><title>%(title)s</title><style>
html,body{margin:0;height:100%%;background:#000;overflow:hidden}
img.pic{position:absolute;inset:0;width:100%%;height:100%%;object-fit:contain}
.top{position:fixed;top:0;left:0;right:0;display:flex;justify-content:space-between;gap:12px;
padding:14px 18px;background:linear-gradient(rgba(0,0,0,.72),transparent);z-index:5;
font:14px/1.4 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#fff}
.top a{color:#fff;text-decoration:none;opacity:.92}
.nav{position:fixed;bottom:0;left:0;right:0;display:flex;justify-content:space-between;
align-items:center;padding:14px 18px 26px;background:linear-gradient(transparent,rgba(0,0,0,.8));z-index:5}
.nav a{width:54px;height:54px;border-radius:50%%;background:rgba(255,255,255,.15);
display:grid;place-items:center;color:#fff;font-size:26px;text-decoration:none;flex:0 0 54px}
.nav a:active{background:rgba(255,255,255,.32)}
.cnt{color:#b9c2d0;font:12px/1.5 -apple-system,"PingFang SC",sans-serif;text-align:center;
max-width:58vw;overflow:hidden}
.cnt b{display:block;color:#fff;font-size:13px;font-weight:600;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
</style></head><body>
<img class="pic" src="%(src)s" alt="">
<div class="top"><a href="%(back)s">‹ 返回</a><a href="%(src)s" download="%(fname)s">下载</a></div>
<div class="nav">%(prev)s<div class="cnt"><b>%(name)s</b>%(pos)s</div>%(next)s</div>
<script>
var P=%(prev_js)s,N=%(next_js)s,x0=null;
document.addEventListener('touchstart',function(e){x0=e.touches[0].clientX;},{passive:true});
document.addEventListener('touchend',function(e){
  if(x0===null)return;var dx=e.changedTouches[0].clientX-x0;x0=null;
  if(Math.abs(dx)>60){location.href=dx<0?N:P;}});
document.addEventListener('keydown',function(e){
  if(e.key==='ArrowLeft')location.href=P;if(e.key==='ArrowRight')location.href=N;});
</script></body></html>"""


def render_photo(p, names, idx, back):
    n = len(names)
    name = names[idx]
    src = "/stream?p=" + quote(p)
    if n > 1:
        dirp = p.rsplit("/", 1)[0]
        prev_href = "/photo?p=" + quote(f"{dirp}/{names[(idx - 1) % n]}")
        next_href = "/photo?p=" + quote(f"{dirp}/{names[(idx + 1) % n]}")
        hidden = ""
    else:
        prev_href = next_href = "/photo?p=" + quote(p)
        hidden = ' style="visibility:hidden"'
    return (PHOTO_PAGE % {
        "title": html.escape(name),
        "src": html.escape(src),
        "fname": html.escape(name),
        "back": html.escape(back),
        "name": html.escape(name),
        "pos": f"{idx + 1} / {n}",
        "prev": f'<a href="{prev_href}"{hidden}>‹</a>',
        "next": f'<a href="{next_href}"{hidden}>›</a>',
        "prev_js": json.dumps(prev_href),
        "next_js": json.dumps(next_href),
    }).encode("utf-8")


PDF_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0d12"><title>%(title)s</title><style>
html,body{margin:0;height:100%%;background:#0b0d12;overflow:hidden}
img.pg{position:absolute;inset:0;width:100%%;height:100%%;object-fit:contain;background:#0b0d12}
.top{position:fixed;top:0;left:0;right:0;display:flex;justify-content:space-between;
align-items:center;gap:10px;padding:12px 16px;background:linear-gradient(rgba(0,0,0,.78),transparent);
z-index:5;font:13.5px/1.4 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#fff}
.top a{color:#fff;text-decoration:none;opacity:.92}
.top b{font-weight:600}
.nav{position:fixed;bottom:0;left:0;right:0;display:flex;justify-content:space-between;
align-items:center;gap:10px;padding:12px 14px 22px;
background:linear-gradient(transparent,rgba(0,0,0,.82));z-index:5}
.nav a{width:52px;height:52px;border-radius:50%%;background:rgba(255,255,255,.15);
display:grid;place-items:center;color:#fff;font-size:24px;text-decoration:none;flex:0 0 52px}
.nav a:active{background:rgba(255,255,255,.32)}
.jump{display:flex;gap:6px;align-items:center}
.jump input{width:62px;background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.2);
border-radius:9px;padding:9px;color:#fff;font-size:14px;text-align:center}
.jump button{background:rgba(255,255,255,.16);border:0;border-radius:9px;color:#fff;
padding:9px 12px;font-size:13.5px}
</style></head><body>
<img class="pg" src="%(src)s" alt="">
<div class="top"><a href="%(back)s">‹ 返回</a>
<div><b>%(page)d</b> / %(total)d</div>
<a href="%(dl)s">下载</a></div>
<div class="nav">%(prev)s
<form class="jump" method="get" action="/pdf">
<input type="hidden" name="p" value="%(p)s">
<input name="n" type="number" min="1" max="%(total)d" value="%(page)d" inputmode="numeric">
<button>跳转</button></form>
%(next)s</div>
<script>
var P=%(prev_js)s,N=%(next_js)s,x0=null;
document.addEventListener('touchstart',function(e){x0=e.touches[0].clientX;},{passive:true});
document.addEventListener('touchend',function(e){
  if(x0===null)return;var dx=e.changedTouches[0].clientX-x0;x0=null;
  if(Math.abs(dx)>60){location.href=dx<0?N:P;}});
document.addEventListener('keydown',function(e){
  if(e.key==='ArrowLeft')location.href=P;if(e.key==='ArrowRight')location.href=N;});
try{var im=new Image();im.src=%(prefetch)s;}catch(e){}
</script></body></html>"""


def render_pdf(p, page, total, back):
    src = "/stream?p=" + quote(p)
    if not find_poppler("pdftoppm.exe") or total <= 0:
        body = (
            '<div style="padding:12px 10px">'
            f'<iframe src="{src}" style="width:100%;height:76vh;border:0;border-radius:12px;'
            'background:#fff"></iframe>'
            '<div style="color:#8b93a1;font-size:12.5px;margin-top:10px;line-height:1.8">'
            "浏览器直接打开 PDF。安卓上如果出现空白页，请用下载按钮，"
            "在文件管理器里点开。<br>"
            f'<a href="/download?p={quote(p)}" style="color:#7aa2ff">下载 PDF</a> · '
            f'<a href="{back}" style="color:#7aa2ff">返回</a></div></div>'
        )
        return page("PDF", body)

    page = max(1, min(page, total))
    prev_p = page - 1 if page > 1 else total
    next_p = page + 1 if page < total else 1
    prev_href = f"/pdf?p={quote(p)}&n={prev_p}"
    next_href = f"/pdf?p={quote(p)}&n={next_p}"
    first = "/pdfpage?p=" + quote(p) + "&n=" + str(page) + "&w=1400"
    nxt = "/pdfpage?p=" + quote(p) + "&n=" + str(next_p) + "&w=1400"
    name = os.path.basename(p)
    return (PDF_PAGE % {
        "title": html.escape(name),
        "src": html.escape(first),
        "back": html.escape(back),
        "dl": "/download?p=" + quote(p),
        "p": html.escape(p, quote=True),
        "page": page,
        "total": total,
        "pos": f"{page} / {total}",
        "prev": f'<a href="{prev_href}">‹</a>',
        "next": f'<a href="{next_href}">›</a>',
        "prev_js": json.dumps(prev_href),
        "next_js": json.dumps(next_href),
        "prefetch": json.dumps(nxt),
    }).encode("utf-8")


def render_search(q, roots):
    q = (q or "").strip()
    hits = []
    for i, base in enumerate(roots):
        if os.path.isfile(base):
            if not q or q.lower() in os.path.basename(base).lower():
                hits.append((os.path.basename(base), str(i), os.path.getsize(base)))
            continue
        for dirpath, dirnames, filenames in os.walk(base, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if not d.startswith("$")]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in VIDEO_EXT and ext not in IMAGE_EXT and ext not in PDF_EXT:
                    continue
                if q and q.lower() not in fn.lower():
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, base).replace("\\", "/")
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                hits.append((fn, f"{i}/{rel}", size))
                if len(hits) >= 400:
                    break
            if len(hits) >= 400:
                break

    hits.sort(key=lambda x: x[2], reverse=True)
    rows = []
    for name, p, size in hits[:300]:
        ext = os.path.splitext(name)[1].lower()
        is_pic = ext in IMAGE_EXT
        is_pdf = ext in PDF_EXT
        kind = "photo" if is_pic else ("pdf" if is_pdf else "play")
        icon = "🖼" if is_pic else ("📖" if is_pdf else "▶")
        rows.append(
            f'<a class="row" href="/{kind}?p={quote(p)}">'
            f'<div class="ico">{icon}</div>'
            f'<div class="nm">{html.escape(os.path.splitext(name)[0])}'
            f'<div class="meta">{human(size)}</div></div>{badge_for(ext)}</a>'
        )
    if not rows:
        rows.append(f'<div class="empty">没找到“{html.escape(q)}”。</div>')

    body = (
        f'<header><h1>搜索：{html.escape(q)}</h1>'
        f'<div class="crumb">共 {len(hits)} 个 · <a href="/">回首页</a></div>'
        '<form class="search" action="/search" method="get">'
        f'<input name="q" value="{html.escape(q)}" autocomplete="off"><button>搜</button></form>'
        "</header>"
        f'<div class="list">{"".join(rows)}</div>'
    )
    return page("搜索", body)


def render_login(err=""):
    warn = (f'<div style="color:#ff8f8f;font-size:14px;margin-bottom:12px">{html.escape(err)}</div>'
            if err else "")
    body = (
        '<div style="max-width:380px;margin:22vh auto 0;padding:0 22px">'
        '<div style="font-size:22px;font-weight:650;margin-bottom:6px">私密视频库</div>'
        '<div style="color:#8b93a1;font-size:14px;margin-bottom:22px">输入密码后本机浏览器会记住 30 天</div>'
        f"{warn}"
        '<form method="post" action="/login">'
        '<input name="k" type="password" autofocus placeholder="访问密码" '
        'style="width:100%;background:#191d24;border:1px solid #2a2f3a;border-radius:12px;'
        'padding:14px 15px;color:#e9ecf1;font-size:17px;outline:none">'
        '<button style="width:100%;margin-top:12px;background:#2f6df6;border:0;border-radius:12px;'
        'color:#fff;padding:14px;font-size:16px">进入</button>'
        "</form></div>"
    )
    return page("需要密码", body)


# ---------------------------------------------------------------- 服务

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PhoneVideo/2.0"

    def log_message(self, fmt, *args):
        pass

    @property
    def roots(self):
        return self.server.roots

    @property
    def password(self):
        return self.server.password

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _redirect(self, loc):
        self.send_response(303)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cookie(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "lanvid":
                return v
        return ""

    def _authed(self):
        if not self.password:
            return True
        if self._cookie() == self.server.token:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                dec = b64decode(auth[6:]).decode("utf-8", "replace")
            except Exception:
                return False
            return dec.partition(":")[2] == self.password
        return False

    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route()

    def do_POST(self):
        if urlparse(self.path).path == "/login":
            self._login()
        else:
            self._send(405, b"method not allowed", "text/plain")

    def _login(self):
        ip = self.client_address[0]
        with _fails_lock:
            count, until = _fails.get(ip, (0, 0.0))
        if time.time() < until:
            self._send(429, render_login("尝试次数太多，请稍后再试"))
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = self.rfile.read(min(n, 4096)).decode("utf-8", "replace")
        except Exception:
            data = ""
        form = parse_qs(data)
        pw = form.get("k", [""])[0]
        nxt = form.get("next", ["/"])[0] or "/"
        if not nxt.startswith("/"):
            nxt = "/"
        if self.password and pw == self.password:
            with _fails_lock:
                _fails.pop(ip, None)
            self.send_response(303)
            self.send_header("Location", nxt)
            self.send_header("Set-Cookie",
                             f"lanvid={self.server.token}; Path=/; Max-Age=2592000; "
                             "HttpOnly; SameSite=Lax")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        time.sleep(0.8)
        with _fails_lock:
            count += 1
            _fails[ip] = (0 if count >= 5 else count,
                          time.time() + 60 if count >= 5 else 0.0)
        self._send(401, render_login("密码不对"))

    def _resolve(self, p):
        """p 形如 "0/sub/file.mp4"，第一段是共享位置的序号。"""
        parts = p.split("/")
        i = int(parts[0])
        if i < 0 or i >= len(self.roots):
            raise ValueError("bad root")
        base = self.roots[i]
        rel = "/".join(parts[1:])
        if not rel:
            return base
        if not os.path.isdir(base):
            raise ValueError("not a dir")
        full = os.path.normpath(os.path.join(base, rel.replace("/", os.sep)))
        root_norm = os.path.normpath(base)
        prefix = root_norm if root_norm.endswith(os.sep) else root_norm + os.sep
        if not full.startswith(prefix):
            raise ValueError("escape")
        return full

    def _route(self):
        try:
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            p = qs.get("p", [""])[0]

            if u.path == "/logout":
                self.send_response(303)
                self.send_header("Location", "/login")
                self.send_header("Set-Cookie", "lanvid=; Path=/; Max-Age=0; HttpOnly")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if u.path == "/login":
                self._send(200, render_login())
                return
            if not self._authed():
                if self.command == "GET" and "text/html" in self.headers.get("Accept", ""):
                    self._redirect("/login?next=" + quote(self.path))
                else:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="video"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                return

            if u.path in ("/", "/index.html"):
                self._send(200, render_root(self.roots))
            elif u.path == "/browse":
                parts = p.split("/")
                i = int(parts[0])
                if i < 0 or i >= len(self.roots):
                    raise ValueError("bad root")
                self._send(200, render_dir(i, "/".join(parts[1:]), self.roots))
            elif u.path == "/play":
                full = self._resolve(p)
                if not os.path.isfile(full):
                    raise ValueError("not a file")
                self._send(200, render_play(full, p, self.headers.get("Host", ""), self.password))
            elif u.path == "/photo":
                full = self._resolve(p)
                if not os.path.isfile(full):
                    raise ValueError("not a file")
                if "/" in p:
                    dirp = p.rsplit("/", 1)[0]
                    folder = os.path.dirname(full)
                    try:
                        names = sorted(
                            [n for n in os.listdir(folder)
                             if os.path.splitext(n)[1].lower() in IMAGE_EXT],
                            key=natural_key)
                    except OSError:
                        names = [os.path.basename(full)]
                    back = "/browse?p=" + quote(dirp)
                else:
                    names = [os.path.basename(full)]
                    back = "/"
                base_name = os.path.basename(full)
                idx = names.index(base_name) if base_name in names else 0
                self._send(200, render_photo(p, names, idx, back))
            elif u.path == "/thumb":
                self._thumb(p)
            elif u.path == "/pdf":
                full = self._resolve(p)
                if not os.path.isfile(full):
                    raise ValueError("not a file")
                total = pdf_page_count(full)
                try:
                    n = int(qs.get("n", ["1"])[0])
                except (TypeError, ValueError):
                    n = 1
                back = ("/browse?p=" + quote(p.rsplit("/", 1)[0])
                        if "/" in p else "/")
                self._send(200, render_pdf(p, n, total, back))
            elif u.path == "/pdfpage":
                full = self._resolve(p)
                if not os.path.isfile(full):
                    raise ValueError("not a file")
                try:
                    n = int(qs.get("n", ["1"])[0])
                except (TypeError, ValueError):
                    n = 1
                try:
                    w = int(qs.get("w", ["1400"])[0])
                except (TypeError, ValueError):
                    w = 1400
                data = render_pdf_page(full, max(1, n), max(400, min(2400, w)))
                if not data:
                    self._send(404, b"no page", "text/plain")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
            elif u.path == "/search":
                self._send(200, render_search(qs.get("q", [""])[0], self.roots))
            elif u.path == "/stream":
                self._stream(p)
            elif u.path == "/download":
                self._stream(p, attachment=True)
            elif u.path == "/open.m3u":
                full = self._resolve(p)
                if not os.path.isfile(full):
                    raise ValueError("not a file")
                name = os.path.basename(full)
                data = m3u_bytes(p, self.headers.get("Host", ""), self.password, name)
                fname = os.path.splitext(name)[0] + ".m3u"
                self.send_response(200)
                self.send_header("Content-Type", "audio/x-mpegurl; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition",
                                 'attachment; filename="play.m3u"; filename*=UTF-8\'\''
                                 + quote(fname, safe=""))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
            elif u.path in ("/convert", "/converted"):
                full = self._resolve(p)
                if not os.path.isfile(full):
                    raise ValueError("not a file")
                try:
                    maxh = int(qs.get("h", ["0"])[0])
                except (TypeError, ValueError):
                    maxh = 0
                maxh = maxh if maxh in (0, 480, 720, 1080, 1440, 2160) else 0
                if u.path == "/converted":
                    self._send(200, render_converted(p, os.path.basename(full), maxh))
                else:
                    self._send(200, render_convert(full, p, maxh))
            elif u.path == "/converted/stream":
                full = self._resolve(p)
                try:
                    maxh = int(qs.get("h", ["0"])[0])
                except (TypeError, ValueError):
                    maxh = 0
                maxh = maxh if maxh in (0, 480, 720, 1080, 1440, 2160) else 0
                self._serve_file(cache_path(full, maxh))
            else:
                self._send(404, page("404", '<div class="empty">页面不存在</div>'))
        except ValueError:
            self._send(404, page("没找到", '<div class="empty">这个位置不存在或已被移除。</div>'))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _thumb(self, p):
        if not HAS_PIL:
            self._send(404, b"no thumb", "text/plain")
            return
        try:
            full = self._resolve(p)
        except ValueError:
            self._send(404, b"not found", "text/plain")
            return
        if not os.path.isfile(full):
            self._send(404, b"not found", "text/plain")
            return
        try:
            data = make_thumb(full)
        except Exception:
            self._send(404, b"thumb failed", "text/plain")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _stream(self, p, attachment=False):
        try:
            full = self._resolve(p)
        except ValueError:
            self._send(404, b"not found", "text/plain")
            return
        self._serve_file(full, attachment)

    def _serve_file(self, full, attachment=False):
        if not os.path.isfile(full):
            self._send(404, b"not found", "text/plain")
            return

        size = os.path.getsize(full)
        ext = os.path.splitext(full)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        start, end = 0, size - 1
        partial = False
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()
            if "-" in spec:
                a, _, b = spec.partition("-")
                try:
                    if a == "":
                        start = max(0, size - int(b))
                    else:
                        start = int(a)
                        if b:
                            end = min(int(b), size - 1)
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    partial = True
                except ValueError:
                    pass

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        if attachment:
            self.send_header(
                "Content-Disposition",
                'attachment; filename="video' + ext + '"; filename*=UTF-8\'\''
                + quote(os.path.basename(full), safe=""))
        self.end_headers()
        if self.command == "HEAD":
            return

        try:
            with open(full, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


class VideoServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 下 SO_REUSEADDR 会让两个进程抢同一个端口，关掉它才能正确发现端口被占用
    allow_reuse_address = False

    def __init__(self, addr, roots, password):
        super().__init__(addr, Handler)
        self.roots = list(roots)
        self.password = password
        self.token = hashlib.sha256(("phonevideo:" + password).encode()).hexdigest()[:40]

    def handle_error(self, request, client_address):
        et = sys.exc_info()[0]
        if et and issubclass(et, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


# ---------------------------------------------------------------- 界面

class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.server = None
        self.thread = None
        self.url = ""

        root.title(APP_TITLE)
        root.geometry("620x600")
        root.minsize(560, 520)
        if self.cfg.get("cache_dir"):
            set_cache_dir(self.cfg["cache_dir"])
        if os.path.exists(ICON_PATH):
            try:
                root.iconbitmap(ICON_PATH)
            except Exception:
                pass

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=6)
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("Hint.TLabel", foreground="#6b7280", font=("Microsoft YaHei UI", 9))

        pad = {"padx": 16}
        ttk.Label(root, text=APP_TITLE, font=("Microsoft YaHei UI", 17, "bold")).pack(
            anchor="w", pady=(16, 2), **pad)
        ttk.Label(root, text="同一 WiFi 下，用手机浏览器打开链接即可看视频、照片和 PDF",
                  style="Hint.TLabel").pack(anchor="w", pady=(0, 12), **pad)

        box = ttk.LabelFrame(root, text=" 共享的内容 ", padding=10)
        box.pack(fill="both", expand=True, **pad)

        inner = ttk.Frame(box)
        inner.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(inner, height=7, activestyle="none",
                                  font=("Microsoft YaHei UI", 9),
                                  selectmode="extended", borderwidth=1, relief="solid",
                                  highlightthickness=0)
        sb = ttk.Scrollbar(inner, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        btns = ttk.Frame(box)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="添加文件夹…", command=self.add_folder).pack(side="left")
        ttk.Button(btns, text="添加视频/照片…", command=self.add_files).pack(side="left", padx=6)
        ttk.Button(btns, text="移除选中", command=self.remove_selected).pack(side="left")

        cfgbox = ttk.LabelFrame(root, text=" 访问设置 ", padding=10)
        cfgbox.pack(fill="x", pady=(12, 0), **pad)

        row = ttk.Frame(cfgbox)
        row.pack(fill="x")
        ttk.Label(row, text="密码").pack(side="left")
        self.pw_var = tk.StringVar(value=self.cfg["password"])
        self.pw_entry = ttk.Entry(row, textvariable=self.pw_var, width=18)
        self.pw_entry.pack(side="left", padx=(8, 8))
        ttk.Button(row, text="换一个", command=self.random_pw).pack(side="left")
        ttk.Label(row, text="端口").pack(side="left", padx=(24, 0))
        self.port_var = tk.StringVar(value=str(self.cfg["port"]))
        self.port_entry = ttk.Entry(row, textvariable=self.port_var, width=7)
        self.port_entry.pack(side="left", padx=(8, 0))

        ttk.Label(cfgbox, text="手机打开下面这个地址，输入上面这个密码即可",
                  style="Hint.TLabel").pack(anchor="w", pady=(10, 0))

        self.start_btn = tk.Button(root, text="启动服务", command=self.toggle,
                                   font=("Microsoft YaHei UI", 12, "bold"),
                                   bg="#2f6df6", fg="white", activebackground="#2559cf",
                                   activeforeground="white", relief="flat", cursor="hand2",
                                   height=2)
        self.start_btn.pack(fill="x", pady=(14, 8), **pad)

        self.url_var = tk.StringVar(value="服务未启动")
        self.url_label = tk.Label(root, textvariable=self.url_var,
                                  font=("Consolas", 13, "bold"), fg="#2563eb",
                                  cursor="hand2")
        self.url_label.pack(**pad)
        self.url_label.bind("<Button-1>", lambda e: self.copy_url())

        self.hint_var = tk.StringVar(value="")
        ttk.Label(root, textvariable=self.hint_var, style="Hint.TLabel",
                  wraplength=560, justify="left").pack(anchor="w", pady=(8, 16), **pad)

        cache_row = ttk.Frame(root)
        cache_row.pack(fill="x", pady=(0, 14), **pad)
        ttk.Button(cache_row, text="打开缓存文件夹", command=self.open_cache).pack(side="left")
        ttk.Button(cache_row, text="更改缓存位置…", command=self.change_cache).pack(
            side="left", padx=6)
        self.cache_var = tk.StringVar(value="")
        ttk.Label(cache_row, textvariable=self.cache_var, style="Hint.TLabel").pack(
            side="left", padx=10)
        self.refresh_cache_label()

        ttk.Label(root, text="组件：" + components_text(), style="Hint.TLabel").pack(
            anchor="w", pady=(0, 12), **pad)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh()

    def open_cache(self):
        d = ensure_cache_dir()
        if d:
            try:
                os.startfile(d)
            except OSError:
                pass
        self.refresh_cache_label()

    def change_cache(self):
        d = filedialog.askdirectory(title="选择转码缓存位置")
        if not d:
            return
        target = os.path.normpath(d)
        old_cfg = self.cfg.get("cache_dir") or ""
        got = set_cache_dir(target)
        if got and os.path.normpath(got) == target:
            self.cfg["cache_dir"] = target
            save_config(self.cfg)
            self.refresh_cache_label()
            self.hint_var.set(f"缓存位置已改为 {target}。旧位置的文件不会自动搬，"
                              "需要的话手动删掉即可。")
        else:
            set_cache_dir(old_cfg)
            self.refresh_cache_label()
            messagebox.showerror(APP_TITLE, "这个文件夹写不进去，换一个试试。")

    def refresh_cache_label(self):
        d = ensure_cache_dir()
        if not d:
            self.cache_var.set("缓存位置不可用")
            return
        short = d if len(d) <= 52 else d[:24] + "…" + d[-24:]
        self.cache_var.set(f"{short} · 已用 {human(cache_size())}")

    # ---- 列表

    def refresh(self):
        self.listbox.delete(0, "end")
        for path in self.cfg["roots"]:
            if os.path.isdir(path):
                mark = "📁"
            elif os.path.splitext(path)[1].lower() in IMAGE_EXT:
                mark = "🖼"
            else:
                mark = "🎬"
            self.listbox.insert("end", f"{mark}  {path}")
        if not self.cfg["roots"]:
            self.listbox.insert("end", "（还没有添加内容，点下面的按钮添加）")

    def add_folder(self):
        path = filedialog.askdirectory(title="选择要共享的文件夹")
        if path:
            self._add(os.path.normpath(path))

    def add_files(self):
        files = filedialog.askopenfilenames(
            title="选择视频或照片",
            filetypes=[("视频和照片", " ".join("*" + e for e in sorted(VIDEO_EXT | IMAGE_EXT))),
                       ("视频文件", " ".join("*" + e for e in sorted(VIDEO_EXT))),
                       ("图片文件", " ".join("*" + e for e in sorted(IMAGE_EXT))),
                       ("所有文件", "*.*")])
        for f in files:
            self._add(os.path.normpath(f))

    def _add(self, path):
        if path in self.cfg["roots"]:
            return
        self.cfg["roots"].append(path)
        save_config(self.cfg)
        self.refresh()

    def remove_selected(self):
        idx = list(self.listbox.curselection())
        if not idx or not self.cfg["roots"]:
            return
        for i in reversed(idx):
            if i < len(self.cfg["roots"]):
                del self.cfg["roots"][i]
        save_config(self.cfg)
        self.refresh()
        if self.server:
            self.server.roots = self.cfg["roots"]

    def random_pw(self):
        self.pw_var.set("".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(8)))

    # ---- 启停

    def toggle(self):
        if self.server:
            self.stop()
        else:
            self.start()

    def start(self):
        roots = [p for p in self.cfg["roots"] if os.path.exists(p)]
        if not roots:
            messagebox.showwarning(APP_TITLE, "先添加至少一个文件夹或视频文件。")
            return
        missing = len(self.cfg["roots"]) - len(roots)

        try:
            want = int(self.port_var.get())
        except ValueError:
            messagebox.showwarning(APP_TITLE, "端口要填数字。")
            return

        self.cfg["password"] = self.pw_var.get().strip()
        self.cfg["port"] = want
        save_config(self.cfg)

        srv = None
        port = want
        for candidate in range(want, want + 20):
            try:
                srv = VideoServer(("0.0.0.0", candidate), roots, self.cfg["password"])
                port = candidate
                break
            except OSError:
                continue
        if srv is None:
            messagebox.showerror(APP_TITLE, "端口都被占用了，换一个端口号试试。")
            return

        self.server = srv
        self.thread = threading.Thread(target=srv.serve_forever, daemon=True)
        self.thread.start()

        ips = lan_ips()
        self.url = f"http://{ips[0]}:{port}"
        self.url_var.set(self.url)
        self.start_btn.configure(text="停止服务", bg="#dc2626", activebackground="#b91c1c")
        self.pw_entry.configure(state="disabled")
        self.port_entry.configure(state="disabled")
        tip = "服务运行中。手机连同一个 WiFi，打开上面的地址。"
        if port != want:
            tip += f"（{want} 被占用，改用了 {port}）"
        if missing:
            tip += f" 有 {missing} 个位置已失效，已自动跳过。"
        if not self.cfg["password"]:
            tip += " 你还没设密码，局域网内任何人都能打开。"
        if len(ips) > 1:
            tip += " 如果打不开，换个网卡地址试试：" + "、".join(
                f"{ip}:{port}" for ip in ips[1:3])
        self.hint_var.set(tip)

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.server = None
        self.url_var.set("服务未启动")
        self.start_btn.configure(text="启动服务", bg="#2f6df6", activebackground="#2559cf")
        self.pw_entry.configure(state="normal")
        self.port_entry.configure(state="normal")
        self.hint_var.set("已停止。")

    def copy_url(self):
        if not self.url:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.url)
        self.hint_var.set("链接已复制，发到手机或直接在手机浏览器输入即可。")

    def on_close(self):
        self.cfg["roots"] = [p for p in self.cfg["roots"]]
        save_config(self.cfg)
        self.stop()
        self.root.destroy()


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    cleanup_cache()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    if "--serve" in sys.argv:
        paths = sys.argv[sys.argv.index("--serve") + 1:]
        cleanup_cache()
        srv = VideoServer(("0.0.0.0", DEFAULT_PORT), paths, "test123")
        print("listening on", DEFAULT_PORT, flush=True)
        srv.serve_forever()
    else:
        main()
