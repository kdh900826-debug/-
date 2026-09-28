"""Step 3.5 / 4.5 의 규칙 기반 검증 유틸.

설계서 §2.2가 요구하는 "규칙 기반 검증"(아웃라인 항목 수·시점 형식, Q&A 밀도·
구간 커버리지)을 기계적으로 수행한다. Step 4.5의 의미 수준 대조는 서브에이전트가
이 결과를 참고해 판단한다 — 이 스크립트는 판정을 대신하지 않는다.

사용:
    python verify.py outline <video_id> [--output-dir <경로>]
    python verify.py qa      <video_id> [--qa-file <경로>] [--output-dir <경로>]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import state as st
from fetch_metadata import load_meta
from save_qa import OUTLINE_ITEM, Q_HEAD, TS_BARE, TS_LINK, to_seconds

MIN_TOPICS = 3
MARKER = re.compile(r"^\[(\d{1,3}):(\d{2})(?::(\d{2}))?\]", re.M)


def transcript_span(out: Path, vid: str) -> tuple[int, int]:
    p = out / "transcripts_raw" / f"{vid}.txt"
    if not p.exists():
        return 0, 0
    marks = [to_seconds(m) for m in MARKER.finditer(p.read_text(encoding="utf-8"))]
    return (min(marks), max(marks)) if marks else (0, 0)


def dense(text: str) -> int:
    """공백을 뺀 실질 글자 수. 분량 비율은 반드시 이 기준끼리만 비교한다."""
    return len(re.sub(r"\s", "", text))


def transcript_dense(out: Path, vid: str) -> int:
    p = out / "transcripts_raw" / f"{vid}.txt"
    if not p.exists():
        return 0
    return dense(MARKER.sub("", p.read_text(encoding="utf-8")))


def body_dense(text: str) -> int:
    """Q&A 본문의 실질 글자 수 — 마크다운 장식과 딥링크는 내용이 아니므로 제외한다."""
    text = re.sub(r"\[[^\]]*\]\([^)]*\)", "", text)   # [00:00](https://...)
    text = re.sub(r"^\s*\*\*\s*Q\s*\d+\s*[.．]\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*A[.．]\s*", "", text, flags=re.M)
    text = text.replace("**", "").replace("·", "")
    return dense(text)


def read_outline(out: Path, vid: str) -> list[tuple[int, str]]:
    p = out / "outlines" / f"{vid}.md"
    if not p.exists():
        return []
    return [(to_seconds(m), m.group(4).strip())
            for m in OUTLINE_ITEM.finditer(p.read_text(encoding="utf-8"))]


def read_qa(path: Path) -> list[tuple[int, int | None, str]]:
    """(Q번호, 시점초 or None, 질문줄) 목록."""
    rows = []
    n = 0
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not Q_HEAD.match(line):
            continue
        n += 1
        m = TS_LINK.search(line) or TS_BARE.search(line)
        rows.append((n, to_seconds(m) if m else None, line.strip()))
    return rows


# --------------------------------------------------------------------------- #

def check_outline(out: Path, vid: str) -> dict:
    path = out / "outlines" / f"{vid}.md"
    problems: list[str] = []

    if not path.exists():
        return {"ok": False, "problems": [f"아웃라인 파일이 없습니다: {path}"]}

    text = path.read_text(encoding="utf-8")
    items = read_outline(out, vid)
    bullets = len([l for l in text.splitlines() if l.strip().startswith(("-", "*"))])

    if len(items) < MIN_TOPICS:
        problems.append(f"주제가 {len(items)}개 — 최소 {MIN_TOPICS}개 필요")
    if bullets > len(items):
        problems.append(f"시점 형식([mm:ss])을 파싱하지 못한 항목 {bullets - len(items)}개")

    secs = [s for s, _ in items]
    if secs != sorted(secs):
        problems.append("주제 시점이 시간 순서가 아닙니다")
    if len(set(secs)) != len(secs):
        problems.append("시점이 중복된 주제가 있습니다")
    for s, name in items:
        if not name:
            problems.append(f"[{s}s] 항목에 주제명이 없습니다")

    lo, hi = transcript_span(out, vid)
    if hi and secs:
        if secs[-1] > hi + 60:
            problems.append(f"마지막 주제 시점({secs[-1]}s)이 대본 범위({hi}s)를 벗어납니다")
        # 대본 뒷부분이 통째로 비면 아웃라인이 조기 종료된 것
        if hi - secs[-1] > max(300, hi * 0.25):
            problems.append(
                f"대본 마지막 {hi - secs[-1]}초 구간에 주제가 없습니다 — 아웃라인이 조기 종료된 것으로 보입니다")

    return {"ok": not problems, "video_id": vid, "topic_count": len(items),
            "transcript_span_sec": hi, "problems": problems,
            "topics": [{"at": s, "title": t} for s, t in items]}


def check_qa(out: Path, vid: str, qa_file: Path | None) -> dict:
    path = qa_file or out / ".tmp" / f"{vid}_qa.md"
    if not path.exists():
        return {"ok": False, "problems": [f"Q&A 파일이 없습니다: {path}"]}

    problems: list[str] = []
    notes: list[str] = []

    rows = read_qa(path)
    if not rows:
        return {"ok": False, "problems": ["`**Qn.` 형식의 질문을 찾지 못했습니다"]}

    no_ts = [n for n, s, _ in rows if s is None]
    if no_ts:
        problems.append(f"딥링크 시점이 없는 Q: {no_ts}")

    secs = [s for _, s, _ in rows if s is not None]
    if secs != sorted(secs):
        problems.append("Q 시점이 영상 흐름 순서가 아닙니다 (설계서 A2 — 재정렬 금지)")

    # 정보 밀도 (설계서 §1.5 — 9,000자당 Q 15~25개)
    meta = load_meta(out, vid)
    chars = meta.get("char_count") or 0
    density = None
    if chars:
        density = round(len(rows) / (chars / 9000), 1)
        if density < 12:
            problems.append(
                f"정보 밀도 미달 — 9,000자당 Q {density}개 (기준 15~25). "
                f"대본 {chars}자에 Q {len(rows)}개는 과도한 압축입니다")
        elif density > 32:
            notes.append(f"9,000자당 Q {density}개 — 기준(15~25)보다 잘게 쪼개졌습니다")

    # 분량 비율 — 양쪽 모두 "공백·마크업 제외 실질 글자 수"로 맞춰 비교한다
    src_chars = transcript_dense(out, vid)
    body_chars = body_dense(path.read_text(encoding="utf-8"))
    ratio = None
    if src_chars:
        pct = round(body_chars / src_chars * 100)
        ratio = round(body_chars / src_chars, 2)
        if pct < 40:
            problems.append(f"본문 분량이 원문 대비 {pct}% — 기준 60~70%에 크게 못 미칩니다")
        elif pct < 55:
            notes.append(f"본문 분량이 원문 대비 {pct}% — 기준(60~70%)보다 얇습니다")

    # 구간 커버리지 — 아웃라인 주제 구간에 Q가 하나도 없으면 누락 후보
    topics = read_outline(out, vid)
    uncovered = []
    if topics and secs:
        _, span = transcript_span(out, vid)
        bounds = [t[0] for t in topics] + [max(span, secs[-1]) + 1]
        for i, (start, name) in enumerate(topics):
            end = bounds[i + 1]
            if not any(start - 30 <= s < end for s in secs):
                uncovered.append({"at": start, "title": name})
        if uncovered:
            problems.append(f"Q가 하나도 없는 아웃라인 구간 {len(uncovered)}개")

    return {"ok": not problems, "video_id": vid, "qa_count": len(rows),
            "transcript_chars": chars,
            "source_chars_ns": src_chars, "body_chars_ns": body_chars,
            "length_ratio": ratio, "density_per_9k": density,
            "uncovered_topics": uncovered,
            "problems": problems, "notes": notes}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="아웃라인 / Q&A 규칙 기반 검증")
    ap.add_argument("target", choices=["outline", "qa"])
    ap.add_argument("video_id")
    ap.add_argument("--qa-file")
    ap.add_argument("--output-dir")
    args = ap.parse_args()

    out = st.output_dir(args.output_dir)
    if args.target == "outline":
        result = check_outline(out, args.video_id)
    else:
        result = check_qa(out, args.video_id,
                          Path(args.qa_file) if args.qa_file else None)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 3


if __name__ == "__main__":
    raise SystemExit(main())
