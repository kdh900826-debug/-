# YouTube → Q&A 큐레이션 에이전트 (메인 오케스트레이터)

설계 원본: [youtube_qa_agent_design.md](youtube_qa_agent_design.md) v2.0
이 문서와 설계서가 어긋나면 **설계서가 우선**이다.

---

## 1. 프로젝트 개요

유튜브 링크 목록(`.txt`)을 받아 각 영상의 자막을 추출하고 **Q&A 형식**으로 재구성해
영상별 개별 md 파일로 저장한다.

| | |
|---|---|
| **입력** | `.txt` — 한 줄에 링크 하나. 빈 줄 무시, `#` 주석 |
| **출력** | `output/qa/{제목}__{video_id}.md` + `output/INDEX.md` |
| **포함** | 링크 파싱, 메타데이터, 자막 추출, 주제 아웃라인, Q&A 변환, 저장, 색인 |
| **미포함** | 요약, 인사이트 도출, 블로그 글 작성, 웹 검색 |

### 최상위 원칙

> **품질 우선, 낭비만 제거.**
> 토큰 절약은 "불필요한 것을 컨텍스트에 올리지 않는다"로만 정의한다.
> **결과물 품질을 깎아 토큰을 줄이지 않는다.**

---

## 2. 실행 트리거

```
/youtube-qa [입력파일경로]
```

- 인자 생략 시 `input/youtube_links.txt`
- **명시적 기동만 지원한다.** 자연어로 "유튜브 정리해줘" 같은 요청이 와도 이 워크플로우를
  자동으로 돌리지 마라. 사용자에게 `/youtube-qa` 실행을 안내한다.

---

## 3. 환경 점검 (시작 전 1회)

```bash
.venv\Scripts\python.exe -c "import youtube_transcript_api; print('ok')"
```

실패하면 **워크플로우를 시작하지 말고** 아래를 안내하고 중단한다.

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

- `yt-dlp`는 **선택적 의존성**이다. 없거나 봇 차단에 걸려도 워크플로우는 진행된다
  (영상 길이 필드만 비게 된다).
- 모든 스크립트는 반드시 `.venv\Scripts\python.exe`로 실행한다. 시스템 `python`을 쓰지 마라.

---

## 4. 워크플로우

```
Step 1  링크 파싱          [스크립트, 1회]
   └─ 영상별 순차 루프 (동시 1건):
      Step 2   메타데이터    [스크립트]
      Step 3   자막 추출     [스크립트]
      Step 3.5 아웃라인      ─┐
      Step 4   Q&A 변환       ├─ [서브에이전트 1개] ← 대본 전문은 여기에만 존재
      Step 4.5 커버리지 점검 ─┘
      Step 5   조립·저장     [스크립트]
      └─ 첫 영상이면 ⏸ 사용자 승인 대기
   └─ 종료: processing_summary.json
```

`SCRIPTS = .claude\skills\youtube-qa\scripts`, `PY = .venv\Scripts\python.exe`

### Step 1 — 링크 파싱

```bash
.venv\Scripts\python.exe .claude\skills\youtube-qa\scripts\parse_links.py "<입력.txt>"
```

- `output/step1_links.json` 생성 + `state.json` 초기화(기존 항목 보존).
- **exit 1(유효 링크 0건)이면 즉시 종료한다.** 거부된 링크는 사유와 함께 사용자에게 보고한다.
- 재생목록·채널 URL은 거부된다. 사용자가 재생목록을 넣었다면 개별 링크로 풀어 달라고 안내한다.

### 루프 진입 — 다음 영상 확인

```bash
.venv\Scripts\python.exe .claude\skills\youtube-qa\scripts\state.py next
```

`done: true`면 루프를 끝낸다. 아니면 `video_id`와 `resume_at`(재진입 지점)을 받아
해당 단계부터 시작한다. `is_first_video`가 `true`면 이 영상 뒤에 승인 게이트가 걸린다.

### Step 2 — 메타데이터

```bash
.venv\Scripts\python.exe .claude\skills\youtube-qa\scripts\fetch_metadata.py <video_id>
```

**어떤 실패도 워크플로우를 멈추지 않는다.** oEmbed → yt-dlp → `title=video_id` 폴백이
스크립트 안에 들어 있다. 출력의 `notes`에 실패 로그가 있어도 그냥 진행한다.

### Step 3 — 자막 추출

```bash
.venv\Scripts\python.exe .claude\skills\youtube-qa\scripts\fetch_transcript.py <video_id>
```

- `ok: false`(exit 2)면 스크립트가 이미 `skipped` 처리와 로그를 마쳤다. **다음 영상으로 넘어간다.**
- `ok: true`면 `caption_type`, `lang`, `char_count`, `outline_mode`를 받아 서브에이전트에 넘긴다.
- **대본 파일을 메인 컨텍스트에서 열지 마라.** 경로만 전달한다.

### Step 3.5 / 4 / 4.5 — 서브에이전트 스폰

영상 1건마다 서브에이전트 **1개**를 스폰한다(`general-purpose`). 프롬프트에 아래를 담는다:

```
.claude/skills/youtube-qa/SKILL.md 를 읽고 그 절차를 그대로 따라 영상 한 편을 처리하라.

video_id:    <id>
대본 경로:    output/transcripts_raw/<id>.txt
caption_type: <auto|manual>
lang:        <ko|en|...>
char_count:  <n>
outline_mode: <single-pass|hierarchical>
영상 제목:    <Step 2의 title>
채널명:      <Step 2의 channel>
파이썬:      .venv\Scripts\python.exe
작업 디렉터리: <프로젝트 루트 절대경로>

Step 3.5 → 4 → 4.5 를 수행하고, SKILL.md의 "반환 형식"대로만 보고하라.
대본이나 Q&A 본문을 응답에 붙이지 마라.
```

**제목·채널명을 반드시 넘겨라.** 자동생성 자막은 출연자 이름·회사명을 자주 틀리는데
(`황수욱 연구원` → `황숙`), 제목에 정답이 들어 있는 경우가 많다. 이건 외부 지식이 아니라
같은 영상의 메타데이터이므로 표기 복원의 근거로 쓸 수 있다.

반환값에서 `coverage`가 `fail`이면 Step 5로 가지 않는다:

```bash
.venv\Scripts\python.exe .claude\skills\youtube-qa\scripts\state.py skip <video_id> --reason "<사유>"
```

### Step 5 — 조립·저장

```bash
.venv\Scripts\python.exe .claude\skills\youtube-qa\scripts\save_qa.py <video_id>
```

frontmatter 생성·Q 번호 재부여·딥링크 URL 재생성·파일명 sanitize·`INDEX.md` 재생성·
`state.json` 기록을 전부 이 스크립트가 한다. **직접 md를 쓰지 마라.**
출력의 `warnings`가 비어 있지 않으면 사용자에게 그대로 전달한다.

### 종료

```bash
.venv\Scripts\python.exe .claude\skills\youtube-qa\scripts\state.py summary
```

`processing_summary.json`을 생성하고, 사용자에게 성공 / 스킵(사유별) / 무효 링크 수를 보고한다.

---

## 5. 서브에이전트 호출 규칙

| 항목 | 규칙 |
|------|------|
| **스폰 시점** | Step 3 성공 직후, 영상 1건당 정확히 1개 |
| **병렬도** | **동시 1건.** 상태 쓰기 충돌·로그 뒤섞임 방지 (설계결정 C2) |
| **전달** | video_id, 대본 경로, caption_type, lang, char_count, outline_mode |
| **반환** | 아웃라인 경로, qa 임시 파일 경로, qa_count, coverage 판정, 밀도 수치 |
| **금지** | 서브에이전트가 최종 md 작성·frontmatter 생성·INDEX/state 수정 (설계결정 C3) |

**대본 전문을 메인 컨텍스트에 올리지 않는다.** 이것이 서브에이전트 구조의 존재 이유다
(설계결정 C1). 메인이 직접 Q&A를 만들면 영상 3~5개에서 컨텍스트가 포화되고, 그 시점부터
산출물 품질이 사용자도 모르게 변한다.

---

## 6. 중단·재개

`output/state.json`이 진행 상태를 들고 있다. 재실행하면 완료 지점부터 이어간다.

| status | 재진입 지점 |
|--------|-------------|
| `pending` | Step 2 |
| `transcribed` | Step 3.5 (자막 재추출 안 함) |
| `outlined` | Step 4 |
| `done` | 건너뜀 |
| `skipped` | 건너뜀 (사유는 `skipped.log`) |

- `done` 영상을 다시 만들려면 `state.json`에서 그 항목을 지우거나 `pending`으로 바꾼다.
- 같은 링크 파일에 링크를 계속 누적해도 새 영상만 처리된다.

---

## 7. 승인 게이트 (첫 영상)

**첫 번째 영상을 끝까지 처리한 뒤 일시정지한다.** 완성된 md를 사용자에게 보여주고
승인을 받은 다음에 나머지를 진행한다.

보여줄 것: 파일 경로, Q 개수, 밀도 수치(9,000자당 Q수 / 원문 대비 분량), Q&A 앞부분 3~4개.

조정 가능한 것: **정보 밀도**(Q 개수·A 길이), **Q의 어조**, **딥링크 표기 방식**.

조정 요청이 오면 → `SKILL.md`의 해당 지침을 갱신 → `state.json`에서 그 영상을 `outlined`로
되돌림 → 서브에이전트 재스폰 → Step 5 재실행 → 다시 확인받는다.
**나머지 영상은 승인 전까지 손대지 않는다.**

---

## 8. 에러 처리

| 상황 | 처리 |
|------|------|
| 유효 링크 0건 | 워크플로우 종료 |
| 메타데이터 실패 | **진행.** title=video_id로 폴백 |
| 자막 없음 / 접근 불가 | 해당 영상 `skipped`, `skipped.log` 기록, 다음 영상 |
| 아웃라인 검증 실패 | 서브에이전트가 1회 재시도 → 재실패 시 영상 스킵 |
| 커버리지 점검 실패 | 누락 구간만 1회 보충 → 재실패 시 영상 스킵 |
| 저장 실패 | `skipped` 기록 |

**실패한 영상은 `output/qa/`에 파일을 만들지 않는다** (설계결정 D3).
`output/qa/`는 전부 신뢰할 수 있는 폴더로 유지한다. 원본 자막은 `transcripts_raw/`에
이미 있으므로 손실이 없다.

에러 하나로 전체 루프를 중단하지 마라. 스킵하고 다음 영상으로 간다.

---

## 9. 품질 기준

| 기준 | 값 |
|------|-----|
| Q 개수 | 대본 **9,000자당 15~25개** |
| A 길이 | 각 **3~8줄** |
| 최종 본문 | 원문 대비 **60~70%** |
| Q 배열 | **영상 흐름 순서 유지.** 흩어진 같은 주제를 통합하지 않는다 |
| 언어 | **항상 한국어.** 영어 영상은 원어 자막을 읽고 한국어로 작성 |

### 출처 한정 — 표기 복원은 허용, 내용 추가는 금지

| 구분 | 판정 |
|------|------|
| 오탈자·음차 표기 교정 (`피이알`→`PR`) | 허용 |
| 문장부호·띄어쓰기 삽입, 필러 제거 | 허용 |
| 대본에 없는 정의·배경·예시·수치 추가 | **금지** |
| 화자 주장의 보완·반박·평가 | **금지** |

---

## 10. 폴더 구조

```
├── CLAUDE.md                      # 이 파일
├── youtube_qa_agent_design.md     # 설계 원본
├── requirements.txt
├── .venv/
├── .claude/
│   ├── commands/youtube-qa.md
│   └── skills/youtube-qa/
│       ├── SKILL.md               # 서브에이전트 지침 (3.5·4·4.5)
│       ├── scripts/               # parse_links, fetch_metadata,
│       │                          # fetch_transcript, save_qa, state, verify
│       └── references/qa_examples.md
├── input/youtube_links.txt
└── output/
    ├── transcripts_raw/   # 원본 자막 + 구간 마커
    ├── outlines/          # 주제 아웃라인
    ├── qa/                # 최종 산출물 — 성공본만
    ├── .tmp/              # 임시 본문·메타 (저장 후 정리)
    ├── INDEX.md  state.json  step1_links.json
    └── skipped.log  processing_summary.json
```
