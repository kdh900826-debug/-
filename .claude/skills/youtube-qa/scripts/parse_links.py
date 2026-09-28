"""Step 1 — 링크 파싱 (설계서 §2.2 Step 1).

.txt 파일 → 빈 줄·주석 제거 → URL 정규화 → video_id 추출 → 중복 제거
→ output/step1_links.json

사용:
    python parse_links.py <입력.txt> [--output-dir <경로>]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import state as st

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtube-nocookie.com",
    "www.youtube-nocookie.com", "youtu.be", "www.youtu.be",
}

# /shorts/ID, /live/ID, /embed/ID, /v/ID
PATH_FORMS = re.compile(r"^/(?:shorts|live|embed|v)/([A-Za-z0-9_-]{11})")

R_PLAYLIST = "유효하지 않은 YouTube URL — 재생목록은 지원하지 않음"
R_CHANNEL = "유효하지 않은 YouTube URL — 개별 영상 링크가 필요함"
R_GENERIC = "유효하지 않은 YouTube URL"


def extract_video_id(raw: str) -> tuple[str | None, str | None]:
    """(video_id, 거부사유) 를 반환한다. 성공 시 사유는 None."""
    url = raw.strip()
    if not url:
        return None, R_GENERIC
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url

    try:
        u = urlparse(url)
    except ValueError:
        return None, R_GENERIC

    host = (u.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        return None, R_GENERIC

    path = u.path.rstrip("/") or "/"
    qs = parse_qs(u.query)

    # 채널·핸들 URL
    if path.startswith("/@") or re.match(r"^/(channel|c|user)/", path):
        return None, R_CHANNEL

    # 단축 URL — https://youtu.be/ID
    if host.endswith("youtu.be"):
        cand = path.lstrip("/").split("/")[0]
        return (cand, None) if VIDEO_ID_RE.match(cand) else (None, R_GENERIC)

    # 표준 watch — ?v=ID  (재생목록 파라미터가 붙어 있어도 v가 있으면 유효)
    if path in ("/watch", "/watch_popup"):
        cand = (qs.get("v") or [""])[0]
        if VIDEO_ID_RE.match(cand):
            return cand, None
        return None, R_PLAYLIST if "list" in qs else R_GENERIC

    # 순수 재생목록
    if path == "/playlist":
        return None, R_PLAYLIST

    # /shorts/, /live/, /embed/, /v/
    m = PATH_FORMS.match(path)
    if m:
        return m.group(1), None

    # 그 외 경로에 v= 파라미터만 붙은 형태
    cand = (qs.get("v") or [""])[0]
    if VIDEO_ID_RE.match(cand):
        return cand, None

    if "list" in qs:
        return None, R_PLAYLIST
    return None, R_GENERIC


def canonical(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def parse_file(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"입력 파일을 찾을 수 없습니다: {path}")

    valid: list[dict] = []
    invalid: list[dict] = []
    seen: set[str] = set()
    duplicates = 0

    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        vid, reason = extract_video_id(line)
        if vid is None:
            invalid.append({"url": line, "reason": reason})
            continue
        if vid in seen:
            duplicates += 1
            continue
        seen.add(vid)
        valid.append({"url": canonical(vid), "video_id": vid, "source_line": line})

    return {
        "input_file": str(path),
        "total_count": len(valid),
        "duplicate_count": duplicates,
        "valid_links": valid,
        "invalid_links": invalid,
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="Step 1 — 링크 파싱")
    ap.add_argument("input", nargs="?", default=None,
                    help="유튜브 링크 .txt (기본: input/youtube_links.txt)")
    ap.add_argument("--output-dir")
    args = ap.parse_args()

    in_path = Path(args.input) if args.input else st.PROJECT_ROOT / "input" / "youtube_links.txt"
    out = st.output_dir(args.output_dir)
    st.ensure_dirs(out)

    result = parse_file(in_path)
    (out / "step1_links.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # 성공 기준: 유효 링크 1건 이상
    if result["total_count"] == 0:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("\n[ERROR] 유효한 YouTube 링크가 0건입니다. 워크플로우를 종료합니다.",
              file=sys.stderr)
        return 1

    st.init(out, str(in_path), result["valid_links"])

    print(json.dumps({
        "total_count": result["total_count"],
        "duplicate_count": result["duplicate_count"],
        "valid_links": [{"url": v["url"], "video_id": v["video_id"]}
                        for v in result["valid_links"]],
        "invalid_links": result["invalid_links"],
        "saved_to": str(out / "step1_links.json"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
