#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
JOB_DIR = BASE_DIR / "jobs"
CACHE_DIR = BASE_DIR / "cache"
ANALYSIS_CACHE_DIR = CACHE_DIR / "analysis"
PACKAGE_DIR = OUTPUT_DIR / "packages"

for folder in (UPLOAD_DIR, OUTPUT_DIR, JOB_DIR, CACHE_DIR, ANALYSIS_CACHE_DIR, PACKAGE_DIR):
    folder.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024 * 1024  # 10 GB; local-only utility
app.config["SECRET_KEY"] = os.environ.get("AC_UI_SECRET", "local-adobe-connect-ui")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
MESSAGE_TIME_RE = re.compile(r'<Message\s+time="(\d+)"', re.I)
DEFAULT_QUALITY = "q480"
FIRST_SEGMENT_SECONDS = 10 * 60

# فقط چهار کیفیت مجاز. 480p برای اکثر لپ‌تاپ‌ها پیشنهاد می‌شود؛
# کیفیت‌های بالاتر فشار CPU/GPU و زمان رندر را به‌طور محسوسی زیاد می‌کنند.
QUALITY_PRESETS = {
    "q360": {
        "label": "360p / خیلی سبک",
        "size": "640x360",
        "fps": 0.75,
        "screen_fps": 4,
        "preset": "ultrafast",
        "crf": 33,
        "audio_bitrate": "80k",
        "preview_every": 2.5,
        "segment_seconds": 600,
        "first_segment_seconds": FIRST_SEGMENT_SECONDS,
        "speed_factor": 2.8,
        "description": "سبک‌ترین حالت برای لپ‌تاپ‌های ضعیف؛ کیفیت پایین‌تر ولی سریع‌تر",
    },
    "q480": {
        "label": "480p / پیشنهادی ✅",
        "size": "854x480",
        "fps": 1.0,
        "screen_fps": 6,
        "preset": "ultrafast",
        "crf": 31,
        "audio_bitrate": "96k",
        "preview_every": 2.0,
        "segment_seconds": 600,
        "first_segment_seconds": FIRST_SEGMENT_SECONDS,
        "speed_factor": 2.3,
        "description": "پیشنهاد اصلی؛ با اکثر لپ‌تاپ‌ها قابل رندر است و زمان/حجم کمتری می‌گیرد",
    },
    "q720": {
        "label": "720p / نیازمند سیستم قوی‌تر",
        "size": "1280x720",
        "fps": 1.25,
        "screen_fps": 8,
        "preset": "ultrafast",
        "crf": 29,
        "audio_bitrate": "112k",
        "preview_every": 1.75,
        "segment_seconds": 600,
        "first_segment_seconds": FIRST_SEGMENT_SECONDS,
        "speed_factor": 1.35,
        "description": "کیفیت بهتر؛ روی سیستم‌های ضعیف کندتر است و زمان زیادی می‌گیرد",
    },
    "q1080": {
        "label": "1080p / سنگین و زمان‌بر",
        "size": "1920x1080",
        "fps": 1.5,
        "screen_fps": 10,
        "preset": "ultrafast",
        "crf": 28,
        "audio_bitrate": "128k",
        "preview_every": 1.5,
        "segment_seconds": 600,
        "first_segment_seconds": FIRST_SEGMENT_SECONDS,
        "speed_factor": 0.8,
        "description": "فقط برای سیستم قوی؛ رندر آن نسبت به 480p خیلی طولانی‌تر است",
    },
}

ELEMENTS = {
    "pdf": "PDF / اسلایدها",
    "screen": "Screen Share",
    "chat": "پیام‌ها / Chat",
    "pointer": "Pointer مدرس",
    "annotations": "Annotation / نوشتن روی اسلاید",
    "audio": "صدا",
}

HARDWARE_ACCEL = {
    "auto": "Auto",
    "cpu": "CPU / سازگار با همه سیستم‌ها",
    "nvidia": "GPU NVIDIA / GTX 1050 به بالا",
}

WORKER_OPTIONS = ["auto", "1", "2", "3", "4"]

uploaded_zips: Dict[str, Path] = {}
upload_meta: Dict[str, dict] = {}
jobs: Dict[str, dict] = {}
processes: Dict[str, subprocess.Popen] = {}
lock = threading.RLock()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def clean_log(line: str) -> str:
    return ANSI_RE.sub("", line).rstrip()


def human_size(n: float) -> str:
    n = float(n or 0)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if n < 1024 or unit == units[-1]:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def format_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds or 0))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_path_for(job_id: str) -> Path:
    return JOB_DIR / job_id / "job_manifest.json"


def persist_job(job_id: str) -> None:
    with lock:
        job = jobs.get(job_id)
        if not job:
            return
        data = dict(job)
        data.pop("pid", None)
        data.pop("process", None)
        path = manifest_path_for(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def load_jobs_from_disk() -> None:
    for path in sorted(JOB_DIR.glob("*/job_manifest.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            job_id = data.get("id") or path.parent.name
            if data.get("status") in {"queued", "running", "paused"}:
                data["status"] = "interrupted"
                data["error"] = "برنامه بسته شده بود؛ برای ادامه Resume را بزن."
            jobs[job_id] = data
        except Exception:
            continue


def stage_from_line(line: str) -> Tuple[str, str, float]:
    mapping = [
        ("دانلود فایل ZIP", "downloading", "Downloading ZIP", 8),
        ("استخراج", "extracting", "Extracting", 16),
        ("تحلیل XML", "parsing", "Parsing", 24),
        ("Metadata exports", "metadata", "Exporting metadata", 30),
        ("ساخت ترک صوتی", "audio", "Building audio", 36),
        ("رندر بخش‌بخش", "rendering", "Rendering segments", 45),
        ("رندر ویدیوی", "rendering", "Rendering video", 45),
        ("Rendering chunk", "rendering", "Rendering segment", 45),
        ("Overlay", "overlay", "Compositing screen-share", 78),
        ("split screenshare", "overlay", "Preparing screen-share", 70),
        ("چسباندن بخش", "merging", "Merging", 94),
        ("Mux نهایی", "encoding", "Encoding final MP4", 92),
        ("WEB_RUNNER_DONE", "done", "Done", 100),
        ("تمام شد", "done", "Done", 100),
    ]
    for key, stage, label, pct in mapping:
        if key in line:
            return stage, label, pct
    return "", "", -1


def update_progress_from_line(job: dict, line: str) -> None:
    current = float(job.get("progress", 0))
    stage, stage_label, stage_pct = stage_from_line(line)
    if stage:
        job["stage"] = stage
        job["stage_label"] = stage_label
        current = max(current, stage_pct)

    m = re.search(r"render progress:\s*(\d+)%", line)
    if m:
        pct = max(0, min(100, int(m.group(1))))
        current = max(current, 45 + pct * 0.42)

    m = re.search(r"segment ready:\s*(\d+).*duration=([0-9.]+)s", line)
    if m:
        job["current_segment"] = int(m.group(1))
        job["completed_segments"] = max(int(job.get("completed_segments") or 0), int(m.group(1)))

    if "این فایل قبلاً پردازش شده" in line or "cache" in line.lower():
        job["cache_used"] = True

    elapsed = max(0.1, time.time() - float(job.get("started_at") or time.time()))
    progress = min(100.0, max(current, float(job.get("progress", 0))))
    job["progress"] = round(progress, 1)
    if progress > 1 and progress < 99:
        total_est = elapsed / (progress / 100.0)
        job["eta_seconds"] = max(0, int(total_est - elapsed))
    job["elapsed_seconds"] = int(elapsed)


def append_log(job_id: str, line: str) -> None:
    line = clean_log(line)
    if not line:
        return
    with lock:
        job = jobs.get(job_id)
        if not job:
            return
        job.setdefault("logs", []).append(line)
        job["logs"] = job["logs"][-900:]
        job["last_update"] = time.time()
        update_progress_from_line(job, line)
    persist_job(job_id)


def read_segments_manifest(job: dict, job_id: str) -> List[dict]:
    segments_dir_value = job.get("segments_dir", "")
    if not segments_dir_value:
        return []
    segments_dir = Path(segments_dir_value)
    manifest_path = segments_dir / "manifest.json"
    items: List[dict] = []
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            for item in data.get("segments", []):
                filename = str(item.get("filename", ""))
                path = segments_dir / filename
                if not filename.startswith("segment_") or path.suffix.lower() != ".mp4" or not path.exists():
                    continue
                clean = dict(item)
                clean["url"] = url_for("segment_video", job_id=job_id, filename=filename)
                clean["download_url"] = clean["url"]
                items.append(clean)
        except Exception:
            items = []
    if not manifest_path.exists() and segments_dir.exists():
        for path in sorted(segments_dir.glob("segment_*.mp4")):
            try:
                index = int(path.stem.split("_", 1)[1])
            except Exception:
                index = len(items) + 1
            items.append({
                "index": index,
                "filename": path.name,
                "start": None,
                "duration": None,
                "end": None,
                "size": path.stat().st_size,
                "url": url_for("segment_video", job_id=job_id, filename=path.name),
                "download_url": url_for("segment_video", job_id=job_id, filename=path.name),
            })
    return sorted(items, key=lambda x: int(x.get("index") or 0))


def read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def build_recording_download_url(class_url: str) -> str:
    """Build the public Adobe Connect ZIP URL without using session/cookies.

    The browser must already be logged in to the LMS/Adobe Connect domain before
    the user clicks this URL. The app no longer opens a session or downloads the
    ZIP itself; it only renders from the uploaded ZIP.
    """
    value = (class_url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("لینک کلاس معتبر نیست. لینک باید با http یا https شروع شود.")

    meeting_path = parsed.path.strip("/")
    if "/output/" in meeting_path:
        meeting_path = meeting_path.split("/output/", 1)[0].strip("/")
    if not meeting_path:
        raise ValueError("مسیر کلاس از داخل لینک پیدا نشد.")

    origin = f"{parsed.scheme}://{parsed.netloc}"
    return f"{origin}/{meeting_path}/output/name.zip?download=zip"


def save_zip_upload(file_storage) -> Optional[str]:
    if not file_storage or not file_storage.filename:
        return None
    filename = secure_filename(file_storage.filename) or "recording.zip"
    if not filename.lower().endswith(".zip"):
        filename += ".zip"
    upload_id = uuid.uuid4().hex[:12]
    target = UPLOAD_DIR / f"{upload_id}_{filename}"
    file_storage.save(target)
    input_hash = file_sha256(target)
    uploaded_zips[upload_id] = target
    upload_meta[upload_id] = {"hash": input_hash, "filename": filename, "size": target.stat().st_size, "uploaded_at": now_iso()}
    return upload_id


def quick_analyze_zip(zip_path: Path, input_hash: str) -> dict:
    cache_path = ANALYSIS_CACHE_DIR / f"{input_hash}.json"
    if cache_path.exists():
        data = read_json(cache_path, {})
        if data:
            data["cache_hit"] = True
            return data

    started = time.time()
    xml_count = flv_count = pdf_count = chat_count = 0
    screen_count = audio_count = 0
    duration_s = 0.0
    xml_names: List[str] = []
    pdf_names: List[str] = []
    has_chat = False
    has_screen = False
    has_audio = False
    document_titles: List[str] = []

    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            infos = z.infolist()
            for info in infos:
                name = info.filename
                lower = name.lower()
                if lower.endswith(".xml"):
                    xml_count += 1
                    xml_names.append(name)
                    # Avoid reading giant XMLs entirely when possible.
                    try:
                        text = z.read(info, pwd=None).decode("utf-8", errors="ignore")
                    except Exception:
                        text = ""
                    for m in MESSAGE_TIME_RE.finditer(text):
                        try:
                            duration_s = max(duration_s, int(m.group(1)) / 1000.0)
                        except Exception:
                            pass
                    for m in re.finditer(r"<metadata>\s*<Number><!\[CDATA\[([0-9.]+)\]\]></Number>", text, re.S):
                        try:
                            duration_s = max(duration_s, float(m.group(1)))
                        except Exception:
                            pass
                    if "ftchat" in lower or "chat" in lower or "<chat" in text.lower() or "setmessages" in text.lower():
                        has_chat = True
                        chat_count += text.lower().count("<message")
                    for title in re.findall(r"<theName><!\[CDATA\[(.*?)\]\]></theName>", text, re.S):
                        clean = re.sub(r"\s+", " ", title).strip()
                        if clean and clean not in document_titles:
                            document_titles.append(clean[:120])
                elif lower.endswith(".pdf"):
                    pdf_count += 1
                    pdf_names.append(Path(name).name)
                elif lower.endswith(".flv"):
                    flv_count += 1
                    if any(x in lower for x in ("camera", "voip", "audio")):
                        audio_count += 1
                        has_audio = True
                    else:
                        screen_count += 1
                        has_screen = True
    except zipfile.BadZipFile:
        return {"ok": False, "error": "فایل ZIP خراب است یا package معتبر نیست."}
    except Exception as exc:
        return {"ok": False, "error": f"تحلیل ZIP ناموفق بود: {exc}"}

    if duration_s <= 0:
        duration_s = 60.0

    # همیشه 480p را پیشنهاد بده؛ 720p و 1080p عمداً انتخاب دستی می‌خواهند
    # چون روی لپ‌تاپ‌های معمولی فشار و زمان رندر را زیاد می‌کنند.
    preset = DEFAULT_QUALITY

    q = QUALITY_PRESETS[preset]
    # Rough estimate: audio + low-fps scene video + occasional screen share. Kept intentionally conservative.
    mb_per_min = {"q360": 3.0, "q480": 4.0, "q720": 7.0, "q1080": 12.0}.get(preset, 4.0)
    output_mb = max(2.0, (duration_s / 60.0) * mb_per_min)
    speed_factor = float(q.get("speed_factor", 1.0))
    eta = int(max(20, duration_s / max(0.2, speed_factor)))

    analysis = {
        "ok": True,
        "input_hash": input_hash,
        "filename": zip_path.name.split("_", 1)[-1],
        "zip_size": zip_path.stat().st_size,
        "zip_size_label": human_size(zip_path.stat().st_size),
        "duration_seconds": round(duration_s, 3),
        "duration_label": format_seconds(duration_s),
        "xml_count": xml_count,
        "flv_count": flv_count,
        "pdf_count": pdf_count,
        "slide_or_document_count": max(pdf_count, len(document_titles)),
        "chat_detected": bool(has_chat),
        "screen_share_detected": bool(has_screen),
        "audio_detected": bool(has_audio or audio_count),
        "screen_share_count": screen_count,
        "audio_count": audio_count,
        "suggested_preset": preset,
        "suggested_preset_label": q["label"],
        "estimated_output_mb": round(output_mb, 1),
        "estimated_output_label": human_size(output_mb * 1024 * 1024),
        "estimated_processing_seconds": eta,
        "estimated_processing_label": format_seconds(eta),
        "document_titles": document_titles[:20],
        "pdf_names": pdf_names[:20],
        "analyzed_at": now_iso(),
        "analysis_elapsed_ms": int((time.time() - started) * 1000),
        "cache_hit": False,
    }
    cache_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    return analysis


def create_output_package(job_id: str) -> Optional[Path]:
    with lock:
        job = jobs.get(job_id)
        if not job:
            return None
        output_path = Path(job.get("output_path", ""))
        workdir = Path(job.get("workdir", ""))
    if not output_path.exists():
        return None

    package_path = PACKAGE_DIR / f"adobe_connect_class_package_{job_id}.zip"
    mp3_path = OUTPUT_DIR / f"adobe_connect_{job_id}.mp3"
    if not mp3_path.exists():
        try:
            cmd = ["ffmpeg", "-y", "-i", str(output_path), "-vn", "-c:a", "libmp3lame", "-b:a", "128k", str(mp3_path)]
            subprocess.run(cmd, cwd=str(BASE_DIR), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=600)
        except Exception:
            pass

    meta_dir = workdir / "metadata"
    pdf_dir = output_path.with_suffix("").with_name(output_path.stem + "_pdfs")
    segments_dir = Path(job.get("segments_dir", "")) if job.get("segments_dir") else None

    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.write(output_path, f"video/{output_path.name}")
        if mp3_path.exists():
            z.write(mp3_path, f"audio/{mp3_path.name}")
        if meta_dir.exists():
            for p in meta_dir.rglob("*"):
                if p.is_file():
                    z.write(p, f"metadata/{p.relative_to(meta_dir)}")
        if pdf_dir.exists():
            for p in pdf_dir.rglob("*"):
                if p.is_file():
                    z.write(p, f"pdf/{p.relative_to(pdf_dir)}")
        if segments_dir and segments_dir.exists():
            manifest = segments_dir / "manifest.json"
            if manifest.exists():
                z.write(manifest, "segments/manifest.json")
        summary = {
            "job_id": job_id,
            "created_at": now_iso(),
            "preset": job.get("quality"),
            "analysis": job.get("analysis", {}),
            "outputs": ["MP4", "MP3" if mp3_path.exists() else "", "PDF", "Chat JSON/TXT", "Chapters JSON/VTT"],
        }
        z.writestr("README.json", json.dumps(summary, ensure_ascii=False, indent=2))

    with lock:
        if job_id in jobs:
            jobs[job_id]["package_path"] = str(package_path)
            jobs[job_id]["mp3_path"] = str(mp3_path) if mp3_path.exists() else ""
            jobs[job_id]["package_size"] = package_path.stat().st_size
    persist_job(job_id)
    return package_path


def public_job_payload(job_id: str) -> dict:
    with lock:
        job = jobs.get(job_id)
        if not job:
            return {"ok": False, "error": "job not found"}
        output_path = Path(job.get("output_path", ""))
        ready = output_path.exists() and output_path.stat().st_size > 0 and job.get("status") == "done"
        preview_dir = Path(job.get("preview_dir", "")) if job.get("preview_dir") else None
        preview_image = preview_dir / "latest.jpg" if preview_dir else None
        preview_ready = bool(preview_image and preview_image.exists() and preview_image.stat().st_size > 0)
        preview_state = read_json(preview_dir / "state.json", {}) if preview_ready and preview_dir else {}
        segments = read_segments_manifest(job, job_id)
        package_path = Path(job.get("package_path", "")) if job.get("package_path") else None
        mp3_path = Path(job.get("mp3_path", "")) if job.get("mp3_path") else None
        workdir = Path(job.get("workdir", "")) if job.get("workdir") else None
        chapters = []
        search_count = 0
        if workdir:
            chapters = read_json(workdir / "metadata" / "chapters.json", [])[:80]
            search_count = len(read_json(workdir / "metadata" / "search_index.json", []))
        elapsed = int(job.get("elapsed_seconds") or (time.time() - float(job.get("started_at") or time.time()))) if job.get("started_at") else 0
        payload = {
            "ok": True,
            "id": job_id,
            "status": job.get("status"),
            "job_type": job.get("job_type", "full"),
            "title": job.get("title", "Adobe Connect Recording"),
            "progress": job.get("progress", 0),
            "stage": job.get("stage", "queued"),
            "stage_label": job.get("stage_label", "Queued"),
            "logs": job.get("logs", []),
            "video_ready": ready,
            "video_url": url_for("video", job_id=job_id) if output_path.exists() and output_path.stat().st_size > 0 else "",
            "download_url": url_for("download_output", job_id=job_id) if output_path.exists() and output_path.stat().st_size > 0 else "",
            "package_ready": bool(package_path and package_path.exists()),
            "package_url": url_for("download_package", job_id=job_id) if package_path and package_path.exists() else "",
            "mp3_ready": bool(mp3_path and mp3_path.exists()),
            "mp3_url": url_for("download_mp3", job_id=job_id) if mp3_path and mp3_path.exists() else "",
            "segments_ready": len(segments),
            "segments": segments,
            "preview_ready": preview_ready,
            "preview_url": url_for("live_preview", job_id=job_id) if preview_ready else "",
            "preview_state": preview_state,
            "error": job.get("error", ""),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "created_at": job.get("created_at"),
            "quality": job.get("quality") if job.get("quality") in QUALITY_PRESETS else DEFAULT_QUALITY,
            "quality_label": QUALITY_PRESETS.get(job.get("quality", DEFAULT_QUALITY), QUALITY_PRESETS[DEFAULT_QUALITY]).get("label", DEFAULT_QUALITY),
            "elements": job.get("elements", []),
            "analysis": job.get("analysis", {}),
            "cache_used": bool(job.get("cache_used") or job.get("analysis", {}).get("cache_hit")),
            "current_segment": job.get("current_segment"),
            "completed_segments": len(segments) or job.get("completed_segments", 0),
            "segments_total": job.get("segments_total"),
            "elapsed_seconds": elapsed,
            "elapsed_label": format_seconds(elapsed),
            "eta_seconds": job.get("eta_seconds", 0),
            "eta_label": format_seconds(job.get("eta_seconds", 0)),
            "search_count": search_count,
            "chapters": chapters,
            "hardware_accel": job.get("hardware_accel", "auto"),
            "workers": job.get("workers", "1"),
        }
        return payload


def run_subprocess_job(job_id: str, command: List[str]) -> None:
    with lock:
        job = jobs[job_id]
        job["status"] = "running"
        job["stage"] = "starting"
        job["stage_label"] = "Starting"
        job["progress"] = max(1, job.get("progress", 0))
        job.setdefault("logs", []).append("شروع پردازش…")
        job["started_at"] = job.get("started_at") or time.time()
        job["last_update"] = time.time()
    persist_job(job_id)

    proc: Optional[subprocess.Popen] = None
    try:
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
        with lock:
            job = jobs.get(job_id, {})
            preview_dir = job.get("preview_dir", "")
            segments_dir = job.get("segments_dir", "")
            hardware = job.get("hardware_accel", "auto")
        if preview_dir:
            child_env["AC_LIVE_PREVIEW_DIR"] = str(preview_dir)
            child_env["AC_LIVE_PREVIEW_EVERY"] = str(job.get("preview_every", 0.75))
        if segments_dir:
            child_env["AC_SEGMENTS_DIR"] = str(segments_dir)
            child_env["AC_SEGMENT_SECONDS"] = str(job.get("segment_seconds", 600))
        child_env["AC_ENCODER"] = str(hardware or "auto")
        proc = subprocess.Popen(
            command,
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=child_env,
        )
        with lock:
            processes[job_id] = proc
            jobs[job_id]["pid"] = proc.pid
        persist_job(job_id)

        assert proc.stdout is not None
        for line in proc.stdout:
            append_log(job_id, line)

        return_code = proc.wait()
        with lock:
            job = jobs[job_id]
            job["return_code"] = return_code
            job["finished_at"] = time.time()
            job["elapsed_seconds"] = int(job["finished_at"] - float(job.get("started_at") or job["finished_at"]))
            output_ok = Path(job["output_path"]).exists() and Path(job["output_path"]).stat().st_size > 0
            if return_code == 0 and output_ok:
                job["status"] = "done"
                job["stage"] = "packaging" if job.get("job_type") == "full" else "done"
                job["stage_label"] = "Packaging" if job.get("job_type") == "full" else "Done"
                job["progress"] = 98 if job.get("job_type") == "full" else 100
                job["logs"].append("ویدیو آماده است ✅")
            elif job.get("status") == "cancelled":
                job["logs"].append("پردازش لغو شد.")
            else:
                job["status"] = "error"
                job["error"] = friendly_error(f"پردازش با کد {return_code} متوقف شد.", job.get("logs", []))
                job["logs"].append(job["error"])
        persist_job(job_id)

        with lock:
            is_full_done = jobs.get(job_id, {}).get("status") == "done" and jobs.get(job_id, {}).get("job_type") == "full"
        if is_full_done:
            append_log(job_id, "ساخت پکیج کامل کلاس…")
            create_output_package(job_id)
            with lock:
                if jobs.get(job_id):
                    jobs[job_id]["stage"] = "done"
                    jobs[job_id]["stage_label"] = "Done"
                    jobs[job_id]["progress"] = 100
                    jobs[job_id]["logs"].append("پکیج کامل کلاس آماده است ✅")
            persist_job(job_id)
    except Exception as exc:
        with lock:
            job = jobs.get(job_id)
            if job:
                job["status"] = "error"
                job["error"] = friendly_error(str(exc), job.get("logs", []))
                job["finished_at"] = time.time()
                job.setdefault("logs", []).append(f"خطا: {job['error']}")
        persist_job(job_id)
    finally:
        with lock:
            processes.pop(job_id, None)
            if jobs.get(job_id):
                jobs[job_id].pop("pid", None)
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        persist_job(job_id)


def friendly_error(error: str, logs: List[str]) -> str:
    text = "\n".join(logs[-60:]) + "\n" + error
    lower = text.lower()
    if "ffmpeg" in lower and ("not found" in lower or "no such file" in lower):
        return "ffmpeg نصب نیست یا در PATH نیست. ffmpeg/ffprobe را نصب کن و دوباره Resume بزن."
    if "encoder" in lower and ("not found" in lower or "unknown encoder" in lower):
        return "encoder انتخاب‌شده در ffmpeg موجود نیست. Hardware Acceleration را روی Auto یا CPU بگذار و Resume بزن."
    if "no space" in lower or "disk" in lower:
        return "فضای دیسک کافی نیست. چند فایل موقت را پاک کن یا مسیر خروجی را عوض کن."
    if "badzipfile" in lower or "zip" in lower and "خراب" in lower:
        return "فایل ZIP خراب است یا خروجی Adobe Connect کامل دانلود نشده است."
    return error


def build_command(job: dict) -> List[str]:
    quality = QUALITY_PRESETS.get(job.get("quality", DEFAULT_QUALITY), QUALITY_PRESETS[DEFAULT_QUALITY])
    elements = job.get("elements") or list(ELEMENTS.keys())
    command = [
        sys.executable,
        str(BASE_DIR / "web_runner.py"),
        "--output", str(job["output_path"]),
        "--workdir", str(job["workdir"]),
        "--fps", str(quality["fps"]),
        "--screen-fps", str(quality["screen_fps"]),
        "--segment-seconds", str(job.get("segment_seconds", quality.get("segment_seconds", 600))),
        "--first-segment-seconds", str(job.get("first_segment_seconds", quality.get("first_segment_seconds", FIRST_SEGMENT_SECONDS))),
        "--preset", str(quality["preset"]),
        "--crf", str(quality.get("crf", 24)),
        "--audio-bitrate", str(quality.get("audio_bitrate", "96k")),
        "--size", str(quality["size"]),
        "--elements", ",".join(elements),
        "--encoder", str(job.get("hardware_accel", "auto")),
        "--workers", str(job.get("workers", "1") if job.get("workers") != "auto" else "1"),
        "--cache-dir", str(CACHE_DIR),
    ]
    if job.get("input_hash"):
        command += ["--input-hash", str(job["input_hash"])]
    if float(job.get("clip_start", 0) or 0) > 0:
        command += ["--clip-start", str(job.get("clip_start"))]
    if float(job.get("clip_duration", 0) or 0) > 0:
        command += ["--clip-duration", str(job.get("clip_duration"))]
    # New UI flow is ZIP-first/offline. Do not pass the class URL together with
    # the ZIP, otherwise the renderer may try to open a session for missing assets.
    if job.get("zip_path"):
        command += ["--zip", job["zip_path"]]
    elif job.get("class_url"):
        command += ["--url", job["class_url"]]
    return command


def create_job_from_request(job_type: str = "full") -> Tuple[Optional[dict], Optional[Tuple[dict, int]]]:
    class_url = request.form.get("class_url", "").strip()
    upload_id = request.form.get("upload_id", "").strip()
    quality_key = request.form.get("quality", DEFAULT_QUALITY)
    if quality_key not in QUALITY_PRESETS:
        quality_key = DEFAULT_QUALITY
    quality = QUALITY_PRESETS.get(quality_key, QUALITY_PRESETS[DEFAULT_QUALITY])
    hardware_accel = request.form.get("hardware_accel", "auto")
    if request.form.get("gpu_1050") in ("1", "on", "true", "yes"):
        hardware_accel = "nvidia"
    if hardware_accel not in HARDWARE_ACCEL:
        hardware_accel = "auto"
    workers = request.form.get("workers", "auto")
    if workers not in WORKER_OPTIONS:
        workers = "auto"

    zip_path: Optional[Path] = uploaded_zips.get(upload_id) if upload_id else None
    if not zip_path and request.files.get("zip_file") and request.files["zip_file"].filename:
        new_upload_id = save_zip_upload(request.files["zip_file"])
        if new_upload_id:
            zip_path = uploaded_zips.get(new_upload_id)
            upload_id = new_upload_id

    elements = request.form.getlist("elements")
    elements = [e for e in elements if e in ELEMENTS]
    if not elements:
        elements = list(ELEMENTS.keys())

    if not zip_path:
        return None, ({"ok": False, "error": "برای ساخت کلاس، اول فایل ZIP ضبط را آپلود کن. دانلود خودکار با session در این نسخه استفاده نمی‌شود."}, 400)

    input_hash = ""
    analysis = {}
    if zip_path and zip_path.exists():
        input_hash = upload_meta.get(upload_id, {}).get("hash") or file_sha256(zip_path)
        analysis = quick_analyze_zip(zip_path, input_hash)
        if analysis.get("ok") and request.form.get("use_suggested") == "1":
            quality_key = analysis.get("suggested_preset", quality_key)
            quality = QUALITY_PRESETS.get(quality_key, quality)

    clip_start = float(request.form.get("preview_start", "0") or 0) if job_type == "preview" else 0.0
    clip_duration = float(request.form.get("preview_duration", "60") or 60) if job_type == "preview" else 0.0
    if job_type == "preview":
        clip_duration = max(10.0, min(300.0, clip_duration))
        # Preview jobs are rendered as one quickly playable MP4, not a segmented final.
        segment_seconds = 0
        first_segment_seconds = 0
    else:
        segment_seconds = int(quality.get("segment_seconds", 600))
        first_segment_seconds = int(quality.get("first_segment_seconds", FIRST_SEGMENT_SECONDS))

    job_id = uuid.uuid4().hex[:12]
    output_path = OUTPUT_DIR / (f"preview_{job_id}.mp4" if job_type == "preview" else f"class_{job_id}.mp4")
    workdir = JOB_DIR / job_id
    workdir.mkdir(parents=True, exist_ok=True)
    preview_dir = workdir / "live_preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    segments_dir = workdir / "rendered_segments"
    segments_dir.mkdir(parents=True, exist_ok=True)

    duration = float(analysis.get("duration_seconds") or 0) if analysis.get("ok") else 0
    segments_total = None
    if duration and job_type == "full":
        first = max(1, first_segment_seconds)
        rest = max(1, segment_seconds)
        segments_total = 1 + max(0, int((max(0, duration - first) + rest - 1) // rest))

    job = {
        "id": job_id,
        "title": "Preview" if job_type == "preview" else "Adobe Connect Class",
        "job_type": job_type,
        "status": "queued",
        "stage": "queued",
        "stage_label": "Queued",
        "progress": 0,
        "logs": [],
        "error": "",
        "output_path": str(output_path),
        "workdir": str(workdir),
        "preview_dir": str(preview_dir),
        "segments_dir": str(segments_dir),
        "quality": quality_key,
        "preview_every": quality.get("preview_every", 0.75),
        "segment_seconds": segment_seconds,
        "first_segment_seconds": first_segment_seconds,
        "elements": elements,
        "created_at": now_iso(),
        "started_at": time.time(),
        "finished_at": None,
        "upload_id": upload_id,
        "zip_path": str(zip_path) if zip_path else "",
        "class_url": class_url,
        "input_hash": input_hash,
        "analysis": analysis if analysis.get("ok") else {},
        "hardware_accel": hardware_accel,
        "gpu_1050": request.form.get("gpu_1050") in ("1", "on", "true", "yes"),
        "workers": workers,
        "clip_start": clip_start,
        "clip_duration": clip_duration,
        "segments_total": segments_total,
        "cache_used": bool(analysis.get("cache_hit")),
    }
    job["command"] = build_command(job)
    return job, None


@app.get("/")
def index():
    return render_template(
        "index.html",
        qualities=QUALITY_PRESETS,
        elements=ELEMENTS,
        hardware_options=HARDWARE_ACCEL,
        worker_options=WORKER_OPTIONS,
        default_quality=DEFAULT_QUALITY,
    )


@app.get("/watch/<job_id>")
def watch(job_id: str):
    return render_template("watch.html", job_id=job_id)


@app.post("/api/download_link")
def api_download_link():
    class_url = request.form.get("class_url", "").strip()
    try:
        download_url = build_recording_download_url(class_url)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({
        "ok": True,
        "class_url": class_url,
        "download_url": download_url,
    })


@app.post("/api/upload_zip")
def api_upload_zip():
    upload_id = save_zip_upload(request.files.get("zip_file"))
    if not upload_id:
        return jsonify({"ok": False, "error": "فایل ZIP انتخاب نشده است."}), 400
    path = uploaded_zips[upload_id]
    meta = upload_meta.get(upload_id, {})
    return jsonify({
        "ok": True,
        "upload_id": upload_id,
        "filename": path.name,
        "size": path.stat().st_size,
        "input_hash": meta.get("hash", ""),
        "download_url": url_for("download_uploaded_zip", upload_id=upload_id),
    })


@app.post("/api/analyze_upload")
def api_analyze_upload():
    upload_id = request.form.get("upload_id", "").strip()
    zip_path = uploaded_zips.get(upload_id) if upload_id else None
    if not zip_path and request.files.get("zip_file") and request.files["zip_file"].filename:
        upload_id = save_zip_upload(request.files["zip_file"]) or ""
        zip_path = uploaded_zips.get(upload_id)
    if not zip_path or not zip_path.exists():
        return jsonify({"ok": False, "error": "فایل ZIP برای تحلیل پیدا نشد."}), 400
    input_hash = upload_meta.get(upload_id, {}).get("hash") or file_sha256(zip_path)
    analysis = quick_analyze_zip(zip_path, input_hash)
    analysis["upload_id"] = upload_id
    return jsonify(analysis), 200 if analysis.get("ok") else 400


@app.get("/download-upload/<upload_id>")
def download_uploaded_zip(upload_id: str):
    path = uploaded_zips.get(upload_id)
    if not path or not path.exists():
        return "فایل پیدا نشد", 404
    return send_file(path, as_attachment=True, download_name=path.name.split("_", 1)[-1])


@app.post("/api/start")
def api_start():
    job, error = create_job_from_request("full")
    if error:
        payload, status = error
        return jsonify(payload), status
    assert job is not None
    with lock:
        jobs[job["id"]] = job
    persist_job(job["id"])
    thread = threading.Thread(target=run_subprocess_job, args=(job["id"], job["command"]), daemon=True)
    thread.start()
    return jsonify({"ok": True, "job_id": job["id"], "watch_url": url_for("watch", job_id=job["id"])})


@app.post("/api/preview")
def api_preview():
    job, error = create_job_from_request("preview")
    if error:
        payload, status = error
        return jsonify(payload), status
    assert job is not None
    with lock:
        jobs[job["id"]] = job
    persist_job(job["id"])
    thread = threading.Thread(target=run_subprocess_job, args=(job["id"], job["command"]), daemon=True)
    thread.start()
    return jsonify({"ok": True, "job_id": job["id"], "watch_url": url_for("watch", job_id=job["id"])})


@app.get("/api/jobs")
def api_jobs():
    with lock:
        ids = sorted(jobs.keys(), key=lambda x: jobs[x].get("started_at") or 0, reverse=True)
    items = []
    for job_id in ids[:100]:
        payload = public_job_payload(job_id)
        if payload.get("ok"):
            payload["logs"] = []
            payload["segments"] = []
            payload["chapters"] = []
            items.append(payload)
    return jsonify({"ok": True, "jobs": items})


@app.get("/api/jobs/<job_id>")
def api_job(job_id: str):
    payload = public_job_payload(job_id)
    status = 200 if payload.get("ok") else 404
    return jsonify(payload), status


@app.post("/api/jobs/<job_id>/pause")
def api_pause_job(job_id: str):
    with lock:
        proc = processes.get(job_id)
        job = jobs.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job پیدا نشد"}), 404
    if proc and proc.poll() is None:
        try:
            if os.name == "posix":
                os.kill(proc.pid, signal.SIGSTOP)
                job["status"] = "paused"
                job["stage_label"] = "Paused"
                job.setdefault("logs", []).append("پردازش Pause شد.")
                persist_job(job_id)
                return jsonify({"ok": True})
            return jsonify({"ok": False, "error": "Pause واقعی فقط روی Linux/macOS پشتیبانی می‌شود؛ از Cancel و Resume استفاده کن."}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": False, "error": "پردازش فعالی برای Pause وجود ندارد."}), 400


@app.post("/api/jobs/<job_id>/resume")
def api_resume_job(job_id: str):
    with lock:
        job = jobs.get(job_id)
        proc = processes.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job پیدا نشد"}), 404
    if proc and proc.poll() is None and job.get("status") == "paused":
        try:
            if os.name == "posix":
                os.kill(proc.pid, signal.SIGCONT)
                with lock:
                    job["status"] = "running"
                    job["stage_label"] = "Running"
                    job.setdefault("logs", []).append("پردازش Resume شد.")
                persist_job(job_id)
                return jsonify({"ok": True, "mode": "continued_process"})
        except Exception:
            pass
    if job.get("status") in {"running", "queued"}:
        return jsonify({"ok": False, "error": "این job هم‌اکنون در حال اجراست."}), 400
    # Rebuild command to pick up any compatible code changes but preserve workdir/segments/cache.
    job["command"] = build_command(job)
    job["status"] = "queued"
    job["error"] = ""
    job.setdefault("logs", []).append("Resume از آخرین segment آماده شروع شد…")
    persist_job(job_id)
    thread = threading.Thread(target=run_subprocess_job, args=(job_id, job["command"]), daemon=True)
    thread.start()
    return jsonify({"ok": True, "mode": "restarted_with_resume"})


@app.post("/api/jobs/<job_id>/cancel")
def api_cancel_job(job_id: str):
    with lock:
        proc = processes.get(job_id)
        job = jobs.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job پیدا نشد"}), 404
    with lock:
        job["status"] = "cancelled"
        job["stage_label"] = "Cancelled"
        job.setdefault("logs", []).append("درخواست Cancel ثبت شد.")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            time.sleep(0.5)
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
    persist_job(job_id)
    return jsonify({"ok": True})


@app.post("/api/jobs/<job_id>/cleanup")
def api_cleanup_job(job_id: str):
    keep_cache = request.form.get("keep_cache", "1") != "0"
    with lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job پیدا نشد"}), 404
    workdir = Path(job.get("workdir", ""))
    removed = []
    for name in ("base_scene.mp4", "screenshare_segment_clips"):
        path = workdir / name
        try:
            if path.is_dir():
                shutil.rmtree(path)
                removed.append(name)
            elif path.exists():
                path.unlink()
                removed.append(name)
        except Exception:
            pass
    if not keep_cache and job.get("input_hash"):
        c = CACHE_DIR / "extracted" / job["input_hash"]
        if c.exists():
            shutil.rmtree(c, ignore_errors=True)
            removed.append("cache")
    with lock:
        job.setdefault("logs", []).append("Cleanup انجام شد: " + (", ".join(removed) if removed else "چیزی برای حذف نبود"))
    persist_job(job_id)
    return jsonify({"ok": True, "removed": removed})


@app.delete("/api/jobs/<job_id>")
def api_delete_job(job_id: str):
    with lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job پیدا نشد"}), 404
    api_cancel_job(job_id)
    for key in ("output_path", "package_path", "mp3_path"):
        try:
            p = Path(job.get(key, ""))
            if p.exists():
                p.unlink()
        except Exception:
            pass
    try:
        workdir = Path(job.get("workdir", ""))
        if workdir.exists():
            shutil.rmtree(workdir)
    except Exception:
        pass
    with lock:
        jobs.pop(job_id, None)
    return jsonify({"ok": True})


@app.get("/api/jobs/<job_id>/search")
def api_search_job(job_id: str):
    q = request.args.get("q", "").strip().lower()
    with lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job پیدا نشد"}), 404
    workdir = Path(job.get("workdir", ""))
    index = read_json(workdir / "metadata" / "search_index.json", [])
    if not q:
        results = index[:30]
    else:
        terms = [t for t in re.split(r"\s+", q) if t]
        results = []
        for item in index:
            hay = f"{item.get('title','')} {item.get('text','')} {item.get('type','')}".lower()
            if all(t in hay for t in terms):
                results.append(item)
                if len(results) >= 50:
                    break
    return jsonify({"ok": True, "query": q, "results": results})


@app.get("/api/jobs/<job_id>/chapters")
def api_chapters(job_id: str):
    with lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job پیدا نشد"}), 404
    workdir = Path(job.get("workdir", ""))
    chapters = read_json(workdir / "metadata" / "chapters.json", [])
    return jsonify({"ok": True, "chapters": chapters})


@app.get("/preview/<job_id>")
def live_preview(job_id: str):
    with lock:
        job = jobs.get(job_id)
    if not job:
        return "job not found", 404
    preview_dir = Path(job.get("preview_dir", "")) if job.get("preview_dir") else None
    if not preview_dir:
        return "preview not available", 404
    path = preview_dir / "latest.jpg"
    if not path.exists():
        return "preview not ready", 404
    response = send_file(path, mimetype="image/jpeg", conditional=False, max_age=0)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.get("/segment/<job_id>/<path:filename>")
def segment_video(job_id: str, filename: str):
    with lock:
        job = jobs.get(job_id)
    if not job:
        return "job not found", 404
    safe = secure_filename(filename)
    if safe != filename or not filename.startswith("segment_") or not filename.endswith(".mp4"):
        return "invalid segment", 400
    segments_dir = Path(job.get("segments_dir", "")) if job.get("segments_dir") else None
    if not segments_dir:
        return "segments not available", 404
    path = segments_dir / filename
    if not path.exists():
        return "segment not ready", 404
    return send_file(path, mimetype="video/mp4", conditional=True)


@app.get("/video/<job_id>")
def video(job_id: str):
    with lock:
        job = jobs.get(job_id)
    if not job:
        return "job not found", 404
    path = Path(job["output_path"])
    if not path.exists():
        return "video not ready", 404
    return send_file(path, mimetype="video/mp4", conditional=True)


@app.get("/download/<job_id>")
def download_output(job_id: str):
    with lock:
        job = jobs.get(job_id)
    if not job:
        return "job not found", 404
    path = Path(job["output_path"])
    if not path.exists():
        return "video not ready", 404
    name = f"adobe_connect_preview_{job_id}.mp4" if job.get("job_type") == "preview" else f"adobe_connect_{job_id}.mp4"
    return send_file(path, as_attachment=True, download_name=name)


@app.get("/download-package/<job_id>")
def download_package(job_id: str):
    with lock:
        job = jobs.get(job_id)
    if not job:
        return "job not found", 404
    path = Path(job.get("package_path", ""))
    if not path.exists():
        path = create_output_package(job_id) or Path("")
    if not path.exists():
        return "package not ready", 404
    return send_file(path, as_attachment=True, download_name=f"adobe_connect_class_package_{job_id}.zip")


@app.get("/download-mp3/<job_id>")
def download_mp3(job_id: str):
    with lock:
        job = jobs.get(job_id)
    if not job:
        return "job not found", 404
    path = Path(job.get("mp3_path", ""))
    if not path.exists():
        create_output_package(job_id)
        with lock:
            job = jobs.get(job_id, {})
        path = Path(job.get("mp3_path", ""))
    if not path.exists():
        return "mp3 not ready", 404
    return send_file(path, as_attachment=True, download_name=f"adobe_connect_{job_id}.mp3")


load_jobs_from_disk()

if __name__ == "__main__":
    host = os.environ.get("AC_UI_HOST", "127.0.0.1")
    port = int(os.environ.get("AC_UI_PORT", "5000"))
    print(f"\nAdobe Connect Web UI is running: http://{host}:{port}\n", flush=True)
    app.run(host=host, port=port, debug=False, threaded=True)
