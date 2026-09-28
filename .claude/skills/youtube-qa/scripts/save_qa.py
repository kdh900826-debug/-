"""Step 5 — 파일 조립·저장 (설계서 §2.2 Step 5, 설계결정 C3/E2/E3/E4).

LLM은 Q&A 본문만 만들고, 결정론적 조립은 전부 이 스크립트가 한다.
  · YAML frontmatter 생성      · 사람용 헤더 조립
  · Q 번호 순차 재부여          · 딥링크 URL 재생성(표시된 mm:ss 기준)
  · 파일명 sanitize             · INDEX.md 재생성      · state.json 기록

사용:
    python save_qa.py <video_id> [--qa-file <경로>] [--output-dir <경로>] [--keep-tmp]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

import state as st
from fetch_metadata import load_meta

TITLE_MAXLEN = 50
# 제거 — 지워도 단어 경계가 무너지지 않는 문자
DROP_CHARS = r'["?*\x00-\x1f]'
# 공백 치환 — 지우면 양옆 단어가 들러붙는 문자.
#   구분자류(/ \ : | < >): "26/8/9" → "2689"가 되는 것을 막는다
#   괄호류(()[]{}#): "신호(김효진 박사)" → "신호김효진_박사"가 되는 것을 막는다.
#     괄호는 파일시스템에는 합법이지만 INDEX.md의 [제목](경로) 링크를 끊는다
SPACE_CHARS = r"[/\\:|<>()\[\]{}#]"

Q_HEAD = re.compile(r"^\s*\*\*\s*Q\s*\d+\s*[.．]\s*", re.M)
TS_LINK = re.compile(r"\[(\d{1,3}):(\d{2})(?::(\d{2}))?\]\((?:[^)]*)\)")
TS_BARE = re.compile(r"\[(\d{1,3}):(\d{2})(?::(\d{2}))?\](?!\()")
OUTLINE_ITEM = re.compile(r"^\s*[-*]\s*\[(\d{1,3}):(\d{2})(?::(\d{2}))?\]\s*(.+?)\s*$", re.M)


# --------------------------------------------------------------------------- #
# 파일명 (설계결정 E2)
# --------------------------------------------------------------------------- #

def sanitize_title(title: str) -> str:
    t = re.sub(DROP_CHARS, "", title or "")
    t = re.sub(SPACE_CHARS, " ", t)
    t = re.sub(r"\s+", "_", t.strip())
    t = t.strip("._") or "untitled"
    return t[:TITLE_MAXLEN].rstrip("._") or "untitled"


def target_name(title: str, video_id: str) -> str:
    return f"{sanitize_title(title)}__{video_id}.md"


# --------------------------------------------------------------------------- #
# 본문 정규화
# --------------------------------------------------------------------------- #

def to_seconds(m: re.Match) -> int:
    """`[mm:ss]` / `[h:mm:ss]` 매치를 초로 바꾼다. 앞 3개 그룹만 본다."""
    g = (list(m.groups()) + [None, None, None])[:3]
    a, b, c = g
    return (int(a) * 3600 + int(b) * 60 + int(c)) if c else (int(a) * 60 + int(b))


def label(seconds: int) -> str:
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def normalize_body(body: str, video_id: str) -> tuple[str, int, list[str]]:
    """Q 번호를 순차 재부여하고 딥링크를 재생성한다. (본문, Q수, 경고) 반환."""
    warnings: list[str] = []

    # 서브에이전트가 헤더/frontmatter를 섞어 보냈다면 제거 (조립은 스크립트 담당).
    # 본문은 반드시 `**Qn.`으로 시작해야 하므로, 앞머리의 제목·구분선만 걷어낸다.
    body = re.sub(r"\A\s*---\r?\n.*?\r?\n---\r?\n", "", body, flags=re.S).strip()
    for _ in range(4):
        stripped = re.sub(r"\A(?:#{1,6} .*|-{3,})\r?\n", "", body).strip()
        if stripped == body:
            break
        body = stripped

    def fix_link(m: re.Match) -> str:
        sec = to_seconds(m)
        return f"[{label(sec)}](https://youtu.be/{video_id}?t={sec})"

    lines = body.split("\n")
    counter = 0
    for i, line in enumerate(lines):
        if not Q_HEAD.match(line):
            continue
        counter += 1
        line = Q_HEAD.sub(f"**Q{counter}. ", line, count=1)
        if TS_LINK.search(line):
            line = TS_LINK.sub(fix_link, line, count=1)
        elif TS_BARE.search(line):
            line = TS_BARE.sub(fix_link, line, count=1)
        else:
            warnings.append(f"Q{counter}: 딥링크 시점이 없습니다")
        lines[i] = line

    return "\n".join(lines).strip() + "\n", counter, warnings


# --------------------------------------------------------------------------- #
# 아웃라인 → INDEX 주제 열 (설계결정 E4)
# --------------------------------------------------------------------------- #

def outline_items(out: Path, video_id: str) -> list[tuple[int, str]]:
    p = out / "outlines" / f"{video_id}.md"
    if not p.exists():
        return []
    return [(to_seconds(m), m.group(4).strip())
            for m in OUTLINE_ITEM.finditer(p.read_text(encoding="utf-8"))]


def topics_line(items: list[tuple[int, str]], limit: int = 5, maxlen: int = 90) -> str:
    names = [t for _, t in items]
    # "영상 소개" 성격의 첫 항목은 정보량이 없어 건너뛴다
    if names and re.search(r"(소개|인사|오프닝|도입|intro)", names[0], re.I):
        names = names[1:]
    line = ", ".join(names[:limit])
    return line[: maxlen - 1] + "…" if len(line) > maxlen else line


# --------------------------------------------------------------------------- #
# 조립
# --------------------------------------------------------------------------- #

def y(v: str) -> str:
    return json.dumps(v or "", ensure_ascii=False)


def assemble(meta: dict, body: str, qa_count: int, processed_at: str) -> str:
    vid = meta["video_id"]
    url = meta.get("url") or f"https://www.youtube.com/watch?v={vid}"
    title = meta.get("title") or vid
    cap = meta.get("caption_type", "")
    lang = meta.get("lang", "")
    cap_ko = {"auto": "자동생성", "manual": "수동"}.get(cap, cap or "알 수 없음")

    fm = [
        "---",
        f"video_id: {y(vid)}",
        f"url: {y(url)}",
        f"title: {y(title)}",
        f"channel: {y(meta.get('channel', ''))}",
        f"duration: {y(meta.get('duration', ''))}",
        f"caption_type: {y(cap)}",
        f"lang: {y(lang)}",
        f"processed_at: {processed_at}",
        f"qa_count: {qa_count}",
        "---",
        "",
    ]

    head = [
        f"# {title}",
        "",
        f"- 원본 링크: {url}",
        f"- 채널명: {meta.get('channel') or '-'}",
        f"- 영상 길이: {meta.get('duration') or '-'}",
        f"- 자막: {cap_ko}" + (f" ({lang})" if lang else ""),
        "",
        "---",
        "",
        "## Q&A",
        "",
        "",
    ]
    return "\n".join(fm + head) + body


# --------------------------------------------------------------------------- #

def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="Step 5 — 파일 조립·저장")
    ap.add_argument("video_id")
    ap.add_argument("--qa-file", help="기본: output/.tmp/{video_id}_qa.md")
    ap.add_argument("--output-dir")
    ap.add_argument("--keep-tmp", action="store_true")
    args = ap.parse_args()

    vid = args.video_id
    out = st.output_dir(args.output_dir)
    st.ensure_dirs(out)

    def fail(reason: str) -> int:
        st.skip(out, vid, reason)
        print(json.dumps({"ok": False, "video_id": vid, "reason": reason},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    qa_path = Path(args.qa_file) if args.qa_file else out / ".tmp" / f"{vid}_qa.md"
    if not qa_path.exists():
        return fail(f"Q&A 임시 파일이 없습니다: {qa_path}")

    raw = qa_path.read_text(encoding="utf-8").strip()
    if not raw:
        return fail("Q&A 임시 파일이 비어 있습니다")

    body, qa_count, warnings = normalize_body(raw, vid)
    if qa_count == 0:
        return fail("Q&A 본문에서 `**Qn.` 형식의 질문을 찾지 못했습니다")

    meta = load_meta(out, vid)
    if not meta.get("video_id"):
        meta.setdefault("video_id", vid)
    if not meta.get("title"):
        meta["title"] = st.get_video(out, vid).get("title") or vid

    processed_at = date.today().isoformat()
    doc = assemble(meta, body, qa_count, processed_at)

    fname = target_name(meta["title"], vid)
    dest = out / "qa" / fname
    dest.write_text(doc, encoding="utf-8")          # 재실행 시 덮어쓰기 (E2)

    # 검증 — 파일 존재·크기·frontmatter 필수 필드
    if not dest.exists() or dest.stat().st_size == 0:
        return fail("저장 실패 — 파일이 비어 있습니다")
    head = dest.read_text(encoding="utf-8")[:2000]
    missing = [k for k in ("video_id", "url", "title", "processed_at", "qa_count")
               if f"\n{k}:" not in "\n" + head]
    if missing:
        return fail(f"frontmatter 필수 필드 누락: {', '.join(missing)}")

    items = outline_items(out, vid)
    st.set_status(out, vid, "done",
                  title=meta["title"],
                  channel=meta.get("channel", ""),
                  duration=meta.get("duration", ""),
                  caption_type=meta.get("caption_type", ""),
                  lang=meta.get("lang", ""),
                  qa_count=qa_count,
                  outline_count=len(items),
                  topics=topics_line(items),
                  file=f"qa/{fname}",
                  processed_at=processed_at,
                  reason=None)
    index = st.rebuild_index(out)

    if not args.keep_tmp:
        qa_path.unlink(missing_ok=True)

    print(json.dumps({
        "ok": True,
        "video_id": vid,
        "file": str(dest),
        "qa_count": qa_count,
        "outline_count": len(items),
        "index": str(index),
        "warnings": warnings,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
