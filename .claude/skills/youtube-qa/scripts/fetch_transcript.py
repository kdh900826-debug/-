"""Step 3 — 자막 추출 (설계서 §2.2 Step 3, 설계결정 A3/B5/B6).

- youtube-transcript-api v1.x (인스턴스 메서드 방식) 기준. v0.x 폴백 포함.
- 자막 선택 우선순위: ko수동 > ko자동 > en수동 > en자동 > 그 외(수동 우선)
- YouTube 자동번역(translated) 자막은 어떤 경우에도 선택하지 않는다.
- 줄별 타임스탬프는 버리고 ~60초 간격 구간 마커 [mm:ss] 만 남긴다.

출력: output/transcripts_raw/{video_id}.txt
      output/.tmp/{video_id}.meta.json 에 caption_type / lang / char_count 병합

사용:
    python fetch_transcript.py <video_id> [--interval 60] [--output-dir <경로>]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import state as st
from fetch_metadata import write_meta

MIN_CHARS = 100          # 성공 기준 (설계서 §2.2 Step 3)
DEFAULT_INTERVAL = 60    # 구간 마커 간격(초)


# --------------------------------------------------------------------------- #
# 자막 목록 조회 — v1.x / v0.x 양쪽 대응
# --------------------------------------------------------------------------- #

def list_transcripts(video_id: str):
    from youtube_transcript_api import YouTubeTranscriptApi

    if hasattr(YouTubeTranscriptApi, "list_transcripts"):   # v0.x
        return YouTubeTranscriptApi.list_transcripts(video_id)
    return YouTubeTranscriptApi().list(video_id)            # v1.x


def snippets(transcript) -> list[dict]:
    """fetch 결과를 {text, start} 리스트로 정규화한다."""
    fetched = transcript.fetch()
    if hasattr(fetched, "to_raw_data"):     # v1.x FetchedTranscript
        fetched = fetched.to_raw_data()
    out = []
    for s in fetched:
        if isinstance(s, dict):
            text, start = s.get("text", ""), s.get("start", 0.0)
        else:
            text, start = getattr(s, "text", ""), getattr(s, "start", 0.0)
        out.append({"text": text, "start": float(start or 0.0)})
    return out


# --------------------------------------------------------------------------- #
# 우선순위 선택
# --------------------------------------------------------------------------- #

def base_lang(code: str) -> str:
    return (code or "").split("-")[0].lower()


def pick(transcript_list) -> tuple[object | None, list[dict]]:
    """(선택된 transcript, 후보 목록) 반환."""
    candidates = []
    for t in transcript_list:
        # 자동번역 자막은 후보에서 제외 (설계결정 A3)
        if getattr(t, "is_translated", False):
            continue
        candidates.append(t)

    def rank(t) -> int:
        lang = base_lang(getattr(t, "language_code", ""))
        auto = bool(getattr(t, "is_generated", False))
        if lang == "ko":
            return 0 if not auto else 1
        if lang == "en":
            return 2 if not auto else 3
        return 4 if not auto else 5

    if not candidates:
        return None, []
    ordered = sorted(candidates, key=rank)
    listing = [{"lang": getattr(t, "language_code", "?"),
                "type": "auto" if getattr(t, "is_generated", False) else "manual"}
               for t in ordered]
    return ordered[0], listing


# --------------------------------------------------------------------------- #
# 구간 마커 삽입 (설계결정 B6)
# --------------------------------------------------------------------------- #

def mmss(seconds: float) -> str:
    """[mm:ss] 표기. 1시간을 넘으면 분이 60을 넘어간다(단일 형식 유지)."""
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def clean(text: str) -> str:
    text = text.replace("\n", " ")
    text = re.sub(r"\[(음악|박수|웃음|Music|Applause|Laughter)\]", " ", text, flags=re.I)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&#39;", "'", text)
    text = re.sub(r"&quot;", '"', text)
    return re.sub(r"\s+", " ", text).strip()


def build_body(snips: list[dict], interval: int) -> tuple[str, int]:
    """~interval초마다 [mm:ss] 마커를 단 본문과 순수 텍스트 글자 수를 반환."""
    lines: list[str] = []
    buf: list[str] = []
    block_start = 0.0
    chars = 0
    started = False

    for s in snips:
        txt = clean(s["text"])
        if not txt:
            continue
        if not started:
            block_start = s["start"]
            started = True
        elif s["start"] - block_start >= interval:
            lines.append(f"[{mmss(block_start)}] " + " ".join(buf))
            buf = []
            block_start = s["start"]
        buf.append(txt)
        chars += len(txt)

    if buf:
        lines.append(f"[{mmss(block_start)}] " + " ".join(buf))
    return "\n".join(lines), chars


# --------------------------------------------------------------------------- #

def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="Step 3 — 자막 추출")
    ap.add_argument("video_id")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    ap.add_argument("--output-dir")
    args = ap.parse_args()

    vid = args.video_id
    out = st.output_dir(args.output_dir)
    st.ensure_dirs(out)

    def fail(reason: str) -> int:
        st.skip(out, vid, reason)
        print(json.dumps({"ok": False, "video_id": vid, "reason": reason},
                         ensure_ascii=False, indent=2))
        return 2

    try:
        tlist = list_transcripts(vid)
    except Exception as exc:                      # noqa: BLE001 — 라이브러리 예외 다양
        name = type(exc).__name__
        readable = {
            "TranscriptsDisabled": "자막 없음 — 이 영상은 자막이 비활성화됨",
            "NoTranscriptFound": "자막 없음",
            "VideoUnavailable": "영상을 이용할 수 없음",
            "VideoUnplayable": "영상을 재생할 수 없음",
            "AgeRestricted": "연령 제한 영상 — 자막 접근 불가",
            "IpBlocked": "IP 차단 — 자막 요청이 거부됨",
            "RequestBlocked": "요청 차단 — 자막 요청이 거부됨",
            "InvalidVideoId": "유효하지 않은 video_id",
        }.get(name, f"자막 목록 조회 실패 ({name})")
        return fail(readable)

    transcript, listing = pick(tlist)
    if transcript is None:
        return fail("자막 없음 — 사용 가능한 원어 자막이 없음(자동번역 제외)")

    try:
        snips = snippets(transcript)
    except Exception as exc:                      # noqa: BLE001
        return fail(f"자막 내려받기 실패 ({type(exc).__name__})")

    body, chars = build_body(snips, args.interval)
    if chars <= MIN_CHARS:
        return fail(f"자막이 너무 짧음 ({chars}자)")

    lang = getattr(transcript, "language_code", "")
    caption_type = "auto" if getattr(transcript, "is_generated", False) else "manual"

    path = out / "transcripts_raw" / f"{vid}.txt"
    path.write_text(body, encoding="utf-8")

    write_meta(out, vid, {
        "caption_type": caption_type,
        "lang": lang,
        "char_count": chars,
        "transcript_path": str(path),
        "available_captions": listing,
        "marker_interval": args.interval,
    })
    st.set_status(out, vid, "transcribed", caption_type=caption_type, lang=lang,
                  char_count=chars)

    print(json.dumps({
        "ok": True,
        "video_id": vid,
        "caption_type": caption_type,
        "lang": lang,
        "char_count": chars,
        "line_count": body.count("\n") + 1,
        "transcript_path": str(path),
        "available_captions": listing,
        # 서브에이전트 분기 힌트 (설계서 §Step 3.5 B2)
        "outline_mode": "hierarchical" if chars > 10000 else "single-pass",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
