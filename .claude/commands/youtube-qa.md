---
description: 유튜브 링크 목록(.txt)을 Q&A 마크다운으로 변환한다. 인자 생략 시 input/youtube_links.txt
argument-hint: [입력파일경로]
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent
---

# /youtube-qa

입력 파일: `$1` (비어 있으면 `input/youtube_links.txt`)

[CLAUDE.md](../../CLAUDE.md)의 워크플로우를 그대로 실행한다. 아래는 실행 체크리스트다.

---

## 0. 환경 점검

```bash
.venv\Scripts\python.exe -c "import youtube_transcript_api; print('ok')"
```

실패 → 설치 명령을 안내하고 **중단**한다. 워크플로우를 시작하지 마라.

## 1. 링크 파싱 (1회)

```bash
.venv\Scripts\python.exe .claude\skills\youtube-qa\scripts\parse_links.py "<입력파일>"
```

- exit 1 → 종료. 무효 링크는 사유와 함께 사용자에게 보고한다.
- 처리 대상 목록과 이미 `done`인 영상 수를 사용자에게 먼저 알린다.

## 2. 영상별 순차 루프 (동시 1건)

각 회차마다:

1. `state.py next` → `video_id`, `resume_at`, `is_first_video` 확보. `done: true`면 루프 종료
2. `fetch_metadata.py <id>` — 실패해도 진행
3. `fetch_transcript.py <id>` — `ok: false`면 이미 스킵 처리됨. 다음 영상으로
4. **서브에이전트 1개 스폰** — `SKILL.md`를 읽고 Step 3.5 → 4 → 4.5 수행.
   대본 경로·caption_type·lang·char_count·outline_mode를 전달한다.
   **대본 파일을 메인에서 열지 마라**
5. `coverage: fail` → `state.py skip <id> --reason "<사유>"` 후 다음 영상
6. `save_qa.py <id>` → md 저장·INDEX 갱신·state 기록
7. **첫 영상이면 여기서 멈춘다** (아래 3번)

## 3. 승인 게이트 — 첫 영상 완료 후

사용자에게 보여줄 것:

- 저장 경로
- Q 개수 / 밀도(9,000자당 Q수, 원문 대비 분량 %)
- Q&A 앞부분 3~4개 발췌

조정 가능: **정보 밀도 · Q의 어조 · 딥링크 표기 방식**

조정 요청 → `SKILL.md` 갱신 → 해당 영상 state를 `outlined`로 되돌림 → 재스폰 → 재저장 →
다시 확인. **승인 전까지 2번째 영상에 손대지 않는다.**

## 4. 종료

```bash
.venv\Scripts\python.exe .claude\skills\youtube-qa\scripts\state.py summary
```

성공 건수 / 스킵(영상별 사유) / 무효 링크 수를 보고하고, `output/INDEX.md` 경로를 안내한다.

---

## 지켜야 할 것

- 모든 스크립트는 `.venv\Scripts\python.exe`로 실행한다
- 대본 전문을 메인 컨텍스트에 올리지 않는다 (서브에이전트 안에만 둔다)
- 최종 md·frontmatter·INDEX·state는 **스크립트만** 쓴다
- 에러 하나로 전체를 중단하지 않는다 — 스킵하고 다음으로
- 실패한 영상은 `output/qa/`에 파일을 만들지 않는다
