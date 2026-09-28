"""state.json 읽기/쓰기/상태 전이 유틸 (설계서 §2.3, §3.6).

다른 스크립트가 임포트해서 쓰고, CLI로도 직접 호출할 수 있다.

CLI:
    python state.py init --input <링크파일> --links <step1_links.json>
    python state.py get <video_id>
    python state.py set <video_id> <status> [--field k=v ...]
    python state.py skip <video_id> --reason "사유"
    python state.py next            # 처리해야 할 다음 영상 1건 출력
    python state.py list            # 전체 상태 요약
    python state.py summary         # processing_summary.json 생성
    python state.py reindex         # state.json 기준으로 INDEX.md 재생성
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]

# 상태 전이 (설계서 §2.3)
STATUSES = ("pending", "transcribed", "outlined", "done", "skipped")
TERMINAL = ("done", "skipped")

# 재실행 시 재진입 지점
REENTRY = {
    "pending": "Step 2 (메타데이터 수집)",
    "transcribed": "Step 3.5 (아웃라인 추출)",
    "outlined": "Step 4 (Q&A 변환)",
    "done": "건너뜀",
    "skipped": "건너뜀",
}


# --------------------------------------------------------------------------- #
# 경로
# --------------------------------------------------------------------------- #

def output_dir(override: str | None = None) -> Path:
    if override:
        d = Path(override)
    elif os.environ.get("YTQA_OUTPUT"):
        d = Path(os.environ["YTQA_OUTPUT"])
    else:
        d = PROJECT_ROOT / "output"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path(out: Path) -> Path:
    return out / "state.json"


def ensure_dirs(out: Path) -> None:
    for sub in ("transcripts_raw", "outlines", "qa", ".tmp"):
        (out / sub).mkdir(parents=True, exist_ok=True)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 읽기/쓰기
# --------------------------------------------------------------------------- #

def load(out: Path) -> dict:
    p = state_path(out)
    if not p.exists():
        return {"input_file": None, "started_at": now(), "videos": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"state.json을 읽을 수 없습니다: {exc}")


def save(out: Path, state: dict) -> None:
    p = state_path(out)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def get_video(out: Path, video_id: str) -> dict:
    return load(out).get("videos", {}).get(video_id, {"status": "pending"})


def set_status(out: Path, video_id: str, status: str, **fields) -> dict:
    """상태를 갱신하고 갱신된 항목을 반환한다."""
    if status not in STATUSES:
        raise SystemExit(f"알 수 없는 status: {status} (허용: {', '.join(STATUSES)})")
    state = load(out)
    entry = state.setdefault("videos", {}).setdefault(video_id, {})
    # parse_links를 거치지 않고 개별 실행된 영상도 순서 목록에 편입시킨다
    order = state.setdefault("order", [])
    if video_id not in order:
        order.append(video_id)
    entry["status"] = status
    for k, v in fields.items():
        if v is not None:
            entry[k] = v
    entry["updated_at"] = now()
    save(out, state)
    return entry


def log_skip(out: Path, video_id: str, reason: str, url: str = "") -> None:
    line = f"[{now()}] {video_id}\t{reason}"
    if url:
        line += f"\t{url}"
    with (out / "skipped.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def skip(out: Path, video_id: str, reason: str, url: str = "", title: str = "") -> dict:
    log_skip(out, video_id, reason, url)
    return set_status(out, video_id, "skipped", reason=reason, url=url or None,
                      title=title or None)


# --------------------------------------------------------------------------- #
# 초기화 / 조회
# --------------------------------------------------------------------------- #

def init(out: Path, input_file: str, links: list[dict]) -> dict:
    """step1 결과로 state를 초기화한다. 기존 항목은 보존한다(누적 사용 지원)."""
    ensure_dirs(out)
    state = load(out)
    state["input_file"] = input_file
    state.setdefault("started_at", now())
    state["last_run_at"] = now()
    videos = state.setdefault("videos", {})
    order = state.setdefault("order", [])
    for link in links:
        vid = link["video_id"]
        if vid not in videos:
            videos[vid] = {"status": "pending", "url": link["url"]}
        else:
            videos[vid].setdefault("url", link["url"])
        if vid not in order:
            order.append(vid)
    save(out, state)
    return state


def pending_list(out: Path) -> list[tuple[str, dict]]:
    state = load(out)
    videos = state.get("videos", {})
    order = state.get("order") or list(videos)
    return [(v, videos[v]) for v in order
            if v in videos and videos[v].get("status") not in TERMINAL]


def is_first_video(out: Path) -> bool:
    """아직 done 처리된 영상이 하나도 없으면 True (승인 게이트 판정용)."""
    return not any(e.get("status") == "done"
                   for e in load(out).get("videos", {}).values())


# --------------------------------------------------------------------------- #
# INDEX.md 재생성 (설계서 §2.2 Step 5, E4)
# --------------------------------------------------------------------------- #

def rebuild_index(out: Path) -> Path:
    state = load(out)
    videos = state.get("videos", {})
    order = state.get("order") or list(videos)

    rows = []
    for vid in order:
        e = videos.get(vid, {})
        if e.get("status") != "done":
            continue
        rel = e.get("file", "")
        # 표에서는 |, 링크 표시부에서는 []가 구조를 깨뜨린다
        title = (e.get("title") or vid).replace("|", "\\|")
        link = f"[{title.replace('[', '(').replace(']', ')')}]({rel})" if rel else title
        rows.append("| {} | {} | {} | {} | {} |".format(
            link,
            (e.get("channel") or "").replace("|", "\\|"),
            (e.get("topics") or "").replace("|", "\\|"),
            e.get("qa_count", ""),
            e.get("processed_at", ""),
        ))

    lines = [
        "# 처리된 영상 색인",
        "",
        f"- 총 {len(rows)}건 · 최종 갱신 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "| 제목 | 채널 | 주제 | Q수 | 처리일 |",
        "|------|------|------|-----|--------|",
    ]
    lines.extend(rows if rows else ["| _(아직 없음)_ |  |  |  |  |"])
    lines.append("")

    p = out / "INDEX.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# 처리 요약 (설계서 §2.5)
# --------------------------------------------------------------------------- #

def build_summary(out: Path) -> dict:
    state = load(out)
    videos = state.get("videos", {})
    order = state.get("order") or list(videos)

    invalid = 0
    links_json = out / "step1_links.json"
    if links_json.exists():
        try:
            invalid = len(json.loads(links_json.read_text(encoding="utf-8"))
                          .get("invalid_links", []))
        except (json.JSONDecodeError, OSError):
            invalid = 0

    summary = {
        "total": len(videos),
        "success": sum(1 for e in videos.values() if e.get("status") == "done"),
        "skipped": [
            {"video_id": v, "reason": videos[v].get("reason", "")}
            for v in order
            if v in videos and videos[v].get("status") == "skipped"
        ],
        "incomplete": [
            {"video_id": v, "status": videos[v].get("status")}
            for v in order
            if v in videos and videos[v].get("status") not in TERMINAL
        ],
        "invalid_links": invalid,
        "generated_at": now(),
    }
    (out / "processing_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="state.json 유틸")
    ap.add_argument("--output-dir")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("--input", required=True)
    p_init.add_argument("--links", required=True, help="step1_links.json 경로")

    p_get = sub.add_parser("get")
    p_get.add_argument("video_id")

    p_set = sub.add_parser("set")
    p_set.add_argument("video_id")
    p_set.add_argument("status", choices=STATUSES)
    p_set.add_argument("--field", action="append", default=[], metavar="k=v")

    p_skip = sub.add_parser("skip")
    p_skip.add_argument("video_id")
    p_skip.add_argument("--reason", required=True)
    p_skip.add_argument("--url", default="")

    sub.add_parser("next")
    sub.add_parser("list")
    sub.add_parser("summary")
    sub.add_parser("reindex")

    args = ap.parse_args()
    out = output_dir(args.output_dir)

    if args.cmd == "init":
        data = json.loads(Path(args.links).read_text(encoding="utf-8"))
        state = init(out, args.input, data.get("valid_links", []))
        todo = pending_list(out)
        print(json.dumps({
            "total": len(state["videos"]),
            "to_process": [v for v, _ in todo],
            "already_done": [v for v, e in state["videos"].items()
                             if e.get("status") == "done"],
        }, ensure_ascii=False, indent=2))

    elif args.cmd == "get":
        print(json.dumps(get_video(out, args.video_id), ensure_ascii=False, indent=2))

    elif args.cmd == "set":
        fields = {}
        for kv in args.field:
            k, _, v = kv.partition("=")
            fields[k] = v
        print(json.dumps(set_status(out, args.video_id, args.status, **fields),
                         ensure_ascii=False, indent=2))

    elif args.cmd == "skip":
        print(json.dumps(skip(out, args.video_id, args.reason, args.url),
                         ensure_ascii=False, indent=2))

    elif args.cmd == "next":
        todo = pending_list(out)
        if not todo:
            print(json.dumps({"done": True}, ensure_ascii=False))
        else:
            vid, entry = todo[0]
            print(json.dumps({
                "done": False,
                "video_id": vid,
                "url": entry.get("url"),
                "status": entry.get("status", "pending"),
                "resume_at": REENTRY.get(entry.get("status", "pending")),
                "remaining": len(todo),
                "is_first_video": is_first_video(out),
            }, ensure_ascii=False, indent=2))

    elif args.cmd == "list":
        state = load(out)
        order = state.get("order") or list(state.get("videos", {}))
        for vid in order:
            e = state["videos"].get(vid, {})
            print(f"{vid}\t{e.get('status','?'):12}\t{e.get('title','')}")

    elif args.cmd == "summary":
        print(json.dumps(build_summary(out), ensure_ascii=False, indent=2))

    elif args.cmd == "reindex":
        print(f"INDEX 재생성: {rebuild_index(out)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
