"""Step 2 — 메타데이터 수집 (설계서 §2.2 Step 2, 설계결정 D1).

폴백 체인:
    1차 oEmbed (인증 불필요, 수백 바이트) → title, channel
    2차 yt-dlp                            → duration (선택적 의존성, 실패 허용)
    전부 실패                              → title = video_id 로 대체하고 진행

어떤 실패도 워크플로우를 멈추지 않는다.

출력: output/.tmp/{video_id}.meta.json  (+ stdout JSON)

사용:
    python fetch_metadata.py <video_id> [--output-dir <경로>]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import state as st

OEMBED = ("https://www.youtube.com/oembed"
          "?url=https://www.youtube.com/watch?v={vid}&format=json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def hhmmss(seconds) -> str:
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def try_oembed(video_id: str, notes: list[str]) -> dict:
    req = urllib.request.Request(OEMBED.format(vid=video_id),
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        return {"title": (data.get("title") or "").strip(),
                "channel": (data.get("author_name") or "").strip()}
    except (urllib.error.URLError, json.JSONDecodeError, OSError, TimeoutError) as exc:
        notes.append(f"oEmbed 실패: {type(exc).__name__}: {exc}")
        return {}


def try_ytdlp(video_id: str, notes: list[str]) -> dict:
    """yt-dlp는 선택적 의존성. 없거나 봇 차단되면 조용히 포기한다."""
    cmd = [sys.executable, "-m", "yt_dlp", "--dump-json", "--skip-download",
           "--no-warnings", "--no-playlist",
           f"https://www.youtube.com/watch?v={video_id}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=90)
    except (OSError, subprocess.TimeoutExpired) as exc:
        notes.append(f"yt-dlp 실행 불가: {type(exc).__name__}")
        return {}
    if proc.returncode != 0 or not proc.stdout.strip():
        head = (proc.stderr or "").strip().splitlines()
        notes.append("yt-dlp 실패: " + (head[-1][:200] if head else f"exit {proc.returncode}"))
        return {}
    try:
        d = json.loads(proc.stdout.splitlines()[0])
    except (json.JSONDecodeError, IndexError):
        notes.append("yt-dlp 출력 파싱 실패")
        return {}
    return {"title": (d.get("title") or "").strip(),
            "channel": (d.get("channel") or d.get("uploader") or "").strip(),
            "duration": hhmmss(d.get("duration"))}


def fetch(video_id: str) -> dict:
    notes: list[str] = []
    meta = {"video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": "", "channel": "", "duration": ""}

    primary = try_oembed(video_id, notes)
    if primary.get("title"):
        # 1차 성공 → yt-dlp는 duration만 시도. 실패해도 진행.
        meta.update(primary)
        meta["source"] = "oembed"
        extra = try_ytdlp(video_id, notes)
        if extra.get("duration"):
            meta["duration"] = extra["duration"]
    else:
        # 1차 실패 → yt-dlp로 전 필드 시도
        extra = try_ytdlp(video_id, notes)
        if extra.get("title"):
            meta.update({k: v for k, v in extra.items() if v})
            meta["source"] = "yt-dlp"
        else:
            meta["title"] = video_id  # 최종 폴백: 제목 자리에 video_id
            meta["source"] = "fallback"
            notes.append("메타데이터 수집 전면 실패 — title을 video_id로 대체")

    meta["notes"] = notes
    return meta


def meta_path(out: Path, video_id: str) -> Path:
    return out / ".tmp" / f"{video_id}.meta.json"


def load_meta(out: Path, video_id: str) -> dict:
    p = meta_path(out, video_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def write_meta(out: Path, video_id: str, patch: dict) -> dict:
    """기존 메타에 병합 저장한다 (Step 3이 자막 메타를 덧붙일 수 있게)."""
    st.ensure_dirs(out)
    meta = load_meta(out, video_id)
    meta.update(patch)
    meta_path(out, video_id).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="Step 2 — 메타데이터 수집")
    ap.add_argument("video_id")
    ap.add_argument("--output-dir")
    ap.add_argument("--no-ytdlp", action="store_true", help="duration 수집 생략")
    args = ap.parse_args()

    out = st.output_dir(args.output_dir)

    if args.no_ytdlp:
        notes: list[str] = []
        meta = {"video_id": args.video_id,
                "url": f"https://www.youtube.com/watch?v={args.video_id}",
                "title": "", "channel": "", "duration": "", "source": "oembed"}
        meta.update(try_oembed(args.video_id, notes))
        if not meta.get("title"):
            meta["title"] = args.video_id
            meta["source"] = "fallback"
        meta["notes"] = notes
    else:
        meta = fetch(args.video_id)

    write_meta(out, args.video_id, meta)
    st.set_status(out, args.video_id, st.get_video(out, args.video_id).get("status", "pending"),
                  title=meta["title"], channel=meta.get("channel") or None,
                  url=meta["url"])

    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
