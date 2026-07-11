#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Adobe Connect Downloader + Offline Renderer
==========================================

Usage:
    python adobe_connect_downloader.py "https://liveme.ac.ir/py82yo5lic0k/?session=..." -o class.mp4

What it does:
    1) Opens the Adobe Connect class/recording URL with the given session.
    2) Keeps cookies/session and downloads /<meeting>/output/name.zip?download=zip.
    3) Extracts the recording package.
    4) Parses XML timeline streams: duration, room size, content descriptors, PDF events,
       PDF page/memento events, presenter pointer/cursor events, whiteboard pencil annotations, chat messages, and audio timing.
    5) Downloads PDF/source files referenced in indexstream/mainstream.
    6) Copies the downloaded/found PDF files into a separate output folder next to the MP4.
    7) Renders a redesigned classroom video with slides + annotations + presenter pointer + a live chat panel.
    8) Aligns cameraVoip/audio FLVs by pacingTick and muxes final MP4.

Dependencies:
    - Python 3.9+
    - ffmpeg + ffprobe available in PATH
    - pip install requests pillow pymupdf

Notes:
    Adobe Connect recordings are not a single standard format; they are a set of FLV/XML shared-object
    streams. This renderer reconstructs the common/important parts: audio timeline, PDF slide timeline,
    presenter pointer/cursor position, whiteboard drawings, basic layout, and detected screen/video FLV overlays. Some proprietary pod states
    may need site-specific tweaks.
"""

from __future__ import annotations

import argparse
import bisect
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys

# Windows/redirected stdout safety: force UTF-8 so Persian logs and symbols do not
# crash when the script is run from the local web UI or a non-UTF-8 console.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import tempfile
import textwrap
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover
    Image = None
    ImageDraw = None
    ImageFont = None

try:  # Optional: improves Persian/Arabic chat rendering when installed.
    import arabic_reshaper  # type: ignore
    from bidi.algorithm import get_display  # type: ignore
except Exception:  # pragma: no cover
    arabic_reshaper = None
    get_display = None

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover
    fitz = None


# ----------------------------- Data models -----------------------------

@dataclass
class DocumentAsset:
    ct_id: int
    name: str
    the_type: str = ""
    download_url: str = ""
    content_output_path: str = ""
    playback_content_output_path: str = ""
    sco_path: str = ""
    local_path: Optional[Path] = None
    page_images: List[Path] = field(default_factory=list)


@dataclass
class ContentEvent:
    time_ms: int
    ct_id: int


@dataclass
class MementoEvent:
    time_ms: int
    ct_id: Optional[int]
    raw: str
    page_index: int
    values: Dict[str, str]


@dataclass
class PointerEvent:
    time_ms: int
    ct_id: Optional[int]
    page_index: Optional[int]
    x: Optional[float]
    y: Optional[float]
    visible: bool = True
    source: str = ""
    coord_mode: str = "auto"


@dataclass
class AnnotationShape:
    time_ms: int
    page_index: Optional[int]
    shape_type: str
    x: float
    y: float
    width: float
    height: float
    stroke_col: int
    stroke_weight: float
    alpha: float
    points: List[Tuple[float, float]]
    html_text: str = ""


@dataclass
class AudioSegment:
    flv_path: Path
    xml_path: Optional[Path]
    start_ms: int
    duration_s: float


@dataclass
class VideoSegment:
    flv_path: Path
    xml_path: Optional[Path]
    start_ms: int
    duration_s: float
    width: int = 0
    height: int = 0


@dataclass
class ChatMessage:
    time_ms: int
    sender: str
    text: str
    raw_when: str = ""


# ----------------------------- Utility functions -----------------------------

ANSI_DIM = "\033[2m"
ANSI_RESET = "\033[0m"


def step(msg: str) -> None:
    print(f"\n▶ {msg}", flush=True)


def info(msg: str) -> None:
    print(f"  {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"  ⚠ {msg}", flush=True)


def die(msg: str, exit_code: int = 1) -> None:
    print(f"\nخطا: {msg}", file=sys.stderr)
    raise SystemExit(exit_code)


def ensure_binary(name: str) -> None:
    if shutil.which(name) is None:
        die(f"برنامه‌ی `{name}` در PATH پیدا نشد. ffmpeg/ffprobe را نصب کن و دوباره اجرا کن.")


def run(cmd: Sequence[str], *, input_bytes: Optional[bytes] = None, check: bool = True) -> subprocess.CompletedProcess:
    printable = " ".join(shlex_quote(c) for c in cmd)
    info(f"$ {printable}")
    return subprocess.run(cmd, input=input_bytes, check=check)


def run_capture(cmd: Sequence[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def shlex_quote(s: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def cdata_tag(block: str, tag: str) -> str:
    # Handles <tag><![CDATA[x]]></tag> and <tag>x</tag>
    m = re.search(rf"<{re.escape(tag)}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{re.escape(tag)}>", block, re.S)
    if not m:
        return ""
    return unescape_xml_text(m.group(1).strip())


def unescape_xml_text(s: str) -> str:
    return (
        s.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
    )


def first_number_in_metadata(xml_text: str) -> Optional[float]:
    m = re.search(r"<Method><!\[CDATA\[onMetaData\]\]></Method>.*?<Number><!\[CDATA\[([0-9.]+)\]\]></Number>", xml_text, re.S)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def ffprobe_json(path: Path) -> dict:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=index,codec_type,codec_name,width,height",
        "-of", "json",
        str(path),
    ]
    cp = run_capture(cmd)
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError:
        return {}


def duration_from_ffprobe(path: Path) -> float:
    data = ffprobe_json(path)
    try:
        return float(data.get("format", {}).get("duration") or 0.0)
    except Exception:
        return 0.0


def duration_from_xml_metadata(xml_path: Optional[Path]) -> float:
    if not xml_path or not xml_path.exists():
        return 0.0
    try:
        return float(first_number_in_metadata(read_text(xml_path)) or 0.0)
    except Exception:
        return 0.0


def stream_types(path: Path) -> Tuple[bool, bool, int, int]:
    data = ffprobe_json(path)
    has_audio = False
    has_video = False
    w = h = 0
    for st in data.get("streams", []):
        if st.get("codec_type") == "audio":
            has_audio = True
        if st.get("codec_type") == "video":
            has_video = True
            w = int(st.get("width") or 0)
            h = int(st.get("height") or 0)
    return has_audio, has_video, w, h


def paired_xml_for(flv: Path) -> Optional[Path]:
    p = flv.with_suffix(".xml")
    return p if p.exists() else None


def parse_first_pacing_start_ms(xml_path: Optional[Path]) -> int:
    """Fallback stream start estimate from the first pacingTick.

    The canonical Adobe Connect start time is normally in indexstream/mainstream
    streamAdded/startTime.  When that metadata is unavailable, the first
    pacingTick is the next-best estimate because:

        stream_absolute_time ~= message_local_time + stream_start_offset

    Later pacingTick values can drift slightly, so averaging/medianing them may
    move the whole voice a few hundred milliseconds away from the actual
    streamAdded time.
    """
    if not xml_path or not xml_path.exists():
        return 0
    txt = read_text(xml_path)
    m = re.search(
        r'<Message\s+time="(?P<t>\d+)"[^>]*>\s*<Method><!\[CDATA\[pacingTick\]\]></Method>\s*<Number><!\[CDATA\[(?P<n>[0-9.]+)\]\]></Number>',
        txt,
        re.S,
    )
    if not m:
        return 0
    try:
        local_ms = int(float(m.group("t")))
        absolute_ms = int(float(m.group("n")))
        return max(0, absolute_ms - local_ms)
    except Exception:
        return 0


def safe_filename(name: str) -> str:
    name = unquote(name).strip().replace("\\", "_").replace("/", "_")
    return re.sub(r"[^\w.()\-\u0600-\u06FF ]+", "_", name) or "asset"


def even_dimension(value: int) -> int:
    """Return an H.264-safe even dimension. libx264 rejects odd widths/heights."""
    value = int(value)
    return value if value % 2 == 0 else value + 1


def parse_memento(raw: str) -> Dict[str, str]:
    parts = [p for p in raw.split("|") if p]
    values: Dict[str, str] = {}
    for p in parts:
        # Adobe memento tokens are like tPgNum-3, tPgPct-0.5, rotn-0
        if "-" in p:
            k, v = p.split("-", 1)
            values[k] = v
    return values


def memento_page_index(values: Dict[str, str]) -> int:
    for key in ("tPgNum", "bPgNum", "currentPage"):
        if key in values:
            try:
                return max(0, int(float(values[key])))
            except Exception:
                pass
    return 0


def color_int_to_rgb(value: int, default=(224, 38, 38)) -> Tuple[int, int, int]:
    try:
        v = int(value)
        if v <= 0:
            return (20, 20, 20)
        return ((v >> 16) & 255, (v >> 8) & 255, v & 255)
    except Exception:
        return default


def which_font() -> Optional[str]:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    return None


# ----------------------------- Main renderer/downloader -----------------------------

class AdobeConnectRenderer:
    def __init__(
        self,
        class_url: str,
        output: Path,
        workdir: Optional[Path],
        fps: float = 2.0,
        width: Optional[int] = None,
        height: Optional[int] = None,
        keep: bool = False,
        offline_zip: Optional[Path] = None,
        skip_download_assets: bool = False,
        pdf_output_dir: Optional[Path] = None,
        ffmpeg_preset: str = "ultrafast",
        video_crf: int = 24,
        audio_bitrate: str = "96k",
        screen_fps: float = 6.0,
        segment_seconds: float = 0.0,
        first_segment_seconds: float = 0.0,
        clip_start_s: float = 0.0,
        clip_duration_s: float = 0.0,
        encoder: str = "auto",
        workers: int = 1,
    ) -> None:
        self.class_url = class_url.strip()
        self.output = output
        self.fps = fps
        self.forced_width = width
        self.forced_height = height
        self.keep = keep
        self.offline_zip = offline_zip
        self.skip_download_assets = skip_download_assets
        self.pdf_output_dir = pdf_output_dir
        self.ffmpeg_preset = ffmpeg_preset
        self.video_crf = max(18, min(35, int(video_crf)))
        self.audio_bitrate = str(audio_bitrate or "96k")
        self.screen_fps = max(1.0, float(screen_fps))
        self.segment_seconds = max(0.0, float(segment_seconds or 0.0))
        self.first_segment_seconds = max(0.0, float(first_segment_seconds or 0.0))
        self.clip_start_s = max(0.0, float(clip_start_s or 0.0))
        self.clip_duration_s = max(0.0, float(clip_duration_s or 0.0))
        self.encoder_preference = (encoder or "auto").strip().lower()
        self.workers = max(1, int(workers or 1))

        # Render-speed caches. Font discovery/opening, background drawing, and PDF page
        # decoding were previously repeated for every frame and made long classes very slow.
        self._font_path = which_font()
        self._font_cache = {}
        self._background_cache = None
        self._page_image_cache = {}
        self._resized_page_cache = {}
        self._encoder_runtime_cache = {}

        self.workdir = workdir or Path(tempfile.mkdtemp(prefix="adobe_connect_"))
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.extract_dir = self.workdir / "recording"
        self.assets_dir = self.workdir / "assets"
        self.pages_dir = self.assets_dir / "pages"
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.pages_dir.mkdir(parents=True, exist_ok=True)

        self.session = requests.Session() if requests else None
        self.origin = ""
        self.meeting_path = ""
        self.session_token = ""

        self.room_w = 1280
        self.room_h = 720
        self.duration_s = 0.0
        self.documents: Dict[int, DocumentAsset] = {}
        self.content_events: List[ContentEvent] = []
        self.mementos: List[MementoEvent] = []
        self.pointer_events: List[PointerEvent] = []
        self.annotations: List[AnnotationShape] = []
        self.user_names: Dict[int, str] = {}
        self.chat_messages: List[ChatMessage] = []
        self.audio_segments: List[AudioSegment] = []
        self.video_segments: List[VideoSegment] = []
        self.stream_start_times: Dict[str, int] = {}
        self._content_event_times: List[int] = []
        self._memento_times: List[int] = []
        self._pointer_times: List[int] = []
        self._annotation_times: List[int] = []
        self._chat_times: List[int] = []
        self._video_start_times: List[int] = []

    # ------------------------- URL/session/download -------------------------

    def parse_url(self) -> None:
        parsed = urlparse(self.class_url)
        if not parsed.scheme or not parsed.netloc:
            die("لینک کلاس معتبر نیست. لینک باید با http یا https شروع شود.")
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.meeting_path = parsed.path.strip("/")
        q = parse_qs(parsed.query)
        self.session_token = (q.get("session") or [""])[0]
        if not self.meeting_path:
            die("meeting path از داخل URL پیدا نشد.")
        info(f"Host: {self.origin}")
        info(f"Meeting path: /{self.meeting_path}/")
        if self.session_token:
            info("Session token detected in URL.")

    def open_session(self) -> None:
        if requests is None:
            die("ماژول requests نصب نیست. اجرا کن: pip install requests")
        assert self.session is not None
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 AdobeConnectDownloader/1.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        parsed = urlparse(self.class_url)
        if self.session_token:
            # Adobe Connect usually sets real cookies after the first GET, but adding likely cookie names
            # improves compatibility with some reverse proxies.
            domain = parsed.hostname or ""
            for cname in ("BREEZESESSION", "breezesession", "session"):
                try:
                    self.session.cookies.set(cname, self.session_token, domain=domain)
                except Exception:
                    pass
        step("ورود اولیه به کلاس و ذخیره Cookie/Session")
        r = self.session.get(self.class_url, allow_redirects=True, timeout=60)
        info(f"GET class URL -> HTTP {r.status_code}, cookies={len(self.session.cookies)}")
        if r.status_code >= 400:
            warn("ورود اولیه خطای HTTP داد؛ با این حال دانلود zip را هم امتحان می‌کنم.")

    def recording_zip_url(self) -> str:
        # Example: https://liveme.ac.ir/py82yo5lic0k/output/name.zip?download=zip
        return f"{self.origin}/{self.meeting_path}/output/name.zip?download=zip"

    def download_zip(self) -> Path:
        if self.offline_zip:
            z = self.offline_zip.resolve()
            if not z.exists():
                die(f"فایل zip داده‌شده پیدا نشد: {z}")
            return z

        assert self.session is not None
        url = self.recording_zip_url()
        zip_path = self.workdir / "recording.zip"
        step("دانلود فایل ZIP ضبط کلاس")
        info(url)
        with self.session.get(url, stream=True, allow_redirects=True, timeout=120) as r:
            info(f"HTTP {r.status_code}, content-type={r.headers.get('content-type', '')}")
            if r.status_code >= 400:
                die("دانلود ZIP ناموفق بود. احتمالاً session/cookie معتبر نیست یا دسترسی به recording بسته است.")
            with zip_path.open("wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 512):
                    if chunk:
                        f.write(chunk)
        # Validate zip magic, because Adobe Connect may return an HTML login page.
        with zip_path.open("rb") as f:
            magic = f.read(4)
        if magic[:2] != b"PK":
            sample = zip_path.read_bytes()[:400].decode("utf-8", errors="ignore")
            bad_path = self.workdir / "not_a_zip_response.html"
            shutil.copy(zip_path, bad_path)
            die(
                "پاسخ سرور ZIP نبود؛ احتمالاً صفحه‌ی Login برگشته است. "
                f"نمونه پاسخ در {bad_path} ذخیره شد. ابتدای پاسخ:\n{sample[:300]}"
            )
        info(f"Saved: {zip_path} ({zip_path.stat().st_size / 1024 / 1024:.2f} MB)")
        return zip_path

    def extract_zip(self, zip_path: Path) -> None:
        step("استخراج فایل‌های ضبط")
        cache_root_env = os.environ.get("AC_CACHE_DIR", "").strip()
        input_hash = os.environ.get("AC_INPUT_HASH", "").strip()
        cache_extract: Optional[Path] = None
        if cache_root_env and input_hash:
            cache_extract = Path(cache_root_env) / "extracted" / input_hash
            if cache_extract.exists() and any(cache_extract.rglob("*.xml")):
                if self.extract_dir.exists():
                    shutil.rmtree(self.extract_dir)
                shutil.copytree(cache_extract, self.extract_dir)
                info("این فایل قبلاً پردازش شده؛ فایل‌های استخراج‌شده از cache استفاده شد.")
                files = list(self.extract_dir.rglob("*"))
                info(f"Extracted files: {len(files)}")
                for ext in ("*.xml", "*.flv", "*.pdf"):
                    info(f"{ext}: {len(list(self.extract_dir.rglob(ext)))}")
                return

        if self.extract_dir.exists():
            shutil.rmtree(self.extract_dir)
        self.extract_dir.mkdir(parents=True)
        try:
            with zipfile.ZipFile(zip_path, "r") as z:
                # Basic zip-slip protection
                for member in z.infolist():
                    dest = (self.extract_dir / member.filename).resolve()
                    if not str(dest).startswith(str(self.extract_dir.resolve())):
                        die(f"Zip unsafe path detected: {member.filename}")
                z.extractall(self.extract_dir)
        except zipfile.BadZipFile:
            die("فایل ZIP خراب است یا Adobe Connect package معتبر نیست. لطفاً فایل recording را دوباره دانلود کن.")
        files = list(self.extract_dir.rglob("*"))
        info(f"Extracted files: {len(files)}")
        for ext in ("*.xml", "*.flv", "*.pdf"):
            info(f"{ext}: {len(list(self.extract_dir.rglob(ext)))}")

        if cache_extract:
            try:
                cache_extract.parent.mkdir(parents=True, exist_ok=True)
                if cache_extract.exists():
                    shutil.rmtree(cache_extract)
                shutil.copytree(self.extract_dir, cache_extract)
                info(f"Cache saved: {cache_extract}")
            except Exception as exc:
                warn(f"ذخیره cache استخراج ناموفق بود: {exc}")

    # ------------------------- Parsing -------------------------

    def parse_recording(self) -> None:
        step("تحلیل XML/FLV و ساخت timeline")
        xml_files = sorted(self.extract_dir.rglob("*.xml"))
        flv_files = sorted(self.extract_dir.rglob("*.flv"))

        self.stream_start_times = self.parse_stream_start_times(xml_files)
        info(f"Canonical stream start events: {len(self.stream_start_times)}")

        self.duration_s = self.detect_duration(xml_files, flv_files)
        self.room_w, self.room_h = self.detect_room_size(xml_files)
        if self.forced_width and self.forced_height:
            self.room_w, self.room_h = self.forced_width, self.forced_height

        raw_room_w, raw_room_h = self.room_w, self.room_h
        self.room_w, self.room_h = even_dimension(self.room_w), even_dimension(self.room_h)
        if (self.room_w, self.room_h) != (raw_room_w, raw_room_h):
            warn(
                f"ابعاد roomSize برای H.264 فرد بود ({raw_room_w}x{raw_room_h})؛ "
                f"به {self.room_w}x{self.room_h} اصلاح شد."
            )

        info(f"Duration: {self.duration_s:.2f}s")
        info(f"Room/video size: {self.room_w}x{self.room_h}")

        self.documents = self.parse_documents(xml_files)
        info(f"Document descriptors found: {len(self.documents)}")
        for d in sorted(self.documents.values(), key=lambda x: x.ct_id):
            info(f"ctID={d.ct_id}  {d.name}  type={d.the_type}")

        ftcontent_files = [
            p for p in xml_files
            if "ftcontent" in p.name.lower()
            or "pointer" in p.name.lower()
            or "cursor" in p.name.lower()
            or "mouse" in p.name.lower()
            or "laser" in p.name.lower()
        ]
        if not ftcontent_files:
            ftcontent_files = xml_files
        self.parse_content_timeline(ftcontent_files)
        info(f"Content switch events: {len(self.content_events)}")
        info(f"PDF memento/page events: {len(self.mementos)}")
        info(f"Presenter pointer/cursor events: {len(self.pointer_events)}")
        info(f"Whiteboard/annotation shapes: {len(self.annotations)}")

        self.user_names = self.parse_user_names(xml_files)
        ftchat_files = [p for p in xml_files if "ftchat" in p.name.lower()]
        self.chat_messages = self.parse_chat_messages(ftchat_files)
        info(f"Chat messages: {len(self.chat_messages)}")

        self.audio_segments = self.discover_audio(flv_files)
        info(f"Audio segments: {len(self.audio_segments)}")
        for a in self.audio_segments:
            info(f"audio {a.flv_path.name}: start={a.start_ms/1000:.3f}s duration={a.duration_s:.3f}s")

        self.video_segments = self.discover_video(flv_files)
        info(f"Detected video FLV streams for optional overlay: {len(self.video_segments)}")
        for v in self.video_segments:
            info(f"video {v.flv_path.name}: {v.width}x{v.height}, start={v.start_ms/1000:.3f}s duration={v.duration_s:.3f}s")
        self._build_timeline_indexes()

    def _build_timeline_indexes(self) -> None:
        """Precompute time indexes used on every rendered frame."""
        self.content_events.sort(key=lambda ev: ev.time_ms)
        self.mementos.sort(key=lambda ev: ev.time_ms)
        self.pointer_events.sort(key=lambda ev: ev.time_ms)
        self.annotations.sort(key=lambda ev: ev.time_ms)
        self.chat_messages.sort(key=lambda ev: ev.time_ms)
        self.video_segments.sort(key=lambda ev: ev.start_ms)
        self._content_event_times = [int(ev.time_ms) for ev in self.content_events]
        self._memento_times = [int(ev.time_ms) for ev in self.mementos]
        self._pointer_times = [int(ev.time_ms) for ev in self.pointer_events]
        self._annotation_times = [int(ev.time_ms) for ev in self.annotations]
        self._chat_times = [int(ev.time_ms) for ev in self.chat_messages]
        self._video_start_times = [int(ev.start_ms) for ev in self.video_segments]

    def detect_duration(self, xml_files: List[Path], flv_files: List[Path]) -> float:
        best = 0.0
        for x in xml_files:
            txt = read_text(x)
            meta = first_number_in_metadata(txt)
            if meta:
                best = max(best, meta)
            for m in re.finditer(r'<Message\s+time="(\d+)"', txt):
                try:
                    best = max(best, int(m.group(1)) / 1000.0)
                except Exception:
                    pass
        # XML metadata is the reliable and fast source for Adobe Connect recording duration.
        # Probing every FLV can be very slow on some screenshare streams, so only use
        # ffprobe as a last-resort fallback if XML gives no duration at all.
        if best <= 0:
            for f in flv_files:
                best = max(best, duration_from_ffprobe(f))
        if best <= 0:
            best = 60.0
            warn("مدت کلاس از فایل‌ها پیدا نشد؛ پیش‌فرض ۶۰ ثانیه در نظر گرفته شد.")
        return best

    def detect_room_size(self, xml_files: List[Path]) -> Tuple[int, int]:
        for name in ("indexstream.xml", "mainstream.xml"):
            for p in xml_files:
                if p.name.lower() == name:
                    txt = read_text(p)
                    m = re.search(r"<roomSize>\s*<Number><!\[CDATA\[([0-9.]+)\]\]></Number>\s*<Number><!\[CDATA\[([0-9.]+)\]\]></Number>", txt, re.S)
                    if m:
                        return int(float(m.group(1))), int(float(m.group(2)))
        return 1280, 720

    def parse_documents(self, xml_files: List[Path]) -> Dict[int, DocumentAsset]:
        docs: Dict[int, DocumentAsset] = {}
        for p in xml_files:
            if p.name.lower() not in ("indexstream.xml", "mainstream.xml"):
                continue
            txt = read_text(p)
            # Parse newValue blocks containing documentDescriptor.
            for block_m in re.finditer(r"<newValue>(?P<block>.*?)</newValue>", txt, re.S):
                block = block_m.group("block")
                if "<documentDescriptor>" not in block:
                    continue
                ct_raw = cdata_tag(block, "ctID")
                try:
                    ct_id = int(float(ct_raw))
                except Exception:
                    continue
                desc_m = re.search(r"<documentDescriptor>(.*?)</documentDescriptor>", block, re.S)
                desc = desc_m.group(1) if desc_m else block
                name = cdata_tag(desc, "theName") or f"ct_{ct_id}"
                the_type = cdata_tag(desc, "theType")
                durl = cdata_tag(desc, "downloadUrl")
                content_output = cdata_tag(desc, "contentOutputPath")
                playback_output = cdata_tag(desc, "playbackContentOutputPath")
                sco_path = cdata_tag(desc, "scoPath")
                # Prefer source-downloadable descriptors. Duplicate ctIDs can appear; keep latest richer object.
                existing = docs.get(ct_id)
                candidate = DocumentAsset(
                    ct_id=ct_id,
                    name=name,
                    the_type=the_type,
                    download_url=durl,
                    content_output_path=content_output,
                    playback_content_output_path=playback_output,
                    sco_path=sco_path,
                )
                if existing is None or (candidate.download_url and not existing.download_url):
                    docs[ct_id] = candidate
        return docs

    def parse_content_timeline(self, xml_files: List[Path]) -> None:
        current_ctid: Optional[int] = None
        current_page_index: Optional[int] = None
        events: List[ContentEvent] = []
        mementos: List[MementoEvent] = []
        pointer_events: List[PointerEvent] = []
        annotations: List[AnnotationShape] = []

        for p in xml_files:
            txt = read_text(p)
            for msg_m in re.finditer(r'<Message\s+time="(?P<t>\d+)"[^>]*>(?P<body>.*?)</Message>', txt, re.S):
                time_ms = int(msg_m.group("t"))
                body = msg_m.group("body")
                string = cdata_tag(body, "String")
                if string == "setContentSo":
                    # One message can contain multiple changes. Look for ctID change.
                    ct_change = self.extract_change_value(body, "ctID")
                    if ct_change is not None:
                        try:
                            current_ctid = int(float(ct_change))
                            events.append(ContentEvent(time_ms=time_ms, ct_id=current_ctid))
                        except Exception:
                            pass
                elif string == "setPdfContentSo":
                    raw = self.extract_change_value(body, "memento")
                    if raw:
                        vals = parse_memento(raw)
                        page = memento_page_index(vals)
                        current_page_index = page
                        mementos.append(MementoEvent(time_ms, current_ctid, raw, page, vals))
                    cur_page = self.extract_change_value(body, "currentPage")
                    if cur_page is not None:
                        try:
                            page = max(0, int(float(cur_page)))
                            current_page_index = page
                            raw2 = f"|tPgNum-{page}|"
                            mementos.append(MementoEvent(time_ms, current_ctid, raw2, page, {"tPgNum": str(page)}))
                        except Exception:
                            pass
                elif string.startswith("set_WB_So_"):
                    shape = self.parse_annotation_shape(body, time_ms, string)
                    if shape:
                        annotations.append(shape)

                pointer_events.extend(
                    self.parse_pointer_events(body, time_ms, string, current_ctid, current_page_index)
                )

        self.content_events = sorted(events, key=lambda e: e.time_ms)
        self.mementos = sorted(mementos, key=lambda e: e.time_ms)
        self.pointer_events = sorted(pointer_events, key=lambda e: e.time_ms)
        self.annotations = sorted(annotations, key=lambda e: e.time_ms)

        # If no explicit switch event but docs exist, start with the first valid document.
        if not self.content_events and self.documents:
            first_ctid = sorted(self.documents.keys())[0]
            self.content_events.append(ContentEvent(0, first_ctid))
        if self.content_events:
            # Normalize negative ctIDs as "no share" but keep them for gaps.
            pass

    def extract_change_values(self, body: str) -> Dict[str, str]:
        """Return all simple Adobe Connect shared-object change/update values in one message."""
        values: Dict[str, str] = {}
        pattern = (
            r"<Object>\s*<code><!\[CDATA\[(?:change|update)\]\]></code>\s*"
            r"<name><!\[CDATA\[(?P<name>.*?)\]\]></name>\s*"
            r"<newValue>(?P<value>.*?)</newValue>"
        )
        for m in re.finditer(pattern, body, re.S):
            name = unescape_xml_text(m.group("name").strip())
            val = m.group("value").strip()
            c = re.match(r"<!\[CDATA\[(.*?)\]\]>", val, re.S)
            if c:
                val = c.group(1)
            else:
                val = unescape_xml_text(re.sub(r"<.*?>", "", val, flags=re.S).strip())
            if name:
                values[name] = val
        return values

    @staticmethod
    def boolish(value: Optional[str], default: bool = True) -> bool:
        if value is None:
            return default
        v = str(value).strip().lower()
        if v in {"0", "false", "no", "off", "hide", "hidden", "disabled", "none", "null"}:
            return False
        if v in {"1", "true", "yes", "on", "show", "shown", "visible", "enabled"}:
            return True
        return default

    @staticmethod
    def pointerish(text: str) -> bool:
        return bool(re.search(r"pointer|cursor|mouse|laser|spotlight", text or "", re.I))

    @staticmethod
    def numberish(value: Optional[str]) -> Optional[float]:
        if value is None:
            return None
        m = re.search(r"-?\d+(?:\.\d+)?", str(value))
        if not m:
            return None
        try:
            return float(m.group(0))
        except Exception:
            return None

    def parse_pointer_events(
        self,
        body: str,
        time_ms: int,
        method_name: str,
        current_ctid: Optional[int],
        current_page_index: Optional[int],
    ) -> List[PointerEvent]:
        """Best-effort parser for Adobe Connect presenter pointer/cursor shared-object states.

        Adobe Connect versions store the laser/presenter pointer with slightly different
        field names. This parser intentionally accepts common variants such as pointerX,
        cursorY, mouseX/mouseY, visible/showPointer/enabled and nested pointer objects.
        Coordinates may be normalized 0..1, percentages 0..100, or room/pod pixels; the
        renderer maps all three forms onto the currently visible PDF page.
        """
        if not self.pointerish(method_name + " " + body):
            return []

        changes = self.extract_change_values(body)
        lower_changes = {k.lower(): v for k, v in changes.items()}
        visible = True
        for key in (
            "showpointer", "pointervisible", "visible", "visibility", "enabled",
            "pointerenabled", "cursorvisible", "showcursor", "laserpointerenabled",
        ):
            if key in lower_changes:
                visible = self.boolish(lower_changes[key], True)
                break

        page_index = current_page_index
        for key in ("currentpage", "pageindex", "pagenum", "page", "tpgnum"):
            if key in lower_changes:
                n = self.numberish(lower_changes[key])
                if n is not None:
                    page_index = max(0, int(n))
                    break
        raw_memento = lower_changes.get("memento")
        if raw_memento:
            vals = parse_memento(raw_memento)
            if vals:
                page_index = memento_page_index(vals)

        events: List[PointerEvent] = []

        def pick_value(names: Sequence[str], source: Dict[str, str]) -> Tuple[Optional[str], Optional[float]]:
            for n in names:
                if n.lower() in source:
                    value = self.numberish(source[n.lower()])
                    if value is not None:
                        return n, value
            return None, None

        def coord_mode_for(x_name: Optional[str], y_name: Optional[str], x_value: Optional[float], y_value: Optional[float]) -> str:
            names = " ".join(n or "" for n in (x_name, y_name)).lower()
            if "pct" in names or "percent" in names:
                return "percent"
            if x_value is not None and y_value is not None and 0.0 <= x_value <= 1.0 and 0.0 <= y_value <= 1.0:
                return "normalized"
            return "room"

        # First try message-level shared-object changes. These are the most common form.
        x_name, x = pick_value((
            "pointerX", "cursorX", "mouseX", "laserX", "spotlightX",
            "pointerPctX", "cursorPctX", "mousePctX", "pctX", "percentX", "xPercent", "xPct",
            "xPointer", "xCursor", "xMouse", "posX", "positionX", "x",
        ), lower_changes)
        y_name, y = pick_value((
            "pointerY", "cursorY", "mouseY", "laserY", "spotlightY",
            "pointerPctY", "cursorPctY", "mousePctY", "pctY", "percentY", "yPercent", "yPct",
            "yPointer", "yCursor", "yMouse", "posY", "positionY", "y",
        ), lower_changes)
        if x is not None and y is not None:
            mode = coord_mode_for(x_name, y_name, x, y)
            source = f"{method_name or 'shared-object'}:{x_name}/{y_name}"
            events.append(PointerEvent(time_ms, current_ctid, page_index, x, y, visible, source, mode))
        elif not visible:
            # Important: preserve explicit pointer-off events even when no coordinates are attached.
            events.append(PointerEvent(time_ms, current_ctid, page_index, None, None, False, method_name or "shared-object"))

        def first_number_from_tags(block: str, tags: Sequence[str]) -> Tuple[Optional[str], Optional[float]]:
            for tag in tags:
                value = self.numberish(cdata_tag(block, tag))
                if value is not None:
                    return tag, value
            return None, None

        # Then scan nested objects that contain pointer/cursor wording and their own x/y fields.
        for obj_m in re.finditer(r"<Object>(?P<block>.*?)</Object>", body, re.S):
            block = obj_m.group("block")
            if not self.pointerish(block):
                continue
            bx_name, bx = first_number_from_tags(block, (
                "pointerX", "cursorX", "mouseX", "laserX",
                "pointerPctX", "cursorPctX", "mousePctX", "pctX", "percentX", "xPercent", "xPct",
                "x", "posX", "positionX",
            ))
            by_name, by = first_number_from_tags(block, (
                "pointerY", "cursorY", "mouseY", "laserY",
                "pointerPctY", "cursorPctY", "mousePctY", "pctY", "percentY", "yPercent", "yPct",
                "y", "posY", "positionY",
            ))
            bvisible = visible
            for tag in ("visible", "enabled", "show", "showPointer", "pointerVisible", "cursorVisible"):
                raw = cdata_tag(block, tag)
                if raw:
                    bvisible = self.boolish(raw, bvisible)
                    break
            bpage = page_index
            for tag in ("currentPage", "pageIndex", "pageNum", "page", "tPgNum"):
                raw = cdata_tag(block, tag)
                n = self.numberish(raw)
                if n is not None:
                    bpage = max(0, int(n))
                    break
            if bx is not None and by is not None:
                mode = coord_mode_for(bx_name, by_name, bx, by)
                source = f"{method_name or 'nested-object'}:{bx_name}/{by_name}"
                ev = PointerEvent(time_ms, current_ctid, bpage, bx, by, bvisible, source, mode)
                if not events or (events[-1].x, events[-1].y, events[-1].visible) != (ev.x, ev.y, ev.visible):
                    events.append(ev)

        return events

    def extract_change_value(self, body: str, name: str) -> Optional[str]:
        # Object with <name>name</name> then <newValue>...</newValue>.
        pattern = rf"<Object>\s*<code><!\[CDATA\[(?:change|update)\]\]></code>\s*<name><!\[CDATA\[{re.escape(name)}\]\]></name>\s*<newValue>(?P<value>.*?)</newValue>"
        m = re.search(pattern, body, re.S)
        if not m:
            return None
        val = m.group("value").strip()
        c = re.match(r"<!\[CDATA\[(.*?)\]\]>", val, re.S)
        if c:
            return c.group(1)
        # For nested/simple direct text.
        return unescape_xml_text(re.sub(r"<.*?>", "", val, flags=re.S).strip())

    def parse_annotation_shape(self, body: str, time_ms: int, so_name: str) -> Optional[AnnotationShape]:
        # Parse only real shape objects: newValue contains <type>pencil/line/rectangle/etc.</type> and coordinates.
        shape_blocks = re.findall(r"<newValue>\s*(<alpha>.*?</newValue>)", body, flags=re.S)
        if not shape_blocks:
            return None
        block = shape_blocks[0]
        shape_type = cdata_tag(block, "type")
        if not shape_type:
            return None
        try:
            x = float(cdata_tag(block, "x") or 0)
            y = float(cdata_tag(block, "y") or 0)
            w = float(cdata_tag(block, "width") or 0)
            h = float(cdata_tag(block, "height") or 0)
            stroke_col = int(float(cdata_tag(block, "strokeCol") or 0))
            stroke_weight = float(cdata_tag(block, "strokeWeight") or 2)
            alpha = float(cdata_tag(block, "alpha") or 1)
        except Exception:
            return None
        pts: List[Tuple[float, float]] = []
        pts_m = re.search(r"<pts>(.*?)</pts>", block, re.S)
        if pts_m:
            for obj in re.findall(r"<Object>(.*?)</Object>", pts_m.group(1), re.S):
                try:
                    px = float(cdata_tag(obj, "x") or 0)
                    py = float(cdata_tag(obj, "y") or 0)
                    pts.append((px, py))
                except Exception:
                    continue
        html_text = re.sub(r"<.*?>", "", cdata_tag(block, "htmlText") or "", flags=re.S).strip()
        page_index = None
        m = re.search(r"set_WB_So_(\d+)", so_name)
        if m:
            # In Adobe Connect this suffix often corresponds to whiteboard/page SO id; it is close enough
            # for page-specific filtering and can be adjusted for site variants.
            page_index = max(0, int(m.group(1)))
        return AnnotationShape(time_ms, page_index, shape_type, x, y, w, h, stroke_col, stroke_weight, alpha, pts, html_text)

    def parse_user_names(self, xml_files: List[Path]) -> Dict[int, str]:
        """Extract Adobe Connect participant names so chat sender IDs can be resolved."""
        users: Dict[int, str] = {}
        for p in xml_files:
            if p.name.lower() not in ("indexstream.xml", "mainstream.xml"):
                continue
            txt = read_text(p)
            for users_m in re.finditer(r"<users>(?P<body>.*?)</users>", txt, re.S):
                users_body = users_m.group("body")
                for obj_m in re.finditer(r"<Object>(?P<block>.*?)</Object>", users_body, re.S):
                    block = obj_m.group("block")
                    uid_raw = cdata_tag(block, "id")
                    try:
                        uid = int(float(uid_raw))
                    except Exception:
                        continue
                    name = (
                        cdata_tag(block, "fullName")
                        or cdata_tag(block, "name")
                        or cdata_tag(block, "originalName")
                    )
                    name = self.clean_chat_text(name)
                    if name:
                        users[uid] = name
        return users

    def parse_chat_messages(self, xml_files: List[Path]) -> List[ChatMessage]:
        """
        Parse participant chat from ftchat*.xml.

        Adobe Connect chat streams vary between versions. Common payloads include a
        recording-relative <Message time="..."> and an inner absolute/epoch-like <when>.
        We prefer Message time when present; otherwise large <when> values are normalized
        relative to the first chat message so they can still be rendered on the video timeline.
        """
        raw_messages: List[Tuple[int, int, int, str, str, str]] = []
        for p in xml_files:
            txt = read_text(p)
            for msg_m in re.finditer(r'<Message\s+time="(?P<t>\d+)"[^>]*>(?P<body>.*?)</Message>', txt, re.S):
                msg_time_ms = int(msg_m.group("t"))
                body = msg_m.group("body")
                for block in self.extract_chat_blocks(body):
                    text = self.clean_chat_text(cdata_tag(block, "text"))
                    if not text:
                        continue
                    sender = self.extract_chat_sender(block, body)
                    raw_when = cdata_tag(block, "when") or cdata_tag(body, "when")
                    when_ms = 0
                    if raw_when:
                        try:
                            when_ms = int(float(raw_when))
                        except Exception:
                            when_ms = 0

                    chosen_time = msg_time_ms if msg_time_ms > 0 else 0
                    if chosen_time <= 0 and when_ms > 0:
                        chosen_time = when_ms
                    raw_messages.append((chosen_time, msg_time_ms, when_ms, sender, text, raw_when))

        if not raw_messages:
            return []

        # If <when> is absolute/epoch-like, normalize it to the recording timeline.
        # Mixed streams can contain both relative Message time and absolute <when>; in that
        # case, align the first absolute timestamp to the first relative timestamp.
        duration_ms = int(max(1.0, self.duration_s) * 1000)
        def is_large_time(value: int) -> bool:
            return value > max(duration_ms + 60000, 100000000000)

        usable_times = [m[0] for m in raw_messages if m[0] > 0]
        large_times = [t for t in usable_times if is_large_time(t)]
        relative_times = [t for t in usable_times if not is_large_time(t)]
        # Best alignment: a message that has both relative Message time and absolute <when>.
        alignment_offsets = [
            when_ms - msg_time_ms
            for _, msg_time_ms, when_ms, _, _, _ in raw_messages
            if msg_time_ms > 0 and when_ms > 0 and is_large_time(when_ms)
        ]
        large_base_ms = 0
        if alignment_offsets:
            large_base_ms = min(alignment_offsets)
        elif large_times:
            if relative_times:
                large_base_ms = min(large_times) - min(relative_times)
            else:
                large_base_ms = min(large_times)

        out: List[ChatMessage] = []
        seen = set()
        for time_value, _msg_time_ms, _when_ms, sender, text, raw_when in raw_messages:
            if time_value <= 0:
                time_ms = 0
            elif is_large_time(time_value):
                time_ms = max(0, time_value - large_base_ms)
            else:
                time_ms = time_value
            key = (time_ms, sender, text)
            if key in seen:
                continue
            seen.add(key)
            out.append(ChatMessage(time_ms=time_ms, sender=sender or "Participant", text=text, raw_when=raw_when))
        return sorted(out, key=lambda m: m.time_ms)

    def extract_chat_blocks(self, body: str) -> List[str]:
        blocks: List[str] = []
        for obj_m in re.finditer(r"<Object>(?P<block>.*?)</Object>", body, re.S):
            block = obj_m.group("block")
            if "<text>" in block:
                blocks.append(block)
        for val_m in re.finditer(r"<newValue>\s*(?P<block>.*?)</newValue>", body, re.S):
            block = val_m.group("block")
            if "<text>" in block and block not in blocks:
                blocks.append(block)
        if "<text>" in body and not blocks:
            blocks.append(body)
        return blocks

    def extract_chat_sender(self, block: str, full_body: str) -> str:
        for tag in (
            "senderDisplayName", "senderName", "displayName", "fullName",
            "sender", "from", "userName", "username", "login", "name",
        ):
            value = self.clean_chat_text(cdata_tag(block, tag))
            if value and value.lower() not in {"chat", "message"}:
                return value

        for tag in ("senderID", "senderId", "userID", "userId", "fromID", "fromId", "publisherID", "uID"):
            raw = cdata_tag(block, tag) or cdata_tag(full_body, tag)
            try:
                uid = int(float(raw))
            except Exception:
                continue
            if uid in self.user_names:
                return self.user_names[uid]
            return f"User {uid}"
        return "Participant"

    @staticmethod
    def clean_chat_text(value: str) -> str:
        if not value:
            return ""
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        value = re.sub(r"<\s*br\s*/?\s*>", "\n", value, flags=re.I)
        value = re.sub(r"<.*?>", "", value, flags=re.S)
        value = unescape_xml_text(value)
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    def parse_stream_start_times(self, xml_files: List[Path]) -> Dict[str, int]:
        """Parse canonical Adobe Connect streamAdded/startTime values.

        The ZIP can contain files in a lexical order that does not match the
        class timeline.  Also, some cameraVoip XML pacingTick offsets are a few
        seconds away from the actual StreamManager startTime.  This mapping is
        therefore the primary source of truth for both voice and screen-share
        placement.  Keys are file stems such as cameraVoip_20_28 or
        screenshare_3_8.
        """
        starts: Dict[str, int] = {}
        for p in xml_files:
            if p.name.lower() not in ("indexstream.xml", "mainstream.xml"):
                continue
            txt = read_text(p)
            for msg_m in re.finditer(r'<Message\s+time="\d+"[^>]*>(?P<body>.*?)</Message>', txt, re.S):
                body = msg_m.group("body")
                if "streamAdded" not in body or "streamName" not in body:
                    continue
                for obj_m in re.finditer(r"<Object>(?P<block>.*?)</Object>", body, re.S):
                    block = obj_m.group("block")
                    if "<streamName>" not in block or "<startTime>" not in block:
                        continue
                    stream_name = cdata_tag(block, "streamName").strip().lstrip("/")
                    stream_type = cdata_tag(block, "streamType").strip()
                    start_raw = cdata_tag(block, "startTime")
                    if not stream_name or not start_raw:
                        continue
                    # Keep only media streams that correspond to files in the package.
                    if stream_type and stream_type not in {"cameraVoip", "screenshare"}:
                        continue
                    try:
                        start_ms = int(float(start_raw))
                    except Exception:
                        continue
                    # If duplicates appear, the earliest streamAdded is the safe timeline anchor.
                    if stream_name not in starts or start_ms < starts[stream_name]:
                        starts[stream_name] = max(0, start_ms)
        return starts

    @staticmethod
    def is_camera_voip_file(path: Path) -> bool:
        return bool(re.match(r"cameraVoip_\d+_\d+\.flv$", path.name, re.I))

    @staticmethod
    def is_screenshare_file(path: Path) -> bool:
        # Adobe Connect stores screen sharing as screenshare_<podId>_<streamId>.flv.
        # Keeping this explicit prevents data streams, webcam videos, and other FLVs
        # from being composited in the screen-share panel by mistake.
        return bool(re.match(r"screenshare_\d+_\d+\.flv$", path.name, re.I))

    def discover_audio(self, flv_files: List[Path]) -> List[AudioSegment]:
        segments: List[AudioSegment] = []
        for f in flv_files:
            # Voice should come from cameraVoip chunks. Some recordings contain other
            # audio-capable FLVs; mixing those here can duplicate or misplace sound.
            if not self.is_camera_voip_file(f):
                continue
            xml = paired_xml_for(f)
            start_ms = self.stream_start_times.get(f.stem, parse_first_pacing_start_ms(xml))
            dur = duration_from_xml_metadata(xml)
            if dur <= 0:
                dur = duration_from_ffprobe(f)
            if dur <= 0:
                continue
            segments.append(AudioSegment(f, xml, start_ms, dur))
        return sorted(segments, key=lambda s: (s.start_ms, s.flv_path.name))

    def discover_video(self, flv_files: List[Path]) -> List[VideoSegment]:
        segments: List[VideoSegment] = []
        for f in flv_files:
            if not self.is_screenshare_file(f):
                continue
            xml = paired_xml_for(f)
            start_ms = self.stream_start_times.get(f.stem, parse_first_pacing_start_ms(xml))
            dur = duration_from_xml_metadata(xml)
            if dur <= 0:
                dur = duration_from_ffprobe(f)
            if dur <= 0:
                continue
            segments.append(VideoSegment(f, xml, start_ms, dur, 0, 0))
        return sorted(segments, key=lambda s: (s.start_ms, s.flv_path.name))

    # ------------------------- Asset download + PDF page rendering -------------------------

    def prepare_documents(self) -> None:
        step("دانلود PDF/source فایل‌های معرفی‌شده در XML")
        # First, attach local files already included in the zip.
        existing_by_name: Dict[str, Path] = {p.name: p for p in self.extract_dir.rglob("*") if p.is_file()}
        for d in self.documents.values():
            if d.name in existing_by_name:
                d.local_path = existing_by_name[d.name]

        if self.skip_download_assets:
            warn("دانلود assetها با --skip-download-assets غیرفعال شده است.")
        else:
            for d in sorted(self.documents.values(), key=lambda x: x.ct_id):
                if d.local_path and d.local_path.exists():
                    continue
                if not d.name.lower().endswith((".pdf", ".ppt", ".pptx")):
                    continue
                try:
                    self.download_document_asset(d)
                except Exception as e:
                    warn(f"دانلود {d.name} ناموفق بود: {e}")

        step("تبدیل PDFها به تصویر صفحه‌ها")
        for d in sorted(self.documents.values(), key=lambda x: x.ct_id):
            if d.local_path and d.local_path.suffix.lower() == ".pdf" and d.local_path.exists():
                d.page_images = self.render_pdf_pages(d.local_path, d.ct_id)
                info(f"{d.name}: {len(d.page_images)} pages rendered")
            else:
                # Placeholder for PPTX or missing PDF. For PPTX, Adobe often provides converted SWF/assets
                # that require additional proprietary handling; keeping a visual placeholder is better than failing.
                d.page_images = [self.make_placeholder_page(d.name, d.ct_id)]
                if d.local_path:
                    warn(f"{d.name}: فرمت PDF نیست؛ placeholder ساخته شد.")
                else:
                    warn(f"{d.name}: فایل محلی پیدا نشد؛ placeholder ساخته شد.")

    def default_pdf_output_dir(self) -> Path:
        """
        Folder where downloaded/found PDF assets are copied for the user.
        If --pdf-output-dir is not passed, PDFs are placed next to the MP4 in:
          <output_stem>_pdfs/
        """
        if self.pdf_output_dir:
            return self.pdf_output_dir
        out = self.output
        return out.with_name(f"{out.stem}_pdfs")

    def unique_copy_destination(self, target_dir: Path, preferred_name: str, ct_id: int) -> Path:
        safe_name = safe_filename(preferred_name)
        if not safe_name.lower().endswith(".pdf"):
            safe_name += ".pdf"
        dest = target_dir / safe_name
        if not dest.exists():
            return dest

        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix or ".pdf"
        dest = target_dir / f"{stem}_ct{ct_id}{suffix}"
        if not dest.exists():
            return dest

        n = 2
        while True:
            candidate = target_dir / f"{stem}_ct{ct_id}_{n}{suffix}"
            if not candidate.exists():
                return candidate
            n += 1

    def export_pdf_assets(self) -> List[Path]:
        """
        Copy all PDF assets that were downloaded or found inside the recording ZIP
        into a stable output folder so they are not lost when the temporary workdir is cleaned.
        """
        step("کپی PDFهای دانلود/پیداشده کنار خروجی")
        target_dir = self.default_pdf_output_dir()
        copied: List[Path] = []
        seen_sources: set[Path] = set()

        for d in sorted(self.documents.values(), key=lambda x: x.ct_id):
            if not d.local_path or not d.local_path.exists():
                continue
            if d.local_path.suffix.lower() != ".pdf" and not d.name.lower().endswith(".pdf"):
                continue

            source = d.local_path.resolve()
            if source in seen_sources:
                continue
            seen_sources.add(source)

            target_dir.mkdir(parents=True, exist_ok=True)
            dest = self.unique_copy_destination(target_dir, d.name or source.name, d.ct_id)
            try:
                shutil.copy2(source, dest)
                copied.append(dest)
                info(f"PDF copied: {dest.resolve()}")
            except Exception as e:
                warn(f"کپی PDF ناموفق بود ({source.name}): {e}")

        if copied:
            info(f"PDF output folder: {target_dir.resolve()}")
        else:
            warn("هیچ PDF قابل‌کپی پیدا نشد.")
        return copied

    def download_document_asset(self, d: DocumentAsset) -> None:
        if requests is None or self.session is None:
            return
        urls = self.candidate_asset_urls(d)
        if not urls:
            return
        dest = self.assets_dir / safe_filename(d.name)
        for url in urls:
            info(f"Trying asset URL for {d.name}: {url}")
            r = self.session.get(url, allow_redirects=True, timeout=90)
            if r.status_code >= 400 or not r.content:
                continue
            # Avoid saving HTML login pages as PDFs.
            head = r.content[:512].lower()
            if b"<html" in head and d.name.lower().endswith(".pdf"):
                continue
            dest.write_bytes(r.content)
            d.local_path = dest
            info(f"Downloaded: {dest} ({dest.stat().st_size/1024:.1f} KB)")
            return
        warn(f"هیچ URLی برای {d.name} فایل معتبر برنگرداند.")

    def candidate_asset_urls(self, d: DocumentAsset) -> List[str]:
        urls: List[str] = []
        filename = d.name
        # Preferred: parse /system/download?download-url=/_a7/<sco>/source/&name=file.pdf
        # and convert it to:
        #   {base_url}/<sco>/source/file.pdf
        # Example:
        #   /system/download?download-url=/_a7/p6ihitv5g8pm/source/&name=0-Intro.pdf
        # becomes:
        #   {base_url}/p6ihitv5g8pm/source/0-Intro.pdf
        if d.download_url:
            full = urljoin(self.origin, d.download_url)
            parsed = urlparse(full)
            qs = parse_qs(parsed.query)
            dl = (qs.get("download-url") or [""])[0]
            nm = (qs.get("name") or [filename])[0]
            if dl:
                # Build a direct source URL and remove Adobe's internal /_a7/ prefix.
                source_path = unquote(dl).strip().strip("/")
                if source_path.startswith("_a7/"):
                    source_path = source_path[len("_a7/"):]
                asset_name = unquote(nm).strip().strip("/")
                direct_path = "/".join(part for part in (source_path.rstrip("/"), asset_name) if part)
                urls.append(f"{self.origin}/{quote(direct_path, safe='/')}")
            urls.append(full)
        # Fallback from contentOutputPath: /_a7/<sco>/default/ -> /_a7/<sco>/source/file.pdf
        if d.content_output_path:
            source_path = re.sub(r"/default/?$", "/source/", d.content_output_path)
            urls.append(urljoin(self.origin, source_path.rstrip("/") + "/" + quote(unquote(filename))))
        # User-provided pattern style: base/sco_path/source/file_name, accounting for /output/.
        if d.sco_path:
            sp = d.sco_path.strip("/")
            sp_source = re.sub(r"/output/?$", "/source", sp)
            urls.append(f"{self.origin}/{sp_source}/{quote(unquote(filename))}")
        # Remove duplicates while preserving order.
        seen = set()
        out = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def render_pdf_pages(self, pdf_path: Path, ct_id: int) -> List[Path]:
        if fitz is None:
            warn("PyMuPDF نصب نیست؛ برای رندر واقعی PDF اجرا کن: pip install pymupdf")
            return [self.make_placeholder_page(pdf_path.name, ct_id)]
        out_dir = self.pages_dir / f"ct_{ct_id}"
        out_dir.mkdir(parents=True, exist_ok=True)
        pages: List[Path] = []
        doc = fitz.open(str(pdf_path))
        # Render PDF pages close to the requested output size. The old fixed 2x zoom
        # was wasteful for 480p/720p renders because every frame had to decode and
        # downscale oversized PNGs.
        target_page_width = max(480, min(1200, int(self.room_w * 1.08)))
        for i, page in enumerate(doc):
            zoom = max(0.75, min(2.0, target_page_width / max(1.0, float(page.rect.width))))
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            p = out_dir / f"page_{i+1:04d}.png"
            pix.save(str(p))
            pages.append(p)
        doc.close()
        return pages

    def font(self, size: int):
        """Return a cached PIL font object."""
        size = max(1, int(size))
        if size not in self._font_cache:
            if self._font_path:
                self._font_cache[size] = ImageFont.truetype(self._font_path, size)
            else:
                self._font_cache[size] = ImageFont.load_default()
        return self._font_cache[size]

    def load_page_image(self, path: Path):
        """Load PDF page image once and reuse it across frames."""
        key = str(path)
        img = self._page_image_cache.get(key)
        if img is None:
            img = Image.open(path).convert("RGB")
            if len(self._page_image_cache) > 32:
                self._page_image_cache.clear()
            self._page_image_cache[key] = img
        return img

    def resized_page_image(self, path: Path, width: int, height: int):
        """Resize page once per target rectangle size.

        Layout transitions create a few different sizes, but most frames reuse the
        exact same body rectangle. Caching here removes thousands of expensive
        Lanczos resizes in long recordings.
        """
        key = (str(path), int(width), int(height))
        img = self._resized_page_cache.get(key)
        if img is None:
            original = self.load_page_image(path)
            img = original.resize((int(width), int(height)), Image.Resampling.LANCZOS)
            if len(self._resized_page_cache) > 64:
                self._resized_page_cache.clear()
            self._resized_page_cache[key] = img
        return img

    def make_placeholder_page(self, title: str, ct_id: int) -> Path:
        if Image is None:
            die("ماژول Pillow نصب نیست. اجرا کن: pip install pillow")
        out_dir = self.pages_dir / f"ct_{ct_id}"
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / "placeholder.png"
        img = Image.new("RGB", (1600, 1000), (248, 250, 252))
        draw = ImageDraw.Draw(img)
        font_big = self.font(64)
        font_small = self.font(34)
        draw.rounded_rectangle((80, 80, 1520, 920), radius=36, fill=(255, 255, 255), outline=(210, 220, 230), width=3)
        draw.text((130, 150), "Adobe Connect content", fill=(20, 24, 32), font=font_big)
        draw.text((130, 260), title, fill=(60, 70, 85), font=font_small)
        draw.text((130, 340), "Source file was not available or not renderable as PDF.", fill=(100, 110, 125), font=font_small)
        img.save(p)
        return p

    # ------------------------- Audio/video generation -------------------------

    def export_audio_timeline_report(self) -> Path:
        """Write a simple CSV showing the corrected order of all voice chunks."""
        report = self.workdir / "audio_timeline_corrected.csv"
        with report.open("w", encoding="utf-8") as f:
            f.write("order,file,start_seconds,duration_seconds,source_xml\n")
            for idx, seg in enumerate(sorted(self.audio_segments, key=lambda s: (s.start_ms, s.flv_path.name)), start=1):
                f.write(
                    f"{idx},{seg.flv_path.name},{seg.start_ms/1000:.3f},{seg.duration_s:.3f},"
                    f"{seg.xml_path.name if seg.xml_path else ''}\n"
                )
        info(f"Audio timeline report: {report}")
        return report

    def build_audio_track(self) -> Path:
        step("ساخت ترک صوتی align شده با timeline")
        audio_out = self.workdir / "timeline_audio.m4a"
        if not self.audio_segments:
            warn("هیچ stream صوتی پیدا نشد؛ ترک سکوت ساخته می‌شود.")
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=44100",
                "-t", f"{self.duration_s:.3f}",
                "-c:a", "aac", "-b:a", self.audio_bitrate,
                str(audio_out),
            ]
            run(cmd)
            return audio_out

        # Fast and exact mixer:
        #   1) Use canonical streamAdded/startTime for each cameraVoip chunk.
        #   2) Decode each FLV with timestamp-aware resampling so local FLV gaps stay in place.
        #   3) Add samples into a disk-backed int32 timeline at the corrected start sample.
        # This avoids ffmpeg's very slow adelay+amix over many two-hour silent streams,
        # while keeping the voice positions exact.
        try:
            import numpy as np  # type: ignore
            import wave
        except Exception:
            die("برای mixer سریع صوتی، numpy لازم است. اجرا کن: pip install numpy")

        sample_rate = 44100
        total_samples = max(1, int(math.ceil(self.duration_s * sample_rate)))
        mix_raw = self.workdir / "timeline_audio_mix_s32le.raw"
        wav_path = self.workdir / "timeline_audio_mix.wav"

        # Create/zero the disk-backed mix buffer.
        mix = np.memmap(str(mix_raw), dtype=np.int32, mode="w+", shape=(total_samples,))
        mix[:] = 0
        mix.flush()

        ordered = sorted(self.audio_segments, key=lambda x: (x.start_ms, x.flv_path.name))
        chunk_bytes = sample_rate * 2 * 20  # 20 seconds of s16le mono
        for idx, seg in enumerate(ordered, start=1):
            start_sample = max(0, int(round(seg.start_ms * sample_rate / 1000.0)))
            if start_sample >= total_samples:
                warn(f"audio {seg.flv_path.name} بیرون از بازه کلاس است و رد شد.")
                continue
            info(f"mix audio {idx}/{len(ordered)}: {seg.flv_path.name} @ {seg.start_ms/1000:.3f}s")
            cmd = [
                "ffmpeg", "-v", "error", "-i", str(seg.flv_path),
                "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(sample_rate),
                "-af", "aresample=44100:async=1000:first_pts=0",
                "-f", "s16le", "pipe:1",
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            pos = start_sample
            assert proc.stdout is not None
            try:
                while True:
                    data = proc.stdout.read(chunk_bytes)
                    if not data:
                        break
                    if len(data) % 2:
                        data = data[:-1]
                    if not data:
                        continue
                    arr = np.frombuffer(data, dtype="<i2").astype(np.int32)
                    if pos >= total_samples:
                        break
                    n = min(int(arr.size), total_samples - pos)
                    if n > 0:
                        mix[pos:pos + n] = mix[pos:pos + n] + arr[:n]
                        pos += n
            finally:
                stderr = proc.stderr.read().decode("utf-8", errors="ignore") if proc.stderr else ""
                rc = proc.wait()
            if rc != 0:
                warn(f"decode صوتی {seg.flv_path.name} خطا داد: {stderr[:300]}")
        mix.flush()

        # Normalize only if overlapping microphones exceed int16 headroom.
        peak = 1
        scan_block = sample_rate * 120
        for pos in range(0, total_samples, scan_block):
            block = mix[pos: min(total_samples, pos + scan_block)]
            if block.size:
                peak = max(peak, int(np.max(np.abs(block))))
        gain = min(1.0, 30000.0 / float(peak))
        if gain < 1.0:
            info(f"Audio peak={peak}; applying safe gain {gain:.4f}")

        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            write_block = sample_rate * 60
            for pos in range(0, total_samples, write_block):
                block = mix[pos: min(total_samples, pos + write_block)]
                if gain < 1.0:
                    out_block = np.clip(block.astype(np.float64) * gain, -32768, 32767).astype("<i2")
                else:
                    out_block = np.clip(block, -32768, 32767).astype("<i2")
                wf.writeframes(out_block.tobytes())

        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(wav_path),
            "-c:a", "aac", "-b:a", self.audio_bitrate,
            str(audio_out),
        ]
        run(cmd)

        # Keep the final AAC, remove huge intermediates even when --keep is enabled.
        try:
            del mix
            mix_raw.unlink(missing_ok=True)
            wav_path.unlink(missing_ok=True)
        except Exception:
            pass
        return audio_out

    def render_window(self) -> Tuple[int, int, int, float, float]:
        total_frames = max(1, int(math.ceil(self.duration_s * self.fps)))
        start_s = min(max(0.0, self.clip_start_s), max(0.0, self.duration_s))
        max_duration = max(0.001, self.duration_s - start_s)
        duration_s = min(self.clip_duration_s, max_duration) if self.clip_duration_s > 0 else max_duration
        start_frame = min(total_frames - 1, max(0, int(math.floor(start_s * self.fps))))
        end_frame = min(total_frames, max(start_frame + 1, int(math.ceil((start_s + duration_s) * self.fps))))
        actual_start = start_frame / self.fps
        actual_duration = max(0.001, (end_frame - start_frame) / self.fps)
        return start_frame, end_frame, total_frames, actual_start, actual_duration

    def available_video_encoders(self) -> Set[str]:
        try:
            cp = run_capture(["ffmpeg", "-hide_banner", "-encoders"], check=False)
            return set(re.findall(r"\b(h264_[A-Za-z0-9_]+|libx264)\b", cp.stdout or ""))
        except Exception:
            return {"libx264"}

    def hardware_encoder_works(self, encoder: str) -> bool:
        """Return True only if ffmpeg can actually open the requested hardware encoder.

        Some ffmpeg builds list h264_nvenc even when the machine has no NVIDIA GPU
        or the driver is not ready. A tiny one-frame test prevents a long render
        from failing at the first encoding step.
        """
        if encoder in self._encoder_runtime_cache:
            return bool(self._encoder_runtime_cache[encoder])
        test_out = self.workdir / f"encoder_test_{encoder}.mp4"
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
            "-frames:v", "1", "-c:v", encoder,
            "-pix_fmt", "yuv420p", str(test_out),
        ]
        try:
            cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=12)
            ok = cp.returncode == 0 and test_out.exists() and test_out.stat().st_size > 0
        except Exception:
            ok = False
        try:
            test_out.unlink(missing_ok=True)
        except Exception:
            pass
        self._encoder_runtime_cache[encoder] = ok
        return ok

    def video_encoder_args(self) -> List[str]:
        requested = (os.environ.get("AC_ENCODER", "") or self.encoder_preference or "auto").lower()
        aliases = {
            "cpu": "libx264",
            "libx264": "libx264",
            "nvidia": "h264_nvenc",
            "nvenc": "h264_nvenc",
            "gpu": "h264_nvenc",
            "intel": "h264_qsv",
            "qsv": "h264_qsv",
            "apple": "h264_videotoolbox",
            "videotoolbox": "h264_videotoolbox",
        }
        available = self.available_video_encoders()
        encoder = aliases.get(requested, "libx264")
        if requested == "auto":
            encoder = "libx264"  # Safe default for every laptop; GPU is explicit.
        if encoder not in available:
            warn(f"Encoder {encoder} در ffmpeg موجود نیست؛ fallback به libx264 انجام شد.")
            encoder = "libx264"
        if encoder != "libx264" and not self.hardware_encoder_works(encoder):
            warn(f"Encoder سخت‌افزاری {encoder} روی این سیستم قابل اجرا نیست؛ fallback به CPU/libx264 انجام شد.")
            encoder = "libx264"
        if encoder == "libx264":
            return ["-c:v", "libx264", "-preset", self.ffmpeg_preset, "-tune", "fastdecode", "-crf", str(self.video_crf)]
        if encoder == "h264_nvenc":
            # GTX 1050+ usually supports NVENC. The "fast" preset is broadly compatible
            # across older/newer ffmpeg builds and avoids long CPU-bound x264 encoding.
            return ["-c:v", encoder, "-preset", "fast", "-rc", "vbr", "-cq", str(self.video_crf), "-b:v", "0"]
        if encoder == "h264_qsv":
            return ["-c:v", encoder, "-global_quality", str(self.video_crf)]
        if encoder == "h264_videotoolbox":
            return ["-c:v", encoder, "-b:v", "2500k"]
        return ["-c:v", "libx264", "-preset", self.ffmpeg_preset, "-crf", str(self.video_crf)]

    def render_base_video(self) -> Path:
        step("رندر ویدیوی گرافیکی کلاس از روی XML/PDF/annotation")
        if Image is None:
            die("ماژول Pillow نصب نیست. اجرا کن: pip install pillow")
        video_out = self.workdir / "base_scene.mp4"
        start_frame, end_frame, total_frames, window_start, window_duration = self.render_window()
        info(f"Rendering {end_frame - start_frame} frames at {self.fps:g} fps from {self.format_time(window_start)} for {self.format_time(window_duration)}")

        # Optional live preview for the local web UI. While ffmpeg is still
        # encoding the final MP4, we periodically publish the latest composed
        # frame as latest.jpg. The browser polls this image, so the user can
        # visually follow the class instead of staring at a loader for minutes.
        preview_dir_env = os.environ.get("AC_LIVE_PREVIEW_DIR", "").strip()
        preview_dir = Path(preview_dir_env) if preview_dir_env else None
        preview_every = float(os.environ.get("AC_LIVE_PREVIEW_EVERY", "0.75") or "0.75")
        next_preview_t = -1.0
        if preview_dir:
            preview_dir.mkdir(parents=True, exist_ok=True)
            info(f"Live preview enabled: {preview_dir}")

        def publish_preview(frame_img, frame_idx: int, frame_time: float, pct: int) -> None:
            if not preview_dir:
                return
            try:
                preview_img = frame_img
                # Keep preview light so saving JPEG does not slow down render noticeably.
                max_w = 960
                if preview_img.width > max_w:
                    ratio = max_w / float(preview_img.width)
                    preview_img = preview_img.resize((max_w, max(1, int(preview_img.height * ratio))), Image.Resampling.BILINEAR)
                tmp_jpg = preview_dir / "latest.tmp.jpg"
                final_jpg = preview_dir / "latest.jpg"
                preview_img.convert("RGB").save(tmp_jpg, "JPEG", quality=78, optimize=False)
                tmp_jpg.replace(final_jpg)

                state = {
                    "frame": frame_idx,
                    "total_frames": total_frames,
                    "time": round(frame_time, 3),
                    "duration": round(self.duration_s, 3),
                    "progress": pct,
                    "updated_at": time.time(),
                }
                tmp_json = preview_dir / "state.tmp.json"
                final_json = preview_dir / "state.json"
                tmp_json.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
                tmp_json.replace(final_json)
            except Exception as exc:
                warn(f"ذخیره پیش‌نمایش زنده ناموفق بود: {exc}")

        cmd = [
            "ffmpeg", "-y",
            "-f", "rawvideo",
            "-pixel_format", "rgb24",
            "-video_size", f"{self.room_w}x{self.room_h}",
            "-framerate", str(self.fps),
            "-i", "-",
            "-an",
            *self.video_encoder_args(),
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(video_out),
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        try:
            last_pct = -1
            for frame_idx in range(start_frame, end_frame):
                t = frame_idx / self.fps
                img = self.compose_frame(t)
                if img.size != (self.room_w, self.room_h):
                    img = img.resize((self.room_w, self.room_h), Image.Resampling.LANCZOS)
                proc.stdin.write(img.convert("RGB").tobytes())  # type: ignore[union-attr]
                pct = int(((frame_idx - start_frame) * 100) / max(1, end_frame - start_frame))
                if preview_dir and (next_preview_t < 0 or t >= next_preview_t or frame_idx == total_frames - 1):
                    publish_preview(img, frame_idx, t, pct)
                    next_preview_t = t + max(0.25, preview_every)
                if pct >= last_pct + 10:
                    last_pct = pct
                    info(f"render progress: {pct}%")
        finally:
            if proc.stdin:
                proc.stdin.close()
            rc = proc.wait()
            if rc != 0:
                die("ffmpeg هنگام ساخت base_scene.mp4 خطا داد.")
        info(f"Video scene saved: {video_out}")
        return video_out

    def _segment_output_dir(self) -> Path:
        env_dir = os.environ.get("AC_SEGMENTS_DIR", "").strip()
        out_dir = Path(env_dir) if env_dir else (self.workdir / "rendered_segments")
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir

    def _write_segments_manifest(self, segments_dir: Path, segments: List[dict], *, final_ready: bool = False) -> None:
        """Publish the list of ready chunks for the Flask UI."""
        try:
            data = {
                "segment_seconds": self.segment_seconds,
                "duration": round(self.duration_s, 3),
                "final_ready": bool(final_ready),
                "segments": segments,
                "updated_at": time.time(),
            }
            tmp = segments_dir / "manifest.tmp.json"
            final = segments_dir / "manifest.json"
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(final)
        except Exception as exc:
            warn(f"نوشتن manifest بخش‌ها ناموفق بود: {exc}")

    def render_base_video_chunk(self, chunk_index: int, start_frame: int, end_frame: int) -> Path:
        """Render one base-scene MP4 chunk from the XML/PDF timeline."""
        if Image is None:
            die("ماژول Pillow نصب نیست. اجرا کن: pip install pillow")
        chunk_start = start_frame / self.fps
        chunk_duration = max(0.001, (end_frame - start_frame) / self.fps)
        video_out = self.workdir / f"base_scene_segment_{chunk_index:04d}.mp4"
        info(f"Rendering chunk {chunk_index}: {self.format_time(chunk_start)} تا {self.format_time(chunk_start + chunk_duration)}")

        preview_dir_env = os.environ.get("AC_LIVE_PREVIEW_DIR", "").strip()
        preview_dir = Path(preview_dir_env) if preview_dir_env else None
        preview_every = float(os.environ.get("AC_LIVE_PREVIEW_EVERY", "0.75") or "0.75")
        next_preview_t = -1.0
        total_frames = max(1, int(math.ceil(self.duration_s * self.fps)))
        if preview_dir:
            preview_dir.mkdir(parents=True, exist_ok=True)

        def publish_preview(frame_img, frame_idx: int, frame_time: float, pct: int) -> None:
            if not preview_dir:
                return
            try:
                preview_img = frame_img
                max_w = 960
                if preview_img.width > max_w:
                    ratio = max_w / float(preview_img.width)
                    preview_img = preview_img.resize((max_w, max(1, int(preview_img.height * ratio))), Image.Resampling.BILINEAR)
                tmp_jpg = preview_dir / "latest.tmp.jpg"
                final_jpg = preview_dir / "latest.jpg"
                preview_img.convert("RGB").save(tmp_jpg, "JPEG", quality=78, optimize=False)
                tmp_jpg.replace(final_jpg)
                state = {
                    "frame": frame_idx,
                    "total_frames": total_frames,
                    "time": round(frame_time, 3),
                    "duration": round(self.duration_s, 3),
                    "progress": pct,
                    "updated_at": time.time(),
                }
                tmp_json = preview_dir / "state.tmp.json"
                final_json = preview_dir / "state.json"
                tmp_json.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
                tmp_json.replace(final_json)
            except Exception as exc:
                warn(f"ذخیره پیش‌نمایش زنده ناموفق بود: {exc}")

        cmd = [
            "ffmpeg", "-y",
            "-f", "rawvideo",
            "-pixel_format", "rgb24",
            "-video_size", f"{self.room_w}x{self.room_h}",
            "-framerate", str(self.fps),
            "-i", "-",
            "-an",
            *self.video_encoder_args(),
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(video_out),
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        try:
            for frame_idx in range(start_frame, end_frame):
                t = frame_idx / self.fps
                img = self.compose_frame(t)
                if img.size != (self.room_w, self.room_h):
                    img = img.resize((self.room_w, self.room_h), Image.Resampling.LANCZOS)
                proc.stdin.write(img.convert("RGB").tobytes())  # type: ignore[union-attr]
                pct = int(frame_idx * 100 / total_frames)
                if preview_dir and (next_preview_t < 0 or t >= next_preview_t or frame_idx >= end_frame - 1):
                    publish_preview(img, frame_idx, t, pct)
                    next_preview_t = t + max(0.25, preview_every)
        finally:
            if proc.stdin:
                proc.stdin.close()
            rc = proc.wait()
            if rc != 0:
                die(f"ffmpeg هنگام ساخت chunk {chunk_index} خطا داد.")
        return video_out

    def make_screenshare_clip_for_segment(
        self,
        source: VideoSegment,
        input_seek: float,
        duration: float,
        chunk_index: int,
        clip_index: int,
    ) -> Path:
        """Create an accurate per-chunk screen-share clip with timestamps reset to zero.

        Adobe Connect screen-share FLVs can be long and can cross the 10-minute
        output chunk boundary.  Seeking directly into those FLVs with `ffmpeg
        -ss ... -i screenshare.flv` is fast, but on some recordings it lands on
        bad FLV timestamps/keyframes and the second rendered chunk gets an empty
        or black screen-share overlay.

        Re-encoding a tiny per-chunk MP4 via the trim filter is slower but much
        safer: ffmpeg decodes the source stream in timestamp order, cuts the exact
        range needed for this output chunk, and resets PTS before the overlay step.
        """
        safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", source.flv_path.stem)
        clip_dir = self.workdir / "screenshare_segment_clips"
        clip_dir.mkdir(parents=True, exist_ok=True)
        clip_path = clip_dir / f"chunk_{chunk_index:04d}_{clip_index:02d}_{safe_stem}_{int(input_seek * 1000):010d}.mp4"
        if clip_path.exists() and clip_path.stat().st_size > 0:
            return clip_path

        trim_start = max(0.0, float(input_seek))
        trim_duration = max(0.05, float(duration))
        info(
            f"split screenshare for chunk {chunk_index}: {source.flv_path.name} "
            f"from {trim_start:.3f}s for {trim_duration:.3f}s"
        )
        # Apply fps before trim. Adobe Connect screen-share streams can be sparse
        # update streams; when a 10-minute output chunk begins between two updates,
        # fps duplicates the last known frame so the new chunk starts with the
        # visible screen state instead of black/empty video.
        vf = (
            f"setpts=PTS-STARTPTS,"
            f"fps={self.screen_fps:g},"
            f"trim=start={trim_start:.6f}:duration={trim_duration:.6f},"
            f"setpts=PTS-STARTPTS"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", str(source.flv_path),
            "-map", "0:v:0",
            "-vf", vf,
            "-an",
            "-t", f"{trim_duration:.3f}",
            "-r", f"{self.screen_fps:g}",
            *self.video_encoder_args(),
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(clip_path),
        ]
        run(cmd)
        return clip_path

    def overlay_detected_videos_for_segment(self, base_video: Path, chunk_start: float, chunk_duration: float, chunk_index: int) -> Path:
        """Overlay screen-share FLVs on a single timeline chunk."""
        if not self.video_segments:
            return base_video

        out = self.workdir / f"scene_segment_{chunk_index:04d}.mp4"
        chunk_end = chunk_start + chunk_duration
        overlaps = []
        for v in self.video_segments:
            v_start = v.start_ms / 1000.0
            v_end = v_start + max(0.1, v.duration_s)
            clip_start = max(chunk_start, v_start)
            clip_end = min(chunk_end, v_end)
            if clip_end <= clip_start + 0.05:
                continue
            overlaps.append({
                "segment": v,
                "input_seek": max(0.0, clip_start - v_start),
                "overlay_start": max(0.0, clip_start - chunk_start),
                "duration": max(0.05, clip_end - clip_start),
            })

        cmd = ["ffmpeg", "-y", "-i", str(base_video)]
        prepared_overlaps = []
        try:
            for clip_index, item in enumerate(overlaps, start=1):
                clip_path = self.make_screenshare_clip_for_segment(
                    item["segment"],
                    float(item["input_seek"]),
                    float(item["duration"]),
                    chunk_index,
                    clip_index,
                )
                prepared = dict(item)
                prepared["clip_path"] = clip_path
                prepared_overlaps.append(prepared)
                cmd += ["-i", str(clip_path)]
        except (subprocess.CalledProcessError, SystemExit) as exc:
            warn(f"تقسیم screen-share برای بخش {chunk_index} خطا داد؛ خروجی پایه استفاده می‌شود: {exc}")
            return base_video

        filter_parts = [f"[0:v]fps={self.screen_fps:g}[basefps]"]
        last = "[basefps]"
        for input_idx, item in enumerate(prepared_overlaps, start=1):
            v = item["segment"]
            rx1, ry1, rx2, ry2 = self.screen_overlay_body_rect_for_segment(v)
            w = max(2, even_dimension(rx2 - rx1))
            h = max(2, even_dimension(ry2 - ry1))
            scaled = f"[segv{input_idx}]"
            over = f"[segov{input_idx}]"
            ov_start = float(item["overlay_start"])
            ov_end = ov_start + float(item["duration"])
            filter_parts.append(
                f"[{input_idx}:v]setpts=PTS-STARTPTS,"
                f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black{scaled}"
            )
            filter_parts.append(
                f"{last}{scaled}overlay={rx1}:{ry1}:enable='between(t,{ov_start:.3f},{ov_end:.3f})':eof_action=pass{over}"
            )
            last = over

        cmd += [
            "-filter_complex", ";".join(filter_parts),
            "-map", last,
            "-an",
            "-t", f"{chunk_duration:.3f}",
            *self.video_encoder_args(), "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(out),
        ]
        try:
            run(cmd)
            return out
        except subprocess.CalledProcessError:
            warn(f"Overlay ویدیوهای screen-share برای بخش {chunk_index} خطا داد؛ خروجی پایه استفاده می‌شود.")
            return base_video

    def mux_segment(self, scene_video: Path, audio_path: Path, chunk_start: float, chunk_duration: float, chunk_index: int, segments_dir: Path) -> Path:
        segment_out = segments_dir / f"segment_{chunk_index:04d}.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-i", str(scene_video),
            "-ss", f"{chunk_start:.3f}",
            "-t", f"{chunk_duration:.3f}",
            "-i", str(audio_path),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", self.audio_bitrate,
            "-shortest",
            "-movflags", "+faststart",
            str(segment_out),
        ]
        run(cmd)
        return segment_out

    def concat_segments_final(self, segment_paths: List[Path]) -> None:
        step("چسباندن بخش‌های آماده و ساخت MP4 کامل")
        self.output.parent.mkdir(parents=True, exist_ok=True)
        concat_file = self.workdir / "segments_concat.txt"
        with concat_file.open("w", encoding="utf-8") as f:
            for p in segment_paths:
                escaped = str(p.resolve()).replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            "-movflags", "+faststart",
            str(self.output),
        ]
        try:
            run(cmd)
        except subprocess.CalledProcessError:
            warn("concat سریع با copy خطا داد؛ نسخه کامل با encode دوباره ساخته می‌شود.")
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_file),
                *self.video_encoder_args(),
                "-c:a", "aac",
                "-b:a", self.audio_bitrate,
                "-movflags", "+faststart",
                str(self.output),
            ]
            run(cmd)
        info(f"Final output: {self.output.resolve()}")

    def render_segmented_final(self, audio_path: Path) -> None:
        """Render immediately playable chunks, then concatenate them into the final MP4."""
        step(f"رندر بخش‌بخش ویدیو؛ بخش اول سریع‌تر و بخش‌های بعدی حدود {self.format_time(self.segment_seconds)}")
        segments_dir = self._segment_output_dir()
        start_frame, end_frame, total_frames, _, _ = self.render_window()
        segment_frames = max(1, int(round(self.segment_seconds * self.fps)))
        first_segment_frames = int(round((self.first_segment_seconds or self.segment_seconds) * self.fps))
        first_segment_frames = max(1, first_segment_frames)
        ready_segments: List[dict] = []
        segment_paths: List[Path] = []

        existing_by_index: Dict[int, dict] = {}
        manifest_path = segments_dir / "manifest.json"
        if manifest_path.exists():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                for item in data.get("segments", []):
                    idx = int(item.get("index") or 0)
                    fp = segments_dir / str(item.get("filename", ""))
                    if idx and fp.exists() and fp.stat().st_size > 0:
                        existing_by_index[idx] = dict(item)
            except Exception:
                existing_by_index = {}
        self._write_segments_manifest(segments_dir, sorted(existing_by_index.values(), key=lambda x: int(x.get("index") or 0)), final_ready=False)

        ranges: List[Tuple[int, int, int]] = []
        cur = start_frame
        chunk_index = 1
        while cur < end_frame:
            length = first_segment_frames if chunk_index == 1 else segment_frames
            nxt = min(end_frame, cur + length)
            ranges.append((chunk_index, cur, nxt))
            cur = nxt
            chunk_index += 1

        for chunk_index, chunk_start_frame, chunk_end_frame in ranges:
            chunk_start = chunk_start_frame / self.fps
            chunk_duration = max(0.001, (chunk_end_frame - chunk_start_frame) / self.fps)
            existing = existing_by_index.get(chunk_index)
            existing_path = segments_dir / str(existing.get("filename", "")) if existing else None
            if existing and existing_path and existing_path.exists() and existing_path.stat().st_size > 0:
                info(f"resume: بخش {chunk_index} قبلاً آماده بوده و skip شد: {existing_path.name}")
                segment_paths.append(existing_path)
                ready_segments.append(existing)
                self._write_segments_manifest(segments_dir, ready_segments, final_ready=False)
                continue

            base = self.render_base_video_chunk(chunk_index, chunk_start_frame, chunk_end_frame)
            scene = self.overlay_detected_videos_for_segment(base, chunk_start, chunk_duration, chunk_index)
            segment_mp4 = self.mux_segment(scene, audio_path, chunk_start, chunk_duration, chunk_index, segments_dir)
            segment_info = {
                "index": chunk_index,
                "filename": segment_mp4.name,
                "start": round(chunk_start, 3),
                "duration": round(chunk_duration, 3),
                "end": round(chunk_start + chunk_duration, 3),
                "size": segment_mp4.stat().st_size if segment_mp4.exists() else 0,
                "ready_at": time.time(),
            }
            ready_segments.append(segment_info)
            segment_paths.append(segment_mp4)
            self._write_segments_manifest(segments_dir, ready_segments, final_ready=False)
            global_pct = int(min(100, ((chunk_end_frame - start_frame) * 100) / max(1, end_frame - start_frame)))
            info(f"segment ready: {chunk_index} start={chunk_start:.3f}s duration={chunk_duration:.3f}s file={segment_mp4.name}")
            info(f"render progress: {global_pct}%")

        self.concat_segments_final(segment_paths)
        self._write_segments_manifest(segments_dir, ready_segments, final_ready=True)

    def current_ctid_at(self, time_ms: int) -> Optional[int]:
        idx = bisect.bisect_right(self._content_event_times, int(time_ms)) - 1
        cur: Optional[int] = self.content_events[idx].ct_id if idx >= 0 else None
        if cur is None and self.documents:
            cur = sorted(self.documents.keys())[0]
        if cur is not None and cur < 0:
            return None
        return cur

    def current_memento_at(self, time_ms: int, ct_id: Optional[int]) -> Optional[MementoEvent]:
        idx = bisect.bisect_right(self._memento_times, int(time_ms)) - 1
        for ev in reversed(self.mementos[max(0, idx - 80): idx + 1]):
            if ev.ct_id is None or ct_id is None or ev.ct_id == ct_id:
                return ev
        return None

    def current_pointer_at(self, time_ms: int, ct_id: Optional[int], page_index: int) -> Optional[PointerEvent]:
        idx = bisect.bisect_right(self._pointer_times, int(time_ms)) - 1
        cur: Optional[PointerEvent] = None
        for ev in reversed(self.pointer_events[max(0, idx - 180): idx + 1]):
            if ev.ct_id is not None and ct_id is not None and ev.ct_id != ct_id:
                continue
            if ev.page_index is not None and ev.page_index != page_index:
                continue
            cur = ev
            break
        if cur is None or not cur.visible or cur.x is None or cur.y is None:
            return None
        return cur

    def active_annotations(self, time_ms: int, page_index: int) -> List[AnnotationShape]:
        idx = bisect.bisect_right(self._annotation_times, int(time_ms))
        candidates = self.annotations[max(0, idx - 650): idx]
        out = [a for a in candidates if a.page_index is None or abs(a.page_index - page_index) <= 1 or a.page_index == page_index]
        return out[-400:]  # avoid pathological unlimited drawing

    # ------------------------- Dynamic modern layout -------------------------

    def compose_frame(self, t: float):
        """Compose one rendered frame with a collision-free adaptive layout.

        Layout rules:
          - chat only: messages occupy the full canvas
          - PDF + chat: PDF is the dominant panel, chat is a compact side panel
          - screen-share + chat: same structure as PDF + chat
          - PDF + screen-share + chat: screen-share gets the largest share, PDF stays readable,
            chat becomes a compact utility panel
          - no item is painted on top of another; panels are separate rectangles
          - when a source appears/disappears, existing panels glide to their new positions
        """
        assert Image is not None and ImageDraw is not None
        time_ms = int(t * 1000)
        bg = self.get_minimal_background()
        img = bg.copy()
        draw = ImageDraw.Draw(img)
        font_title = self.font(max(17, self.room_h // 34))
        font_small = self.font(max(12, self.room_h // 56))
        font_tiny = self.font(max(10, self.room_h // 78))

        header_h, margin = self.layout_header_metrics()

        # Sleek top bar.
        draw.rounded_rectangle(
            (margin, margin, self.room_w - margin, header_h),
            radius=max(14, int(header_h * 0.28)),
            fill=(15, 23, 42),
        )
        draw.text((margin * 2, margin + (header_h - margin - font_title.size) / 2), "Adobe Connect", fill=(248, 250, 252), font=font_title)
        timestamp = self.format_time(t) + " / " + self.format_time(self.duration_s)
        tw = draw.textlength(timestamp, font=font_small)
        draw.text((self.room_w - margin * 2 - tw, margin + (header_h - margin - font_small.size) / 2), timestamp, fill=(203, 213, 225), font=font_small)

        ct_id = self.current_ctid_at(time_ms)
        doc = self.documents.get(ct_id) if ct_id is not None else None
        memento = self.current_memento_at(time_ms, ct_id)
        page_index = memento.page_index if memento else 0
        if doc and doc.page_images:
            page_index = min(max(0, page_index), len(doc.page_images) - 1)

        boxes = self.layout_boxes_at(time_ms)

        if "pdf" in boxes:
            page_title = "PDF"
            subtitle = "No document"
            if doc and doc.page_images:
                subtitle = f"{doc.name} · page {page_index + 1}/{len(doc.page_images)}"
            elif doc:
                subtitle = doc.name
            self.draw_panel_shell(img, boxes["pdf"]["panel"], page_title, subtitle, icon="doc")
            self.draw_content_area(img, boxes["pdf"]["body"], doc, page_index, memento, time_ms)

        screen = self.screen_video_at(time_ms)
        if "screen" in boxes and screen is not None:
            subtitle = "Live screen share"
            if screen.width and screen.height:
                subtitle = f"Screen share · {screen.width}×{screen.height}"
            self.draw_panel_shell(img, boxes["screen"]["panel"], "Screen", subtitle, icon="screen")
            self.draw_screen_placeholder(img, boxes["screen"]["body"], time_ms)

        if "chat" in boxes:
            self.draw_chat_panel(img, time_ms, boxes["chat"]["panel"])

        # Optional timeline progress bar.
        progress_y = self.room_h - max(8, margin // 2)
        progress_w = int((self.room_w - margin * 2) * min(1.0, t / max(0.1, self.duration_s)))
        draw.rounded_rectangle((margin, progress_y, self.room_w - margin, progress_y + 5), radius=3, fill=(226, 232, 240))
        draw.rounded_rectangle((margin, progress_y, margin + progress_w, progress_y + 5), radius=3, fill=(59, 130, 246))

        draw.text((margin, self.room_h - margin - font_tiny.size), "reconstructed from Adobe Connect XML/FLV", fill=(148, 163, 184), font=font_tiny)
        return img

    def get_minimal_background(self):
        """Return a cached soft background image.

        The old code rebuilt the same RGBA overlay for every frame. For a two-hour
        recording this alone can mean tens of thousands of unnecessary alpha
        composites.
        """
        assert Image is not None and ImageDraw is not None
        if self._background_cache is None:
            base = Image.new("RGB", (self.room_w, self.room_h), (246, 248, 251))
            overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
            odraw = ImageDraw.Draw(overlay)
            odraw.ellipse((-int(self.room_w * 0.12), -int(self.room_h * 0.18), int(self.room_w * 0.38), int(self.room_h * 0.45)), fill=(219, 234, 254, 120))
            odraw.ellipse((int(self.room_w * 0.68), int(self.room_h * 0.18), int(self.room_w * 1.18), int(self.room_h * 0.86)), fill=(224, 231, 255, 90))
            odraw.ellipse((int(self.room_w * 0.18), int(self.room_h * 0.72), int(self.room_w * 0.70), int(self.room_h * 1.20)), fill=(240, 249, 255, 120))
            self._background_cache = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
        return self._background_cache

    def draw_minimal_background(self, img) -> None:
        img.paste(self.get_minimal_background())

    def layout_header_metrics(self) -> Tuple[int, int]:
        margin = max(10, int(min(self.room_w, self.room_h) * 0.018))
        header_h = margin + max(42, int(self.room_h * 0.058))
        return header_h, margin

    def layout_canvas_rect(self) -> Tuple[int, int, int, int]:
        header_h, margin = self.layout_header_metrics()
        bottom_reserved = max(24, int(self.room_h * 0.045))
        return (
            margin,
            header_h + margin,
            self.room_w - margin,
            self.room_h - bottom_reserved,
        )

    def screen_video_at(self, time_ms: int) -> Optional[VideoSegment]:
        idx = bisect.bisect_right(self._video_start_times, int(time_ms)) - 1
        # Sparse screen-share streams may overlap slightly; check nearby intervals.
        for v in reversed(self.video_segments[max(0, idx - 3): idx + 1]):
            start = int(v.start_ms)
            end = start + int(max(0.1, v.duration_s) * 1000)
            if start <= time_ms < end:
                return v
        return None

    def layout_state_at(self, time_ms: int) -> Tuple[bool, bool, bool]:
        ct_id = self.current_ctid_at(time_ms)
        doc = self.documents.get(ct_id) if ct_id is not None else None
        has_pdf = bool(doc and doc.page_images)
        has_screen = self.screen_video_at(time_ms) is not None
        has_chat = bool(self.chat_messages)
        return has_pdf, has_screen, has_chat

    def layout_boxes_at(self, time_ms: int) -> Dict[str, Dict[str, Tuple[int, int, int, int]]]:
        target_state = self.layout_state_at(time_ms)

        # For screen-share starts, move the existing panels into their final non-overlapping
        # positions just before the video overlay appears. That keeps the ffmpeg overlay from
        # colliding with a still-moving PDF/chat panel during the first transition frames.
        upcoming = self.upcoming_screen_start_transition(time_ms)
        if upcoming is not None:
            event_ms, from_state, to_state = upcoming
            source = self.compute_layout_boxes(*from_state)
            target = self.compute_layout_boxes(*to_state)
            progress = self.ease_out_cubic(1.0 - min(1.0, max(0.0, (event_ms - time_ms) / self.layout_animation_ms())))
            return self.interpolate_layout_boxes(source, target, progress)

        target = self.compute_layout_boxes(*target_state)
        transition = self.recent_layout_transition(time_ms)
        if transition is None:
            return target
        event_ms, from_state, to_state = transition

        # Screen-share starts are handled by the pre-transition animation above, so after the
        # first screen frame the layout is already settled and remains collision-free.
        if (not from_state[1]) and to_state[1]:
            return target

        # For screen-share endings and ordinary PDF/chat transitions, animate after the event.
        if to_state != target_state:
            return target
        source = self.compute_layout_boxes(*from_state)
        progress = self.ease_out_cubic(min(1.0, max(0.0, (time_ms - event_ms) / self.layout_animation_ms())))
        return self.interpolate_layout_boxes(source, target, progress)

    @staticmethod
    def layout_animation_ms() -> int:
        return 650

    def recent_layout_transition(self, time_ms: int) -> Optional[Tuple[int, Tuple[bool, bool, bool], Tuple[bool, bool, bool]]]:
        window = self.layout_animation_ms()
        events = set()
        # Screen-share/video start and end are the most important visual transitions.
        for v in self.video_segments:
            start = int(v.start_ms)
            end = start + int(max(0.1, v.duration_s) * 1000)
            events.add(start)
            events.add(end)
        # PDF/content changes can also add/remove the document panel.
        for ev in self.content_events:
            events.add(int(ev.time_ms))
        # First chat message: useful for chat-only recordings or late chat appearance.
        if self.chat_messages:
            events.add(int(self.chat_messages[0].time_ms))

        best: Optional[Tuple[int, Tuple[bool, bool, bool], Tuple[bool, bool, bool]]] = None
        for ev_ms in sorted(events):
            if ev_ms < 0 or time_ms < ev_ms or time_ms - ev_ms > window:
                continue
            before = self.layout_state_at(max(0, ev_ms - 1))
            after = self.layout_state_at(ev_ms + 1)
            if before == after:
                continue
            best = (ev_ms, before, after)
        return best

    def upcoming_screen_start_transition(self, time_ms: int) -> Optional[Tuple[int, Tuple[bool, bool, bool], Tuple[bool, bool, bool]]]:
        window = self.layout_animation_ms()
        best: Optional[Tuple[int, Tuple[bool, bool, bool], Tuple[bool, bool, bool]]] = None
        for v in self.video_segments:
            ev_ms = int(v.start_ms)
            if time_ms >= ev_ms or ev_ms - time_ms > window:
                continue
            before = self.layout_state_at(max(0, ev_ms - 1))
            after = self.layout_state_at(ev_ms + 1)
            if before == after or before[1] or not after[1]:
                continue
            if best is None or ev_ms < best[0]:
                best = (ev_ms, before, after)
        return best

    @staticmethod
    def ease_out_cubic(p: float) -> float:
        p = min(1.0, max(0.0, p))
        return 1 - pow(1 - p, 3)

    @staticmethod
    def lerp(a: float, b: float, p: float) -> float:
        return a + (b - a) * p

    def lerp_rect(self, a: Tuple[int, int, int, int], b: Tuple[int, int, int, int], p: float) -> Tuple[int, int, int, int]:
        return tuple(int(round(self.lerp(x, y, p))) for x, y in zip(a, b))  # type: ignore[return-value]

    @staticmethod
    def collapsed_rect(rect: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
        x1, y1, x2, y2 = rect
        cx = (x1 + x2) / 2
        cy = (y1 + y2) / 2
        w = max(8, (x2 - x1) * 0.08)
        h = max(8, (y2 - y1) * 0.08)
        return (int(cx - w / 2), int(cy - h / 2), int(cx + w / 2), int(cy + h / 2))

    def interpolate_layout_boxes(
        self,
        source: Dict[str, Dict[str, Tuple[int, int, int, int]]],
        target: Dict[str, Dict[str, Tuple[int, int, int, int]]],
        progress: float,
    ) -> Dict[str, Dict[str, Tuple[int, int, int, int]]]:
        out: Dict[str, Dict[str, Tuple[int, int, int, int]]] = {}
        for key, target_box in target.items():
            source_box = source.get(key)
            if source_box is None:
                panel_from = self.collapsed_rect(target_box["panel"])
            else:
                panel_from = source_box["panel"]
            panel = self.lerp_rect(panel_from, target_box["panel"], progress)
            out[key] = {"panel": panel, "body": self.panel_body_rect(panel)}
        return out

    def compute_layout_boxes(self, has_pdf: bool, has_screen: bool, has_chat: bool) -> Dict[str, Dict[str, Tuple[int, int, int, int]]]:
        """Return non-overlapping panel/body rectangles for the current media mix."""
        canvas = self.layout_canvas_rect()
        x1, y1, x2, y2 = canvas
        W = max(1, x2 - x1)
        H = max(1, y2 - y1)
        gap = max(10, int(min(self.room_w, self.room_h) * 0.018))
        boxes: Dict[str, Dict[str, Tuple[int, int, int, int]]] = {}

        def add(key: str, rect: Tuple[int, int, int, int]) -> None:
            # Clamp defensively so odd custom sizes never create invalid rectangles.
            rx1, ry1, rx2, ry2 = rect
            rx1 = max(x1, min(x2 - 4, rx1))
            ry1 = max(y1, min(y2 - 4, ry1))
            rx2 = max(rx1 + 4, min(x2, rx2))
            ry2 = max(ry1 + 4, min(y2, ry2))
            panel = (rx1, ry1, rx2, ry2)
            boxes[key] = {"panel": panel, "body": self.panel_body_rect(panel)}

        active = [name for name, ok in (("pdf", has_pdf), ("screen", has_screen), ("chat", has_chat)) if ok]
        if not active:
            add("empty", canvas)
            return boxes

        # Very narrow outputs switch to vertical responsive layouts to guarantee no overlap.
        narrow = W < 900

        if active == ["chat"]:
            add("chat", canvas)
            return boxes
        if active == ["pdf"]:
            add("pdf", canvas)
            return boxes
        if active == ["screen"]:
            add("screen", canvas)
            return boxes

        if has_pdf and has_screen and has_chat:
            if narrow:
                screen_h = int(H * 0.56)
                bottom_y = y1 + screen_h + gap
                chat_w = max(180, int(W * 0.30))
                add("screen", (x1, y1, x2, y1 + screen_h))
                add("pdf", (x1, bottom_y, x2 - chat_w - gap, y2))
                add("chat", (x2 - chat_w, bottom_y, x2, y2))
            else:
                chat_w = max(190, int(W * 0.17))
                remaining_w = max(1, W - chat_w - 2 * gap)
                # Screen-share is wider because most shares are 16:9 or ultrawide.
                screen_w = int(remaining_w * 0.63)
                pdf_w = remaining_w - screen_w
                add("screen", (x1, y1, x1 + screen_w, y2))
                add("pdf", (x1 + screen_w + gap, y1, x1 + screen_w + gap + pdf_w, y2))
                add("chat", (x2 - chat_w, y1, x2, y2))
            return boxes

        if has_pdf and has_screen:
            if narrow:
                screen_h = int(H * 0.58)
                add("screen", (x1, y1, x2, y1 + screen_h))
                add("pdf", (x1, y1 + screen_h + gap, x2, y2))
            else:
                screen_w = int((W - gap) * 0.62)
                add("screen", (x1, y1, x1 + screen_w, y2))
                add("pdf", (x1 + screen_w + gap, y1, x2, y2))
            return boxes

        if has_pdf and has_chat:
            if narrow:
                chat_h = max(140, int(H * 0.28))
                add("pdf", (x1, y1, x2, y2 - chat_h - gap))
                add("chat", (x1, y2 - chat_h, x2, y2))
            else:
                chat_w = min(max(230, int(W * 0.24)), int(W * 0.34))
                add("pdf", (x1, y1, x2 - chat_w - gap, y2))
                add("chat", (x2 - chat_w, y1, x2, y2))
            return boxes

        if has_screen and has_chat:
            if narrow:
                chat_h = max(140, int(H * 0.28))
                add("screen", (x1, y1, x2, y2 - chat_h - gap))
                add("chat", (x1, y2 - chat_h, x2, y2))
            else:
                chat_w = min(max(230, int(W * 0.23)), int(W * 0.32))
                add("screen", (x1, y1, x2 - chat_w - gap, y2))
                add("chat", (x2 - chat_w, y1, x2, y2))
            return boxes

        # Fallback should be rare, but keeps the renderer deterministic.
        for key in active:
            add(key, canvas)
            break
        return boxes

    def panel_body_rect(self, panel: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
        x1, y1, x2, y2 = panel
        w = max(1, x2 - x1)
        h = max(1, y2 - y1)
        pad = max(8, int(min(w, h) * 0.035))
        header_h = max(30, min(58, int(h * 0.115)))
        return (x1 + pad, y1 + header_h + max(6, pad // 2), x2 - pad, y2 - pad)

    def draw_panel_shell(self, img, rect: Tuple[int, int, int, int], title: str, subtitle: str = "", icon: str = "") -> None:
        assert Image is not None and ImageDraw is not None
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        x1, y1, x2, y2 = rect
        w = x2 - x1
        h = y2 - y1
        radius = max(16, int(min(w, h) * 0.055))
        header_h = max(30, min(58, int(h * 0.115)))
        pad = max(10, int(min(w, h) * 0.04))
        title_font = self.font(max(13, min(22, h // 18)))
        sub_font = self.font(max(10, min(15, h // 30)))

        odraw.rounded_rectangle((x1 + 3, y1 + 5, x2 + 3, y2 + 5), radius=radius, fill=(15, 23, 42, 22))
        odraw.rounded_rectangle((x1, y1, x2, y2), radius=radius, fill=(255, 255, 255, 244), outline=(226, 232, 240, 255), width=1)
        odraw.rounded_rectangle((x1, y1, x2, y1 + header_h), radius=radius, fill=(248, 250, 252, 250), outline=(226, 232, 240, 235), width=1)
        odraw.rectangle((x1, y1 + header_h - radius, x2, y1 + header_h), fill=(248, 250, 252, 250))

        # Tiny icon chip.
        chip = (x1 + pad, y1 + max(6, (header_h - 22) // 2), x1 + pad + 22, y1 + max(6, (header_h - 22) // 2) + 22)
        chip_fill = (219, 234, 254, 255) if icon in {"screen", "doc"} else (241, 245, 249, 255)
        odraw.rounded_rectangle(chip, radius=7, fill=chip_fill)
        cx1, cy1, cx2, cy2 = chip
        if icon == "screen":
            odraw.rectangle((cx1 + 5, cy1 + 6, cx2 - 5, cy2 - 7), outline=(37, 99, 235, 255), width=2)
            odraw.line((cx1 + 9, cy2 - 4, cx2 - 9, cy2 - 4), fill=(37, 99, 235, 255), width=2)
        elif icon == "doc":
            odraw.rectangle((cx1 + 7, cy1 + 5, cx2 - 6, cy2 - 5), outline=(37, 99, 235, 255), width=2)
            odraw.line((cx1 + 9, cy1 + 11, cx2 - 8, cy1 + 11), fill=(37, 99, 235, 255), width=1)
            odraw.line((cx1 + 9, cy1 + 15, cx2 - 9, cy1 + 15), fill=(37, 99, 235, 255), width=1)
        else:
            odraw.ellipse((cx1 + 6, cy1 + 6, cx2 - 6, cy2 - 6), fill=(37, 99, 235, 255))

        text_x = x1 + pad + 32
        title_y = y1 + max(4, (header_h - self.text_height(odraw, title, title_font) - (self.text_height(odraw, subtitle, sub_font) if subtitle else 0)) / 2 - 1)
        odraw.text((text_x, title_y), self.display_text_for_pil(title), fill=(15, 23, 42, 255), font=title_font)
        if subtitle:
            clean_sub = subtitle
            max_sub_w = max(30, x2 - pad - text_x)
            while odraw.textlength(self.display_text_for_pil(clean_sub), font=sub_font) > max_sub_w and len(clean_sub) > 8:
                clean_sub = clean_sub[:-2]
            if clean_sub != subtitle:
                clean_sub = clean_sub.rstrip() + "…"
            odraw.text((text_x, title_y + self.text_height(odraw, title, title_font) + 1), self.display_text_for_pil(clean_sub), fill=(100, 116, 139, 255), font=sub_font)

        img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"))

    def visible_chat_messages(self, time_ms: int) -> List[ChatMessage]:
        if not self.chat_messages:
            return []
        idx = bisect.bisect_right(self._chat_times, int(time_ms))
        return self.chat_messages[max(0, idx - 90): idx]

    def draw_chat_panel(self, img, time_ms: int, panel_rect: Tuple[int, int, int, int]) -> None:
        if not self.chat_messages:
            return
        assert Image is not None and ImageDraw is not None

        messages = self.visible_chat_messages(time_ms)
        self.draw_panel_shell(img, panel_rect, "Messages", f"{len(messages)} / {len(self.chat_messages)}", icon="chat")

        body = self.panel_body_rect(panel_rect)
        x1, y1, x2, y2 = body
        w = max(1, x2 - x1)
        h = max(1, y2 - y1)
        sender_font = self.font(max(10, min(14, h // 18)))
        msg_font = self.font(max(10, min(16, h // 16)))
        tiny_font = self.font(max(9, min(12, h // 24)))
        pad = max(8, int(min(w, h) * 0.045))
        max_text_w = max(40, w - pad * 2)
        meta_h = self.text_height(ImageDraw.Draw(img), "00:00 Participant", sender_font)
        line_gap = max(2, int(h * 0.006))
        msg_line_h = max(self.text_height(ImageDraw.Draw(img), "Ag", msg_font), getattr(msg_font, "size", 12)) + line_gap
        compact = w < 260 or h < 240
        max_lines_per_msg = 2 if compact else 4

        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        # Chat body background is intentionally very light; bubbles carry the hierarchy.
        odraw.rounded_rectangle(body, radius=max(12, int(min(w, h) * 0.04)), fill=(248, 250, 252, 255), outline=(226, 232, 240, 255), width=1)

        blocks = []
        used_h = 0
        for msg in reversed(messages):
            sender = msg.sender or "Participant"
            meta = f"{self.format_time(msg.time_ms / 1000)}  {sender}"
            lines = self.wrap_text_to_width(odraw, msg.text, msg_font, max_text_w)
            if len(lines) > max_lines_per_msg:
                lines = lines[: max_lines_per_msg - 1] + [lines[max_lines_per_msg - 1] + " …"]
            block_h = meta_h + 4 + len(lines) * msg_line_h + pad + 6
            if used_h + block_h > (h - pad * 2) and blocks:
                break
            blocks.append((msg, meta, lines, block_h))
            used_h += block_h
        blocks.reverse()

        if not blocks:
            empty_text = "No messages yet"
            tw = odraw.textlength(empty_text, font=msg_font)
            odraw.text((x1 + (w - tw) / 2, y1 + pad), empty_text, fill=(100, 116, 139, 255), font=msg_font)
        else:
            y = y2 - pad - used_h
            for i, (_, meta, lines, block_h) in enumerate(blocks):
                bubble_x1 = x1 + pad
                bubble_x2 = x2 - pad
                bubble_y1 = y
                bubble_y2 = y + block_h - max(5, pad // 2)
                odraw.rounded_rectangle((bubble_x1, bubble_y1, bubble_x2, bubble_y2), radius=max(10, pad), fill=(255, 255, 255, 255), outline=(226, 232, 240, 230), width=1)
                meta_draw = self.display_text_for_pil(meta)
                meta_x = bubble_x2 - pad - odraw.textlength(meta_draw, font=sender_font) if self.contains_rtl(meta) else bubble_x1 + pad
                odraw.text((meta_x, y + max(4, pad // 2)), meta_draw, fill=(71, 85, 105, 255), font=sender_font)
                y += meta_h + 4 + max(4, pad // 2)
                for line in lines:
                    line_draw = self.display_text_for_pil(line)
                    line_x = bubble_x2 - pad - odraw.textlength(line_draw, font=msg_font) if self.contains_rtl(line) else bubble_x1 + pad
                    odraw.text((line_x, y), line_draw, fill=(15, 23, 42, 255), font=msg_font)
                    y += msg_line_h
                y = bubble_y2 + max(5, pad // 2)
                if i < len(blocks) - 1:
                    odraw.line((x1 + pad * 2, y - max(3, pad // 3), x2 - pad * 2, y - max(3, pad // 3)), fill=(226, 232, 240, 140), width=1)

        # If the full panel is mostly chat, add a small hint that it is live/timeline-synced.
        if panel_rect[2] - panel_rect[0] > self.room_w * 0.55:
            hint = "Timeline synced chat"
            odraw.text((x2 - pad - odraw.textlength(hint, font=tiny_font), y2 - pad - self.text_height(odraw, hint, tiny_font)), hint, fill=(148, 163, 184, 255), font=tiny_font)

        img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"))

    @staticmethod
    def contains_rtl(text: str) -> bool:
        return bool(re.search(r"[\u0600-\u06FF]", text or ""))

    @staticmethod
    def display_text_for_pil(text: str) -> str:
        """Prepare RTL text for PIL when python-bidi/arabic-reshaper are available.

        Without those optional packages, a simple reverse fallback is used so Persian/Arabic
        messages are at least read in the correct visual order in most basic Pillow builds.
        """
        if not text:
            return text
        if not AdobeConnectRenderer.contains_rtl(text):
            return text
        try:
            if arabic_reshaper is not None and get_display is not None:
                return get_display(arabic_reshaper.reshape(text))
        except Exception:
            pass
        return text[::-1]

    @staticmethod
    def text_height(draw, text: str, font) -> int:
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            return max(1, bbox[3] - bbox[1])
        except Exception:
            return max(1, getattr(font, "size", 12))

    @staticmethod
    def wrap_text_to_width(draw, text: str, font, max_width: int) -> List[str]:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines: List[str] = []
        for para in text.split("\n"):
            words = para.split()
            if not words:
                lines.append("")
                continue
            cur = ""
            for word in words:
                test = word if not cur else cur + " " + word
                if draw.textlength(test, font=font) <= max_width:
                    cur = test
                    continue
                if cur:
                    lines.append(cur)
                    cur = word
                while draw.textlength(cur, font=font) > max_width and len(cur) > 1:
                    cut = len(cur)
                    while cut > 1 and draw.textlength(cur[:cut], font=font) > max_width:
                        cut -= 1
                    lines.append(cur[:cut])
                    cur = cur[cut:]
            if cur:
                lines.append(cur)
        return lines or [""]

    def draw_content_area(self, img, rect, doc: Optional[DocumentAsset], page_index: int, memento: Optional[MementoEvent], time_ms: int) -> None:
        assert Image is not None and ImageDraw is not None
        draw = ImageDraw.Draw(img)
        x1, y1, x2, y2 = rect
        w = max(1, x2 - x1)
        h = max(1, y2 - y1)
        radius = max(12, int(min(w, h) * 0.035))
        draw.rounded_rectangle(rect, radius=radius, fill=(248, 250, 252), outline=(226, 232, 240), width=1)
        if not doc or not doc.page_images:
            self.draw_no_content(draw, rect)
            return
        try:
            page_path = doc.page_images[page_index]
            page = self.load_page_image(page_path)
        except Exception:
            self.draw_no_content(draw, rect)
            return

        # Fit page into its own panel body. Nothing else is painted in this rectangle.
        scale = min(w / page.width, h / page.height)
        render_w = max(1, int(page.width * scale))
        render_h = max(1, int(page.height * scale))
        page_resized = self.resized_page_image(page_path, render_w, render_h)
        px = x1 + (w - render_w) // 2
        py = y1 + (h - render_h) // 2
        # Soft shadow under the page.
        shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        sdraw = ImageDraw.Draw(shadow)
        sdraw.rounded_rectangle((px + 2, py + 4, px + render_w + 2, py + render_h + 4), radius=8, fill=(15, 23, 42, 28))
        img.paste(Image.alpha_composite(img.convert("RGBA"), shadow).convert("RGB"))
        img.paste(page_resized, (px, py))

        page_rect = (px, py, px + render_w, py + render_h)
        shapes = self.active_annotations(time_ms, page_index)
        for shape in shapes:
            self.draw_shape(draw, shape, page_rect)

        pointer = self.current_pointer_at(time_ms, doc.ct_id, page_index)
        if pointer:
            self.draw_pointer(draw, pointer, page_rect)

    def draw_no_content(self, draw, rect) -> None:
        font = self.font(28)
        x1, y1, x2, y2 = rect
        text = "No active shared content"
        tw = draw.textlength(text, font=font)
        draw.text((x1 + (x2 - x1 - tw) / 2, y1 + (y2 - y1) / 2), text, fill=(100, 116, 139), font=font)

    def draw_screen_placeholder(self, img, rect: Tuple[int, int, int, int], time_ms: int) -> None:
        assert Image is not None and ImageDraw is not None
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        x1, y1, x2, y2 = rect
        w = max(1, x2 - x1)
        h = max(1, y2 - y1)
        radius = max(12, int(min(w, h) * 0.035))
        odraw.rounded_rectangle(rect, radius=radius, fill=(11, 16, 32, 255), outline=(30, 41, 59, 255), width=1)
        # Minimal grid, hidden enough not to fight the real overlay.
        step_x = max(54, w // 8)
        step_y = max(42, h // 6)
        for xx in range(x1 + step_x, x2, step_x):
            odraw.line((xx, y1, xx, y2), fill=(51, 65, 85, 80), width=1)
        for yy in range(y1 + step_y, y2, step_y):
            odraw.line((x1, yy, x2, yy), fill=(51, 65, 85, 80), width=1)
        font = self.font(max(14, min(28, h // 12)))
        sub_font = self.font(max(10, min(15, h // 25)))
        title = "Screen share"
        sub = "The shared screen is composited here"
        tw = odraw.textlength(title, font=font)
        sw = odraw.textlength(sub, font=sub_font)
        odraw.text((x1 + (w - tw) / 2, y1 + h / 2 - getattr(font, "size", 14)), title, fill=(226, 232, 240, 180), font=font)
        odraw.text((x1 + (w - sw) / 2, y1 + h / 2 + 8), sub, fill=(148, 163, 184, 150), font=sub_font)
        img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"))

    def draw_pointer(self, draw, pointer: PointerEvent, page_rect: Tuple[int, int, int, int]) -> None:
        x1, y1, x2, y2 = page_rect
        pw = max(1, x2 - x1)
        ph = max(1, y2 - y1)
        assert pointer.x is not None and pointer.y is not None
        raw_x, raw_y = pointer.x, pointer.y

        if pointer.coord_mode == "normalized" or (pointer.coord_mode == "auto" and 0.0 <= raw_x <= 1.0 and 0.0 <= raw_y <= 1.0):
            px = x1 + raw_x * pw
            py = y1 + raw_y * ph
        elif pointer.coord_mode == "percent":
            px = x1 + (raw_x / 100.0) * pw
            py = y1 + (raw_y / 100.0) * ph
        else:
            px = x1 + (raw_x / max(1, self.room_w)) * pw
            py = y1 + (raw_y / max(1, self.room_h)) * ph

        px = max(x1, min(x2, px))
        py = max(y1, min(y2, py))

        radius = max(10, int(min(self.room_w, self.room_h) * 0.018))
        dot = max(3, radius // 4)
        red = (220, 38, 38)
        dark_red = (127, 29, 29)
        shadow = (15, 23, 42)

        draw.ellipse((px - radius + 2, py - radius + 2, px + radius + 2, py + radius + 2), outline=shadow, width=max(2, radius // 6))
        draw.ellipse((px - radius, py - radius, px + radius, py + radius), outline=red, width=max(2, radius // 5))
        draw.ellipse((px - dot, py - dot, px + dot, py + dot), fill=red, outline=dark_red)
        tail = radius * 1.35
        draw.line((px - tail, py + tail, px - radius * 0.25, py + radius * 0.25), fill=red, width=max(2, radius // 5))

    def draw_shape(self, draw, shape: AnnotationShape, page_rect: Tuple[int, int, int, int]) -> None:
        x1, y1, x2, y2 = page_rect
        pw = x2 - x1
        ph = y2 - y1
        color = color_int_to_rgb(shape.stroke_col)
        width = max(1, int(shape.stroke_weight))

        base_w = max(1, self.room_w)
        base_h = max(1, self.room_h)
        sx = x1 + (shape.x / base_w) * pw
        sy = y1 + (shape.y / base_h) * ph
        sw = (shape.width / base_w) * pw
        sh = (shape.height / base_h) * ph

        if shape.shape_type.lower() in ("pencil", "highlighter", "line") and shape.points:
            pts = [(sx + px * sw, sy + py * sh) for px, py in shape.points]
            if len(pts) >= 2:
                draw.line(pts, fill=color, width=width, joint="curve")
            elif len(pts) == 1:
                r = width + 1
                draw.ellipse((pts[0][0]-r, pts[0][1]-r, pts[0][0]+r, pts[0][1]+r), fill=color)
        elif shape.shape_type.lower() in ("rectangle", "rect"):
            draw.rectangle((sx, sy, sx + sw, sy + sh), outline=color, width=width)
        elif shape.shape_type.lower() in ("ellipse", "circle"):
            draw.ellipse((sx, sy, sx + sw, sy + sh), outline=color, width=width)
        elif shape.html_text:
            font = self.font(18)
            draw.text((sx, sy), shape.html_text, fill=color, font=font)

    def screen_overlay_body_rect_for_segment(self, segment: VideoSegment) -> Tuple[int, int, int, int]:
        sample_ms = int(segment.start_ms) + 1
        has_pdf, _has_screen, has_chat = self.layout_state_at(sample_ms)
        boxes = self.compute_layout_boxes(has_pdf, True, has_chat)
        if "screen" not in boxes:
            boxes = self.compute_layout_boxes(False, True, has_chat)
        return boxes["screen"]["body"]

    def overlay_detected_videos(self, base_video: Path) -> Path:
        if not self.video_segments:
            return base_video
        if self.clip_start_s > 0 or self.clip_duration_s > 0:
            _, _, _, window_start, window_duration = self.render_window()
            return self.overlay_detected_videos_for_segment(base_video, window_start, window_duration, 0)
        step("Overlay کردن فایل‌های screenshare داخل پنل جداگانه و بدون هم‌افتادگی")
        out = self.workdir / "scene_with_video_overlays.mp4"

        cmd = ["ffmpeg", "-y", "-i", str(base_video)]
        for v in self.video_segments:
            cmd += ["-i", str(v.flv_path)]

        # The base scene may be rendered at a low FPS for speed. Upsample only inside
        # ffmpeg before overlaying screen-share videos so the shared screen remains smooth
        # without forcing PIL to render thousands of extra frames.
        filter_parts = [f"[0:v]fps={self.screen_fps:g}[basefps]"]
        last = "[basefps]"
        for idx, v in enumerate(self.video_segments, start=1):
            start = v.start_ms / 1000.0
            end = start + max(0.1, v.duration_s)
            rx1, ry1, rx2, ry2 = self.screen_overlay_body_rect_for_segment(v)
            w = max(2, even_dimension(rx2 - rx1))
            h = max(2, even_dimension(ry2 - ry1))
            scaled = f"[v{idx}]"
            over = f"[ov{idx}]"
            filter_parts.append(
                f"[{idx}:v]setpts=PTS-STARTPTS+{start:.6f}/TB,"
                f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black{scaled}"
            )
            filter_parts.append(
                f"{last}{scaled}overlay={rx1}:{ry1}:enable='between(t,{start:.3f},{end:.3f})':eof_action=pass{over}"
            )
            last = over
        filter_complex = ";".join(filter_parts)
        cmd += [
            "-filter_complex", filter_complex,
            "-map", last,
            "-an",
            "-t", f"{self.duration_s:.3f}",
            *self.video_encoder_args(), "-pix_fmt", "yuv420p",
            str(out),
        ]
        try:
            run(cmd)
            return out
        except subprocess.CalledProcessError:
            warn("Overlay ویدیوهای screen-share خطا داد؛ خروجی پایه بدون overlay استفاده می‌شود.")
            return base_video

    def mux_final(self, video_path: Path, audio_path: Path) -> None:
        step("Mux نهایی و ساخت MP4 خروجی")
        self.output.parent.mkdir(parents=True, exist_ok=True)
        _, _, _, window_start, window_duration = self.render_window()
        cmd = ["ffmpeg", "-y", "-i", str(video_path)]
        if self.clip_start_s > 0 or self.clip_duration_s > 0:
            cmd += ["-ss", f"{window_start:.3f}", "-t", f"{window_duration:.3f}"]
        cmd += [
            "-i", str(audio_path),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", self.audio_bitrate,
            "-shortest",
            "-movflags", "+faststart",
            str(self.output),
        ]
        run(cmd)
        info(f"Final output: {self.output.resolve()}")

    def build_chapters(self) -> List[dict]:
        chapters: List[dict] = [{"time": 0.0, "title": "شروع کلاس", "type": "start"}]
        for ev in self.content_events:
            doc = self.documents.get(ev.ct_id)
            title = f"اسلاید / محتوا: {doc.name}" if doc else "تغییر محتوای اصلی"
            chapters.append({"time": round(ev.time_ms / 1000, 3), "title": title, "type": "content", "ct_id": ev.ct_id})
        for ev in self.mementos:
            chapters.append({"time": round(ev.time_ms / 1000, 3), "title": f"تغییر صفحه: {ev.page_index + 1}", "type": "slide", "page": ev.page_index + 1})
        for v in self.video_segments:
            start = round(v.start_ms / 1000, 3)
            end = round(start + max(0.1, v.duration_s), 3)
            chapters.append({"time": start, "title": "شروع Screen Share", "type": "screen_start"})
            chapters.append({"time": end, "title": "پایان Screen Share", "type": "screen_end"})
        for msg in self.chat_messages:
            text = (msg.text or "").strip()
            if len(text) >= 80 or "?" in text or "؟" in text:
                chapters.append({"time": round(msg.time_ms / 1000, 3), "title": f"پیام مهم: {text[:70]}", "type": "chat", "sender": msg.sender})

        deduped: List[dict] = []
        seen = set()
        for ch in sorted(chapters, key=lambda x: float(x.get("time", 0.0))):
            bucket = int(float(ch.get("time", 0.0)) // 2)
            key = (bucket, ch.get("title"), ch.get("type"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(ch)
        return deduped[:800]

    def export_metadata_files(self) -> Path:
        meta_dir = self.workdir / "metadata"
        meta_dir.mkdir(parents=True, exist_ok=True)
        chapters = self.build_chapters()
        chat_items = [
            {"time": round(m.time_ms / 1000, 3), "time_ms": m.time_ms, "sender": m.sender, "text": m.text}
            for m in self.chat_messages
        ]
        docs = [
            {"ct_id": d.ct_id, "name": d.name, "type": d.the_type, "pages": len(d.page_images), "local_path": str(d.local_path) if d.local_path else ""}
            for d in sorted(self.documents.values(), key=lambda x: x.ct_id)
        ]
        search_index = []
        for m in chat_items:
            search_index.append({"time": m["time"], "type": "chat", "title": m["sender"], "text": m["text"]})
        for d in docs:
            search_index.append({"time": 0.0, "type": "document", "title": d["name"], "text": f"{d['name']} {d['type']}"})
        for ch in chapters:
            search_index.append({"time": ch["time"], "type": "chapter", "title": ch["title"], "text": ch["title"]})

        (meta_dir / "chat.json").write_text(json.dumps(chat_items, ensure_ascii=False, indent=2), encoding="utf-8")
        (meta_dir / "chat.txt").write_text("\n".join(f"[{self.format_time(m['time'])}] {m['sender']}: {m['text']}" for m in chat_items), encoding="utf-8")
        (meta_dir / "chapters.json").write_text(json.dumps(chapters, ensure_ascii=False, indent=2), encoding="utf-8")
        (meta_dir / "search_index.json").write_text(json.dumps(search_index, ensure_ascii=False, indent=2), encoding="utf-8")
        (meta_dir / "recording_summary.json").write_text(json.dumps({
            "duration": round(self.duration_s, 3),
            "room_size": [self.room_w, self.room_h],
            "documents": docs,
            "chat_messages": len(chat_items),
            "screen_share_segments": len(self.video_segments),
            "audio_segments": len(self.audio_segments),
            "chapters": len(chapters),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        def vtt_time(t: float) -> str:
            t = max(0.0, float(t))
            h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
            return f"{h:02d}:{m:02d}:{s:06.3f}".replace('.', ',')
        lines = ["WEBVTT", ""]
        for i, ch in enumerate(chapters):
            start = float(ch.get("time", 0.0))
            end = float(chapters[i + 1].get("time", start + 10.0)) if i + 1 < len(chapters) else min(self.duration_s, start + 10.0)
            lines += [f"{vtt_time(start)} --> {vtt_time(max(start + 1.0, end))}", str(ch.get("title", "Chapter")), ""]
        (meta_dir / "chapters.vtt").write_text("\n".join(lines), encoding="utf-8")
        info(f"Metadata exports: {meta_dir}")
        return meta_dir

    @staticmethod
    def format_time(seconds: float) -> str:
        seconds = max(0, int(seconds))
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        if h:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def cleanup(self) -> None:
        if self.keep:
            info(f"Workdir kept: {self.workdir}")
        else:
            try:
                shutil.rmtree(self.workdir)
            except Exception:
                pass

    def run_all(self) -> None:
        started = time.time()
        ensure_binary("ffmpeg")
        ensure_binary("ffprobe")
        if Image is None:
            die("Pillow نصب نیست. اجرا کن: pip install pillow")
        self.parse_url()
        if self.offline_zip:
            # When a real Adobe Connect URL is supplied together with --zip, use it only
            # for cookies/session and for downloading missing PDF/source assets.
            if self.origin != "https://offline.local":
                self.open_session()
            zip_path = self.download_zip()
        else:
            self.open_session()
            zip_path = self.download_zip()
        self.extract_zip(zip_path)
        self.parse_recording()
        audio_report = self.export_audio_timeline_report()
        self.prepare_documents()
        self.export_pdf_assets()
        self.export_metadata_files()
        audio = self.build_audio_track()
        if self.segment_seconds > 0:
            self.render_segmented_final(audio)
        else:
            base = self.render_base_video()
            scene = self.overlay_detected_videos(base)
            self.mux_final(scene, audio)
        elapsed = time.time() - started
        step(f"تمام شد ✅ زمان اجرا: {elapsed:.1f}s")


# ----------------------------- CLI -----------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Download and reconstruct an Adobe Connect recording ZIP into MP4.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            Examples:
              python adobe_connect_downloader.py "https://liveme.ac.ir/py82yo5lic0k/?session=TOKEN" -o class.mp4
              python adobe_connect_downloader.py --zip recording.zip -o class.mp4 --keep
              python adobe_connect_downloader.py "URL" -o class.mp4 --fps 1 --size 854x480
            """
        ),
    )
    p.add_argument("url", nargs="?", help="Adobe Connect class/recording URL containing ?session=...")
    p.add_argument("-o", "--output", default="adobe_connect_recording.mp4", help="Output MP4 path")
    p.add_argument("--workdir", help="Working directory; default: temporary directory")
    p.add_argument("--keep", action="store_true", help="Keep working directory after finishing")
    p.add_argument("--zip", dest="offline_zip", help="Use an already downloaded recording ZIP instead of downloading")
    p.add_argument("--fps", type=float, default=1.0, help="Base scene FPS. Keep this low for speed; screen-share overlays are upsampled separately with --screen-fps.")
    p.add_argument("--screen-fps", type=float, default=6.0, help="Output FPS while compositing screenshare_*.flv overlays. Default: 6 for laptop-friendly rendering.")
    p.add_argument("--segment-seconds", type=float, default=0.0, help="When >0, publish playable MP4 chunks while rendering and concatenate them at the end.")
    p.add_argument("--first-segment-seconds", type=float, default=0.0, help="Optional shorter first segment for faster first playable output.")
    p.add_argument("--clip-start", type=float, default=0.0, help="Render only from this timeline second.")
    p.add_argument("--clip-duration", type=float, default=0.0, help="Render only this many seconds; 0 means full remaining duration.")
    p.add_argument("--encoder", default="auto", help="Video encoder: auto/cpu/nvidia.")
    p.add_argument("--workers", type=int, default=1, help="Reserved for safe parallel segment rendering architecture.")
    p.add_argument("--preset", default="ultrafast", help="x264 preset for intermediate renders. Use veryfast/medium for smaller files; ultrafast is quicker.")
    p.add_argument("--crf", type=int, default=31, help="x264 CRF. Higher values render smaller/lower-quality files; 31 is the optimized 480p default.")
    p.add_argument("--audio-bitrate", default="96k", help="AAC audio bitrate, e.g. 80k, 96k, 128k.")
    p.add_argument("--size", help="Force output size, e.g. 854x480. Default: use roomSize from XML.")
    p.add_argument("--skip-download-assets", action="store_true", help="Do not download PDF/source assets; use files already in zip/placeholders.")
    p.add_argument("--pdf-output-dir", help="Folder to copy downloaded/found PDF files into. Default: <output_stem>_pdfs next to the MP4.")
    return p


def parse_size(size: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    if not size:
        return None, None
    m = re.fullmatch(r"(\d+)x(\d+)", size.strip().lower())
    if not m:
        die("فرمت --size باید مثل 1280x720 باشد.")
    return int(m.group(1)), int(m.group(2))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not args.url and not args.offline_zip:
        args.url = input("لینک کلاس Adobe Connect را وارد کن: ").strip()
    if not args.url and args.offline_zip:
        # Dummy valid origin/path for offline render; asset downloads are impossible offline.
        args.url = "https://offline.local/recording/"
        args.skip_download_assets = True
    width, height = parse_size(args.size)
    app = AdobeConnectRenderer(
        class_url=args.url,
        output=Path(args.output),
        workdir=Path(args.workdir) if args.workdir else None,
        fps=max(0.5, args.fps),
        width=width,
        height=height,
        keep=args.keep,
        offline_zip=Path(args.offline_zip) if args.offline_zip else None,
        skip_download_assets=args.skip_download_assets,
        pdf_output_dir=Path(args.pdf_output_dir) if args.pdf_output_dir else None,
        ffmpeg_preset=args.preset,
        video_crf=max(18, min(35, int(args.crf))),
        audio_bitrate=args.audio_bitrate,
        screen_fps=args.screen_fps,
        segment_seconds=args.segment_seconds,
        first_segment_seconds=args.first_segment_seconds,
        clip_start_s=args.clip_start,
        clip_duration_s=args.clip_duration,
        encoder=args.encoder,
        workers=args.workers,
    )
    try:
        app.run_all()
    finally:
        app.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
