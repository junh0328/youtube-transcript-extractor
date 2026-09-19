#!/usr/bin/env bash
#
# 실행에 필요한 것들이 갖춰졌는지 확인하고, 빠진 것을 설치한다.
#
#   ./setup.sh             빠진 게 있으면 물어보고 설치
#   ./setup.sh --check     설치하지 않고 진단만
#   ./setup.sh --yes       묻지 않고 설치
#   ./setup.sh --no-probe  claude 로그인 확인용 호출을 건너뜀
#
# 종료 코드 0 = 바로 쓸 수 있음, 1 = 빠진 게 있음

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="ask"
PROBE=1
for arg in "$@"; do
  case "$arg" in
    --check) MODE="check" ;;
    --yes|-y) MODE="yes" ;;
    --no-probe) PROBE=0 ;;
    --help|-h) sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "모르는 옵션: $arg (--check, --yes, --no-probe, --help)"; exit 2 ;;
  esac
done

# .env 가 있으면 먼저 읽는다. summarize.py 도 같은 파일을 보므로 판단 기준이 일치한다
if [ -f "$REPO/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO/.env"
  set +a
fi

if [ -t 1 ]; then
  G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'
else
  G=""; Y=""; R=""; D=""; N=""
fi

MISSING=0   # 하나라도 있으면 "바로 사용 가능" 이라고 말할 수 없다

ok()   { printf '  %s✓%s %-22s %s\n' "$G" "$N" "$1" "${2:-}"; }
warn() { printf '  %s!%s %-22s %s\n' "$Y" "$N" "$1" "${2:-}"; }
bad()  { printf '  %s✗%s %-22s %s\n' "$R" "$N" "$1" "${2:-}"; }
note() { printf '      %s%s%s\n' "$D" "$1" "$N"; }

# 설치해도 되는지 확인한다. --check 면 무조건 안 함, --yes 면 무조건 함
may_install() {
  [ "$MODE" = "check" ] && return 1
  [ "$MODE" = "yes" ] && return 0
  [ -t 0 ] || return 1   # 파이프로 돌릴 땐 묻지 않는다
  local answer
  read -r -p "      → $1 [y/N] " answer
  [[ "$answer" =~ ^[Yy]$ ]]
}

echo
echo "yt-transcript-extractor 환경 점검"
echo "────────────────────────────────────────────────"

# --- Python ---------------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
  PYV="$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)"
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    ok "python3" "$PYV"
  else
    bad "python3" "$PYV (3.9 이상 필요)"
    note "brew install python@3.12  또는 python.org 에서 설치"
    MISSING=1
  fi
else
  bad "python3" "없음"
  note "macOS: brew install python@3.12 / Linux: apt install python3"
  MISSING=1
fi

# --- Homebrew (설치 수단) ---------------------------------------------------
HAS_BREW=0
if command -v brew >/dev/null 2>&1; then
  HAS_BREW=1
  ok "brew" "$(brew --version 2>/dev/null | head -1)"
else
  warn "brew" "없음 (설치 자동화에만 쓰임)"
fi

# --- yt-dlp (자막 받기) ------------------------------------------------------
install_ytdlp() {
  if [ "$HAS_BREW" = "1" ]; then
    brew install yt-dlp
  elif command -v pip3 >/dev/null 2>&1; then
    pip3 install --user -U yt-dlp
  else
    return 1
  fi
}

if command -v yt-dlp >/dev/null 2>&1; then
  ok "yt-dlp" "$(yt-dlp --version 2>/dev/null)"
else
  bad "yt-dlp" "없음 — 자막을 받을 수 없음"
  if may_install "지금 설치할까요?"; then
    if install_ytdlp && command -v yt-dlp >/dev/null 2>&1; then
      ok "yt-dlp" "$(yt-dlp --version 2>/dev/null) (방금 설치)"
    else
      bad "yt-dlp" "설치 실패"
      note "brew install yt-dlp  또는  pip3 install -U yt-dlp"
      MISSING=1
    fi
  else
    note "brew install yt-dlp  또는  pip3 install -U yt-dlp"
    MISSING=1
  fi
fi

# --- 요약 백엔드 -------------------------------------------------------------
# 기본은 claude CLI. YT_LLM_BASE_URL 이 있으면 OpenAI 호환 엔드포인트를 쓴다
BACKEND_OK=1
if [ -n "${YT_LLM_BASE_URL:-}" ]; then
  ok "요약 백엔드" "OpenAI 호환 · ${YT_LLM_BASE_URL}"
  if [ -n "${YT_LLM_MODEL:-}" ]; then
    ok "YT_LLM_MODEL" "$YT_LLM_MODEL"
  else
    bad "YT_LLM_MODEL" "없음 — 모델 이름이 필요합니다"
    MISSING=1; BACKEND_OK=0
  fi
  if [ -n "${YT_LLM_API_KEY:-}" ]; then
    ok "YT_LLM_API_KEY" "설정됨"
  else
    warn "YT_LLM_API_KEY" "없음 (Ollama 등 로컬 서버면 무방)"
  fi
elif command -v claude >/dev/null 2>&1; then
  ok "요약 백엔드" "claude CLI · $(claude --version 2>/dev/null | head -1)"
else
  bad "요약 백엔드" "없음 — claude CLI 도 YT_LLM_BASE_URL 도 없습니다"
  note "https://claude.com/claude-code 에서 설치 후 로그인, 또는"
  note "export YT_LLM_BASE_URL=... YT_LLM_MODEL=... YT_LLM_API_KEY=..."
  note "자막만 쓰려면: python3 yt-transcript.py \"\$URL\" --no-summary"
  MISSING=1; BACKEND_OK=0
fi

# 설치돼 있어도 로그인·키가 잘못돼 있으면 요약 단계에서야 실패한다.
# 그건 실제로 한 번 불러 봐야만 알 수 있어서, 백엔드와 무관하게 같은 방식으로 확인한다
if [ "$BACKEND_OK" = "1" ]; then
  if [ "$PROBE" = "1" ]; then
    [ -t 1 ] && printf '      %s모델 호출 확인 중...%s\r' "$D" "$N"
    PROBE_OUT="$(cd "$REPO" && python3 summarize.py --selftest 2>&1)"
    PROBE_RC=$?
    [ -t 1 ] && printf '\033[2K'
    if [ "$PROBE_RC" = "0" ]; then
      ok "모델 호출" "${PROBE_OUT#\[정상\] }"
    else
      bad "모델 호출" "${PROBE_OUT#\[실패\] }"
      note "로그인 또는 API 키를 확인하세요"
      note "확인을 건너뛰려면: ./setup.sh --no-probe"
      MISSING=1
    fi
  else
    warn "모델 호출" "확인 안 함 (--no-probe)"
  fi
fi

# --- 저장소 파일 -------------------------------------------------------------
echo
echo "저장소 구성"
echo "────────────────────────────────────────────────"

for f in yt-transcript.py summarize.py render.py grounding.py extract.py template.py template.json; do
  if [ -f "$REPO/$f" ]; then
    ok "$f"
  else
    bad "$f" "없음"
    MISSING=1
  fi
done

if [ "$MISSING" = "0" ] && command -v python3 >/dev/null 2>&1; then
  if (cd "$REPO" && python3 template.py >/dev/null 2>&1); then
    TPL="$(cd "$REPO" && python3 -c 'import template; t=template.load(); print("%s v%s" % (t["name"], t["version"]))' 2>/dev/null)"
    ok "템플릿 로드" "${TPL:-정상}"
  else
    bad "템플릿 로드" "template.json 을 읽지 못함"
    MISSING=1
  fi

  if (cd "$REPO" && python3 -c '
import ast, sys
for f in ["yt-transcript.py","summarize.py","render.py","grounding.py","extract.py","template.py"]:
    ast.parse(open(f, encoding="utf-8").read())
' 2>/dev/null); then
    ok "스크립트 문법" "6개 정상"
  else
    bad "스크립트 문법" "오류"
    MISSING=1
  fi
fi

mkdir -p "$REPO/transcripts" "$REPO/summaries"

# --- 결과 -------------------------------------------------------------------
echo
echo "────────────────────────────────────────────────"
if [ "$MISSING" != "0" ]; then
  printf '%s준비되지 않음%s — 위의 ✗ 항목을 먼저 해결하세요.\n\n' "$R" "$N"
  exit 1
fi

printf '%s바로 사용할 수 있습니다.%s\n\n' "$G" "$N"
echo "  python3 yt-transcript.py \"<유튜브 URL>\""
echo
echo "  → transcripts/<제목>.md   자막"
echo "  → summaries/<제목>.json   요약 (정본)"
echo "  → summaries/<제목>.md     요약 (읽기용)"
echo
