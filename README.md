# yt-transcript-extractor

유튜브 영상 URL을 넣으면 **타임스탬프가 붙은 자막**을 `.md` 로 추출하는 스크립트.

옵션 없이 URL만 넣으면:

- **한국어**로 나온다. 영어 영상이면 유튜브의 자동 번역 트랙을 쓴다.
- **45초 단위로 문단화**된다. 자동 자막의 2초짜리 조각을 읽을 수 있는 덩어리로 묶는다.
- **`.md` 한 개**만 생성된다. 타임스탬프는 해당 시점으로 바로 점프하는 링크로 걸린다.

```markdown
[`00:00:45`](https://www.youtube.com/watch?v=VIDEO_ID&t=45s) 자막 문단 텍스트가 45초 단위로 묶여 이어진다...
```

후처리·RAG 색인용 구조화 데이터가 필요하면 `--format json` 으로 `start` / `end` / `timestamp` / `text` 세그먼트 배열을 받을 수 있다.

## 요구 사항

- Python 3.9+
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) — `brew install yt-dlp`

## 사용법

```bash
python3 yt-transcript.py "<유튜브 URL>"
```

### 옵션

| 옵션 | 설명 | 기본값 |
| --- | --- | --- |
| `--lang` | 선호 언어 코드(쉼표 구분, 앞에서부터 우선) | `ko,en-orig,en` |
| `--format` | `md` / `json` / `both` | `md` |
| `--outdir` | 출력 디렉터리 | 현재 디렉터리 |
| `--chunk` | N초 단위로 세그먼트를 병합해 문단화 (0이면 원본 유지) | `45` |
| `--no-auto` | 자동 생성(ASR) 자막 제외, 수동 자막만 사용 | off |
| `--from-json` | 이미 추출한 `.json` 을 재가공 (네트워크 요청 없음) | - |

### 예시

```bash
# 기본 — 한국어, 45초 문단, md 한 개
python3 yt-transcript.py "https://youtu.be/VIDEO_ID"

# 번역 품질이 중요할 때: 영어 원본 트랙
python3 yt-transcript.py "https://youtu.be/VIDEO_ID" --lang en-orig

# 나중에 다시 가공할 수 있게 json 까지 함께 저장
python3 yt-transcript.py "https://youtu.be/VIDEO_ID" --format both

# 이미 뽑은 json 으로 문단 길이만 바꿔 재생성 (재요청 없음 → 429 회피)
python3 yt-transcript.py --from-json "transcripts/제목_ko.json" --chunk 90
```

`--lang` 기본값의 `en-orig,en` 은 **한국어 트랙이 아예 없는 영상을 위한 안전망**이다. 이 경우 영어로 대체하면서 다음 경고를 출력하므로, 결과가 한국어가 아니라는 사실이 조용히 묻히지 않는다.

```
[경고] 'ko' 자막이 없어 'en' 로 대체합니다. 한국어 결과가 아닙니다.
```

출력 파일명은 `<영상 제목>.<확장자>` 다. **같은 URL 을 다시 돌리면 기존 파일을 덮어쓴다** — 옵션을 바꿔가며 여러 번 실행해도 파일이 쌓이지 않고 항상 최신 결과 하나만 남는다. 덮어쓴 경우 출력이 `[완료]` 대신 `[덮어씀]` 으로 표시된다.

언어나 문단 길이가 다른 결과를 나란히 두고 비교하려면 `--outdir` 로 분리한다.

```bash
python3 yt-transcript.py "$URL" --outdir ko
python3 yt-transcript.py "$URL" --outdir en --lang en-orig
```

## 언어 코드에 대해

자동 자막만 있는 영상은 보통 두 종류가 존재한다.

- `en-orig` — **원본 음성을 그대로 인식한 트랙** (가장 정확)
- `ko`, `ja`, … — 원본을 기계 번역한 트랙

기본값은 편의를 위해 한국어 번역본이지만, **기계 번역이라 오역이 섞인다.** 실제로 `transcripts/` 의 한국어 결과에는 인명 오기("Camirand" → "카메론"), 문맥 오역("October low"(10월 저점) → "10월 최저 기온") 같은 사례가 있다. 정확도가 중요한 용도라면 `--lang en-orig` 로 원본 트랙을 함께 뽑아 대조하는 편이 좋다.

요청한 언어가 없으면 사용 가능한 트랙 목록을 출력한다.

## 구현 메모

- **json3 포맷** 사용 — VTT보다 타임스탬프가 정밀하고 파싱이 단순하다.
- **롤링 중복 제거** — 자동 자막은 같은 문장이 여러 프레임에 반복 등장하는데, 직전 세그먼트와 텍스트가 같으면 `end` 만 늘리고 합친다.
- **구간 겹침 보정** — 각 세그먼트의 `end` 가 다음 `start` 를 넘지 않도록 잘라낸다.
- **429 백오프 재시도** — 유튜브가 자동 자막 요청에 `HTTP 429` 를 자주 돌려준다. 20초·40초·60초 간격으로 최대 4회 재시도한다.
- **언어 코드 접두사 매칭** — `ko` 요청 시 `ko-KR` 같은 지역 변형도 잡는다.

## 요약 (summaries/)

자막을 요약해 슬랙·카카오톡으로 흘려보내기 위한 부분. 핵심 규칙은 **요약을 '완성된 텍스트'가 아니라 구조(JSON)로 저장하고, 채널 형식은 보내기 직전에 입힌다**는 것이다.

```
URL → yt-transcript.py → transcripts/<제목>.md
                              ↓ (LLM 요약)
                         summaries/<제목>.json   ← 정본
                              ↓ render.py
        ┌─────────────┬─────────────┬─────────────┐
   <제목>.md      슬랙 블록      슬랙 스레드     카카오톡 텍스트
   (읽기용)      (핵심만)       (상세)        (1000자, 장식 없음)
```

슬랙 mrkdwn(`*굵게*`, `<URL|텍스트>`), 카카오톡 순수 텍스트, 마크다운은 문법이 전부 다르다. 요약을 텍스트로 저장하면 채널이 늘 때마다 다시 요약해야 하고 채널 간 내용이 어긋난다. 구조로 저장하면 렌더러만 추가하면 된다.

### 렌더링

```bash
python3 render.py "summaries/<제목>.json" --to kakao          # 순수 텍스트
python3 render.py "summaries/<제목>.json" --to slack          # Block Kit (핵심)
python3 render.py "summaries/<제목>.json" --to slack-thread   # Block Kit (상세)
python3 render.py "summaries/<제목>.json" --to md -o "summaries/<제목>.md"
```

표준출력으로 나가므로 그대로 파이프할 수 있다.

```bash
python3 render.py "summaries/<제목>.json" --to slack \
  | curl -sX POST -H 'Content-type: application/json' -d @- "$SLACK_WEBHOOK_URL"
```

### 스키마

| 필드 | 역할 | 길이 예산 |
| --- | --- | --- |
| `headline` | 한 줄 결론. 카톡 푸시 미리보기·슬랙 알림에 뜨는 부분 | ~100자 |
| `key_points[]` | 핵심 3~5개. **카카오톡은 여기까지만** 나간다 | 각 ~90자 |
| `sections[].points[]` | 상세. 슬랙 스레드와 `.md` 전용 | 제한 없음 |
| `entities.tickers` / `targets` | 라우팅·필터링용 구조화 값 | - |
| `disclaimer` | 고정 면책 문구 | - |

설계 의도:

- **깊이를 3단으로 고정한다.** 가변 깊이(1 → 1.1 → 1.1.1)는 웹에서는 읽히지만 채팅에서는 무너진다. 카카오톡에는 들여쓰기도 불릿도 굵게도 없다. 깊이가 고정이어야 "채널별로 어디까지 보낼지"가 스키마 수준에서 결정된다.
- **길이 예산을 요약 생성 시점에 건다.** 렌더 단계에서 자르면 문장이 중간에 끊긴다. 그래도 초과하면 `render.py` 가 항목 단위로 덜어내고 `…전체 5개 중 3개` 를 남긴다.
- **모든 주장에 `t`(초)를 붙인다.** 점프 링크 생성에 쓰이고, 동시에 요약의 각 문장이 원문 어디서 나왔는지 대조하는 수단이 된다. 자동 발송은 사람이 중간에 검수하지 않으므로 이 대조 경로가 사실상 유일한 안전장치다.
- **화자 귀속 문체를 쓴다.** 모든 주장은 `~라고 주장한다 / 본다 / 예상한다` 로 쓴다. 남에게 자동 발송되는 글에서 화자의 전망을 단정형으로 옮기면 요약자의 주장이 되어버린다.

### 요약 생성

현재 `summaries/*.json` 은 LLM(Claude)이 자막을 읽고 스키마에 맞춰 작성한다. 자동화하려면 Anthropic API 의 구조화 출력으로 이 스키마를 강제하는 단계를 붙이면 된다.

입력은 `--lang en-orig` 로 뽑은 **원본 트랙을 쓰는 편이 낫다.** 기계 번역 자막에는 오역이 섞이고(예: `October low` → `10월 최저 기온`, `Wyckoff` → `워프 축적`, `$90k` → `90km/h`, 채널명 `Camirand` → `카메론`) 일부 구간은 번역되지 않고 영어로 남는다. 번역본을 요약하면 이 오류가 그대로 굳는다.

## 한계

자막 트랙 자체가 없는 영상은 이 방식으로 추출할 수 없다. 그 경우 음성을 내려받아 Whisper 등으로 STT를 돌려야 한다.

## 예시 결과물

`transcripts/예시 영상 A.md` — [해당 영상](https://www.youtube.com/watch?v=VIDEO_ID) (19분 31초) 을 기본 옵션으로 추출한 결과. 한국어 번역 트랙의 450개 원본 세그먼트를 45초 단위로 묶어 27개 문단이 됐다.
