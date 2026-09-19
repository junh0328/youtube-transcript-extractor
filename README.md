# yt-transcript-extractor

유튜브 영상 URL을 넣으면 **타임스탬프가 붙은 자막**을 `.md` / `.json` 으로 추출하는 스크립트.

- `.md` — 타임스탬프가 **해당 시점으로 바로 점프하는 링크**로 걸린 읽기용 문서
- `.json` — `start` / `end` / `timestamp` / `text` 구조의 세그먼트 배열 (요약·RAG 색인·후처리용)

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
| `--lang` | 선호 언어 코드(쉼표 구분, 앞에서부터 우선) | `ko,en` |
| `--format` | `md` / `json` / `both` | `both` |
| `--outdir` | 출력 디렉터리 | 현재 디렉터리 |
| `--chunk` | N초 단위로 세그먼트를 병합해 문단화 (0이면 원본 유지) | `0` |
| `--no-auto` | 자동 생성(ASR) 자막 제외, 수동 자막만 사용 | off |
| `--from-json` | 이미 추출한 `.json` 을 재가공 (네트워크 요청 없음) | - |

### 예시

```bash
# 기본 (한국어 우선, md + json)
python3 yt-transcript.py "https://youtu.be/VIDEO_ID"

# 영어 원본 트랙을 45초 단위로 묶어 읽기 좋게
python3 yt-transcript.py "https://youtu.be/VIDEO_ID" --lang en-orig --chunk 45

# 이미 뽑은 json 으로 청크 버전만 다시 생성 (재요청 없음 → 429 회피)
python3 yt-transcript.py --from-json "transcripts/제목_ko.json" --chunk 60 --format md
```

출력 파일명은 `<영상 제목>_<언어>[_chunk<N>s].<확장자>` 형식이라 언어·청크 조합이 서로 덮어쓰지 않는다.

## 언어 코드에 대해

자동 자막만 있는 영상은 보통 두 종류가 존재한다.

- `en-orig` — **원본 음성을 그대로 인식한 트랙** (가장 정확)
- `ko`, `ja`, … — 원본을 기계 번역한 트랙

정확도가 중요하면 `--lang en-orig` 처럼 원본 트랙을 쓰고, 번역본은 참고용으로 함께 뽑는 편이 좋다.
요청한 언어가 없으면 사용 가능한 트랙 목록을 출력한다.

## 구현 메모

- **json3 포맷** 사용 — VTT보다 타임스탬프가 정밀하고 파싱이 단순하다.
- **롤링 중복 제거** — 자동 자막은 같은 문장이 여러 프레임에 반복 등장하는데, 직전 세그먼트와 텍스트가 같으면 `end` 만 늘리고 합친다.
- **구간 겹침 보정** — 각 세그먼트의 `end` 가 다음 `start` 를 넘지 않도록 잘라낸다.
- **429 백오프 재시도** — 유튜브가 자동 자막 요청에 `HTTP 429` 를 자주 돌려준다. 20초·40초·60초 간격으로 최대 4회 재시도한다.
- **언어 코드 접두사 매칭** — `ko` 요청 시 `ko-KR` 같은 지역 변형도 잡는다.

## 한계

자막 트랙 자체가 없는 영상은 이 방식으로 추출할 수 없다. 그 경우 음성을 내려받아 Whisper 등으로 STT를 돌려야 한다.

## 예시 결과물

`transcripts/` 에 [예시 영상 A](https://www.youtube.com/watch?v=VIDEO_ID) (19분 31초) 추출 결과가 들어 있다.

| 파일 | 내용 | 세그먼트 |
| --- | --- | --- |
| `..._en-orig.json` / `.md` | 영어 원본 트랙, 원본 세그먼트 | 515 |
| `..._en-orig_chunk45s.md` | 영어 원본, 45초 문단화 | 27 |
| `..._ko.json` / `.md` | 한국어 번역 트랙, 원본 세그먼트 | 450 |
| `..._ko_chunk45s.md` | 한국어 번역, 45초 문단화 | 27 |
