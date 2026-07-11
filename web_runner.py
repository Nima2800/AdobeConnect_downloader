#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small web-runner wrapper for adobe_connect_downloader9.py.

The original script remains untouched. This wrapper adds element filtering so the
Flask UI can decide which visual parts should appear in the rendered class video.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import time
from pathlib import Path
from typing import Optional, Sequence, Set, Tuple

from adobe_connect_downloader9 import AdobeConnectRenderer, die, ensure_binary, run


VALID_ELEMENTS = {"pdf", "screen", "chat", "pointer", "annotations", "audio"}


def parse_size(size: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    if not size:
        return None, None
    import re

    m = re.fullmatch(r"(\d+)x(\d+)", size.strip().lower())
    if not m:
        die("فرمت سایز باید مثل 1280x720 باشد.")
    return int(m.group(1)), int(m.group(2))


class WebAdobeConnectRenderer(AdobeConnectRenderer):
    def __init__(self, *args, include_elements: Set[str], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.include_elements = include_elements

    def parse_recording(self) -> None:
        super().parse_recording()

        if "pdf" not in self.include_elements:
            self.documents = {}
            self.content_events = []
            self.mementos = []

        if "screen" not in self.include_elements:
            self.video_segments = []

        if "chat" not in self.include_elements:
            self.chat_messages = []

        if "pointer" not in self.include_elements:
            self.pointer_events = []

        if "annotations" not in self.include_elements:
            self.annotations = []
        if hasattr(self, "_build_timeline_indexes"):
            self._build_timeline_indexes()

    def prepare_documents(self) -> None:
        if "pdf" not in self.include_elements:
            return
        return super().prepare_documents()

    def export_pdf_assets(self):
        if "pdf" not in self.include_elements:
            return []
        return super().export_pdf_assets()

    def build_audio_track(self) -> Path:
        if "audio" in self.include_elements:
            return super().build_audio_track()

        # Keep a silent track so the final MP4 stays broadly compatible with players.
        audio_out = self.workdir / "silent_audio.m4a"
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", f"{max(0.1, self.duration_s):.3f}",
            "-c:a", "aac", "-b:a", self.audio_bitrate,
            str(audio_out),
        ]
        run(cmd)
        return audio_out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Web UI runner for Adobe Connect renderer")
    parser.add_argument("--url", default="", help="Adobe Connect class/recording URL with session")
    parser.add_argument("--zip", dest="offline_zip", default="", help="Local recording ZIP")
    parser.add_argument("--output", required=True, help="Output MP4 path")
    parser.add_argument("--workdir", required=True, help="Job work directory")
    parser.add_argument("--fps", type=float, default=1.0)
    parser.add_argument("--screen-fps", type=float, default=6.0)
    parser.add_argument("--segment-seconds", type=float, default=600.0, help="Seconds per playable chunk; 600 means 10-minute chunks.")
    parser.add_argument("--first-segment-seconds", type=float, default=600.0, help="Length of the first playable chunk; 600 means the first 10 minutes of class.")
    parser.add_argument("--clip-start", type=float, default=0.0, help="Preview/full render start second.")
    parser.add_argument("--clip-duration", type=float, default=0.0, help="Preview/full render duration; 0 means full remaining duration.")
    parser.add_argument("--encoder", default="auto", help="auto/cpu/nvidia")
    parser.add_argument("--workers", type=int, default=1, help="Reserved parallel renderer worker count; safe default is 1.")
    parser.add_argument("--cache-dir", default="", help="Persistent cache root.")
    parser.add_argument("--input-hash", default="", help="SHA-256 hash of input ZIP for cache lookup.")
    parser.add_argument("--preset", default="ultrafast")
    parser.add_argument("--crf", type=int, default=31, help="x264 CRF; higher is faster/smaller/lower quality.")
    parser.add_argument("--audio-bitrate", default="96k", help="AAC audio bitrate for final/segment muxing.")
    parser.add_argument("--size", default="854x480")
    parser.add_argument("--elements", default="pdf,screen,chat,pointer,annotations,audio")
    parser.add_argument("--skip-download-assets", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    include = {x.strip().lower() for x in args.elements.split(",") if x.strip()}
    include = include & VALID_ELEMENTS
    if not include:
        include = set(VALID_ELEMENTS)

    class_url = args.url.strip()
    offline_zip = Path(args.offline_zip).resolve() if args.offline_zip else None
    if not class_url and offline_zip:
        class_url = "https://offline.local/recording/"
        args.skip_download_assets = True
    if not class_url:
        die("لینک کلاس یا فایل ZIP لازم است.")

    if args.cache_dir:
        import os
        os.environ["AC_CACHE_DIR"] = args.cache_dir
    if args.input_hash:
        import os
        os.environ["AC_INPUT_HASH"] = args.input_hash

    width, height = parse_size(args.size)
    renderer = WebAdobeConnectRenderer(
        class_url=class_url,
        output=Path(args.output).resolve(),
        workdir=Path(args.workdir).resolve(),
        fps=max(0.5, args.fps),
        width=width,
        height=height,
        keep=True,
        offline_zip=offline_zip,
        skip_download_assets=args.skip_download_assets,
        pdf_output_dir=Path(args.output).resolve().with_suffix("").with_name(Path(args.output).resolve().stem + "_pdfs"),
        ffmpeg_preset=args.preset,
        video_crf=max(18, min(35, int(args.crf))),
        audio_bitrate=args.audio_bitrate,
        screen_fps=args.screen_fps,
        segment_seconds=max(0.0, float(args.segment_seconds or 0.0)),
        first_segment_seconds=max(0.0, float(args.first_segment_seconds or 0.0)),
        clip_start_s=max(0.0, float(args.clip_start or 0.0)),
        clip_duration_s=max(0.0, float(args.clip_duration or 0.0)),
        encoder=args.encoder,
        workers=max(1, int(args.workers or 1)),
        include_elements=include,
    )

    started = time.time()
    try:
        renderer.run_all()
    finally:
        # keep=True so logs/intermediates remain available for debugging from UI.
        renderer.cleanup()
    print(f"WEB_RUNNER_DONE elapsed={time.time() - started:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
