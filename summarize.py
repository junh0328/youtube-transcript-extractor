#!/usr/bin/env python3
"""자막 .md 를 읽어 한국어 요약 JSON 을 만든다.

    summarize.py <자막.md> [--outdir summaries] [--model claude-opus-5]

요약 생성은 로컬에 설치된 `claude` CLI 를 헤드리스(-p)로 호출한다.
별도의 API 키 없이 기존 로그인을 그대로 쓰기 위한 선택이다.
ANTHROPIC_API_KEY 를 쓰는 SDK 방식으로 바꾸려면 call_model() 만 교체하면 된다.

영상 메타데이터(제목·URL·길이 등)는 모델에게 맡기지 않고 자막 헤더에서 직접 읽는다.
모델은 요약 본문만 만든다 — 틀릴 수 있는 값을 모델에게 넘기지 않기 위해서다.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

import extract
import template as tmpl


def parse_transcript(path):
    """자막 .md 헤더에서 영상 메타데이터를 읽는다."""
    with open(path, encoding="utf-8") as fp:
        text = fp.read()

    def grab(pattern, default=""):
        m = re.search(pattern, text, re.M)
        return m.group(1).strip() if m else default

    title = grab(r"^#\s+(.+)$")
    url = grab(r"\*\*URL\*\*:\s*(\S+)")
    duration = grab(r"\*\*길이\*\*:\s*([\d:]+)", "0:00")
    sub = grab(r"\*\*자막\*\*:\s*(\S+)")

    parts = [int(x) for x in duration.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    duration_sec = parts[0] * 3600 + parts[1] * 60 + parts[2]

    vid = re.search(r"[?&]v=([\w-]+)", url)
    body = text.split("---", 1)[1].strip() if "---" in text else text

    return {
        "video": {
            "video_id": vid.group(1) if vid else None,
            "title": title,
            "url": url,
            "channel": grab(r"\*\*채널\*\*:\s*(.+)"),
            "duration_sec": duration_sec,
        },
        "source": {
            "transcript": path,
            "lang": sub,
            "auto_generated": "자동 생성" in text,
        },
        "body": body,
    }


def load_env_file(path=None):
    """레포의 .env 를 읽어 환경변수로 올린다.

    이미 설정된 환경변수는 덮지 않는다 — 셸에서 일시적으로 바꿔 쓰는 쪽이
    파일보다 우선해야 한 번만 다르게 돌려보기가 쉽다.
    """
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


def resolve_backend(model):
    """어떤 LLM 으로 부를지 정한다.

    기본은 로컬 `claude` CLI 다. 환경변수 YT_LLM_BASE_URL 이 있으면
    OpenAI 호환 엔드포인트로 보낸다 — OpenAI, Groq, Together, OpenRouter,
    vLLM, Ollama, Gemini(OpenAI 호환 경로)가 모두 같은 규격이라
    구현 하나로 커버된다.
    """
    load_env_file()
    base = os.environ.get("YT_LLM_BASE_URL", "").strip()
    chosen = model or os.environ.get("YT_LLM_MODEL", "").strip()
    if base:
        if not chosen:
            sys.exit("[에러] YT_LLM_BASE_URL 을 쓰려면 YT_LLM_MODEL 또는 --model 이 필요합니다.")
        return {"kind": "openai", "base_url": base, "model": chosen,
                "api_key": os.environ.get("YT_LLM_API_KEY", "").strip()}
    return {"kind": "claude-cli", "model": chosen or "claude-opus-5"}


def describe_backend(backend):
    if backend["kind"] == "claude-cli":
        return "claude CLI · {}".format(backend["model"])
    host = re.sub(r"^https?://", "", backend["base_url"]).split("/")[0]
    return "{} · {}".format(host, backend["model"])


def _call_claude_cli(prompt, backend):
    """(응답 텍스트, 에러) 를 돌려준다."""
    proc = subprocess.run(["claude", "-p", "--model", backend["model"]],
                          input=prompt, capture_output=True, text=True)
    if proc.returncode != 0:
        return None, "claude 호출 실패: " + (proc.stderr or "").strip()[:150]
    return proc.stdout, None


def _call_openai(prompt, backend, json_mode=True):
    """OpenAI 호환 /chat/completions 로 보낸다. 표준 라이브러리만 쓴다."""
    url = backend["base_url"].rstrip("/") + "/chat/completions"
    payload = {"model": backend["model"],
               "messages": [{"role": "user", "content": prompt}]}
    if json_mode:
        # JSON 만 나오도록 제공자 쪽에서 막아준다. 지원하지 않으면 아래에서 한 번 더 시도
        payload["response_format"] = {"type": "json_object"}

    headers = {"Content-Type": "application/json"}
    if backend["api_key"]:
        headers["Authorization"] = "Bearer " + backend["api_key"]

    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            body = json.load(response)
        return body["choices"][0]["message"]["content"], None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        if json_mode and exc.code in (400, 404, 422):
            # response_format 을 모르는 제공자(예: 일부 로컬 서버)면 빼고 재시도
            return _call_openai(prompt, backend, json_mode=False)
        return None, "HTTP {}: {}".format(exc.code, detail)
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError) as exc:
        return None, "{}: {}".format(type(exc).__name__, str(exc)[:150])


def send(prompt, backend):
    if backend["kind"] == "openai":
        return _call_openai(prompt, backend)
    return _call_claude_cli(prompt, backend)


def call_model(prompt, backend, duration, tpl, retries=3):
    """템플릿을 만족하는 요약이 나올 때까지 모델을 호출한다.

    프롬프트만으로는 스키마가 지켜진다는 보장이 없다. 받은 값을 검사해서
    구조가 깨졌으면 무엇이 잘못됐는지 되먹여 다시 받는다.
    """
    feedback = ""
    for attempt in range(retries):
        raw, error = send(prompt + feedback, backend)
        if error:
            # 전송 실패는 모델이 잘못 쓴 게 아니다. 되먹일 내용이 없고, 쉬었다 다시 걸어야 한다
            # (무료 티어의 429 가 대표적이다)
            problems = [error]
            if attempt < retries - 1:
                wait = 15 * (attempt + 1)
                print("[재시도] {} — {}초 뒤 다시 시도합니다".format(error, wait), file=sys.stderr)
                time.sleep(wait)
                continue
        else:
            data = extract_json(raw)
            if data is None:
                problems = ["응답이 올바른 JSON 이 아닙니다"]
            else:
                problems, soft = check(data, duration, tpl)
                if not problems:
                    return repair(data, duration, soft, tpl)

        print("[재시도] 템플릿 위반 {}건 ({}/{}회차)".format(
            len(problems), attempt + 1, retries), file=sys.stderr)
        for p in problems[:5]:
            print("         · " + p, file=sys.stderr)
        feedback = ("\n\n(직전 응답의 문제: " + " / ".join(problems[:8]) +
                    ". 스키마를 정확히 지켜 JSON 만 다시 출력하세요.)")

    sys.exit("[에러] {}회 시도했지만 템플릿을 만족하는 요약을 얻지 못했습니다.".format(retries))


def extract_json(text):
    """코드펜스나 잡설이 섞여 나와도 JSON 본문만 건져낸다."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _as_seconds(value):
    """t 를 정수 초로 해석한다. 못 하면 None."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _check_entry(entry, where, duration, fatal, soft):
    """{text, t} 한 항목을 검사한다."""
    if not isinstance(entry, dict):
        fatal.append("{} 가 객체가 아닙니다".format(where))
        return
    text = entry.get("text")
    if not isinstance(text, str) or not text.strip():
        fatal.append("{}.text 가 비어 있거나 문자열이 아닙니다".format(where))
    sec = _as_seconds(entry.get("t"))
    if sec is None:
        fatal.append("{}.t 를 초로 해석할 수 없습니다".format(where))
    elif not 0 <= sec <= duration:
        soft.append("{}.t={} 가 영상 길이(0~{}초) 밖입니다".format(where, sec, duration))


def check(data, duration, tpl):
    """템플릿을 지켰는지 검사한다. (치명적 문제, 보정 가능한 문제) 를 돌려준다.

    치명적 = 구조가 깨져 렌더러가 터지는 것. 모델에 되먹여 다시 받는다.
    보정 가능 = 개수·길이·범위처럼 잘라내거나 눌러서 맞출 수 있는 것.
    """
    fatal, soft = [], []
    if not isinstance(data, dict):
        return ["최상위가 JSON 객체가 아닙니다"], []

    max_headline = tpl["limits"]["headline_chars"]
    headline = data.get("headline")
    if not isinstance(headline, str) or not headline.strip():
        fatal.append("headline 이 비어 있거나 문자열이 아닙니다")
    elif len(headline) > max_headline:
        soft.append("headline 이 {}자로 {}자 예산을 넘습니다".format(len(headline), max_headline))

    key_points = data.get("key_points")
    if not isinstance(key_points, list) or not key_points:
        fatal.append("key_points 가 비어 있거나 배열이 아닙니다")
    else:
        for i, entry in enumerate(key_points):
            _check_entry(entry, "key_points[{}]".format(i), duration, fatal, soft)
        lo, hi = tmpl.span(tpl, "key_points")
        if not lo <= len(key_points) <= hi:
            soft.append("key_points 가 {}개입니다(규칙 {}~{}개)".format(len(key_points), lo, hi))

    sections = data.get("sections")
    if not isinstance(sections, list) or not sections:
        fatal.append("sections 가 비어 있거나 배열이 아닙니다")
    else:
        for i, sec in enumerate(sections):
            where = "sections[{}]".format(i)
            if not isinstance(sec, dict):
                fatal.append("{} 가 객체가 아닙니다".format(where))
                continue
            if not isinstance(sec.get("title"), str) or not sec["title"].strip():
                fatal.append("{}.title 이 비어 있거나 문자열이 아닙니다".format(where))
            if _as_seconds(sec.get("t")) is None:
                fatal.append("{}.t 를 초로 해석할 수 없습니다".format(where))
            points = sec.get("points")
            if not isinstance(points, list) or not points:
                fatal.append("{}.points 가 비어 있거나 배열이 아닙니다".format(where))
                continue
            for j, entry in enumerate(points):
                _check_entry(entry, "{}.points[{}]".format(where, j), duration, fatal, soft)
            lo, hi = tmpl.span(tpl, "section_points")
            if not lo <= len(points) <= hi:
                soft.append("{}.points 가 {}개입니다(규칙 {}~{}개)".format(where, len(points), lo, hi))
        lo, hi = tmpl.span(tpl, "sections")
        if not lo <= len(sections) <= hi:
            soft.append("sections 가 {}개입니다(규칙 {}~{}개)".format(len(sections), lo, hi))

    return fatal, soft


def repair(data, duration, soft, tpl):
    """보정 가능한 위반을 실제로 맞춘다. 무엇을 손댔는지 반드시 남긴다."""
    for note in soft:
        print("[보정] " + note, file=sys.stderr)

    def fix_entry(entry):
        entry["t"] = min(max(_as_seconds(entry.get("t")) or 0, 0), duration)
        return entry

    hi = tmpl.span(tpl, "key_points")[1]
    if len(data["key_points"]) > hi:
        print("[보정] key_points {}개 중 뒤의 {}개를 버립니다".format(
            len(data["key_points"]), len(data["key_points"]) - hi), file=sys.stderr)
        data["key_points"] = data["key_points"][:hi]
    data["key_points"] = [fix_entry(e) for e in data["key_points"]]

    hi = tmpl.span(tpl, "sections")[1]
    if len(data["sections"]) > hi:
        print("[보정] sections {}개 중 뒤의 {}개를 버립니다".format(
            len(data["sections"]), len(data["sections"]) - hi), file=sys.stderr)
        data["sections"] = data["sections"][:hi]
    max_points = tmpl.span(tpl, "section_points")[1]
    for sec in data["sections"]:
        sec["t"] = min(max(_as_seconds(sec.get("t")) or 0, 0), duration)
        sec["points"] = [fix_entry(e) for e in sec["points"][:max_points]]

    # entities 는 없어도 되는 필드다. 모양이 깨진 항목만 조용히 버리지 말고 알린다
    entities = data.get("entities")
    if not isinstance(entities, dict):
        entities = {}
    keywords = entities.get("keywords")
    entities["keywords"] = [k for k in keywords if isinstance(k, str)] if isinstance(keywords, list) else []

    figures, kept = entities.get("figures"), []
    for fig in figures if isinstance(figures, list) else []:
        sec = _as_seconds(fig.get("t")) if isinstance(fig, dict) else None
        if isinstance(fig, dict) and isinstance(fig.get("label"), str) \
                and isinstance(fig.get("value"), str) and sec is not None:
            fig["t"] = min(max(sec, 0), duration)
            kept.append(fig)
    dropped = (len(figures) if isinstance(figures, list) else 0) - len(kept)
    if dropped:
        print("[보정] figures {}개가 label/value/t 를 갖추지 못해 제외했습니다".format(dropped),
              file=sys.stderr)
    entities["figures"] = kept
    data["entities"] = entities

    if not isinstance(data.get("disclaimer"), str):
        data["disclaimer"] = ""
    return data


def safe_name(title):
    name = re.sub(r'[/\\:*?"<>|]', "_", title).strip()
    return (name[:100] or "summary").rstrip(". ")


def main():
    ap = argparse.ArgumentParser(description="자막을 한국어 요약 JSON 으로 변환")
    ap.add_argument("transcript", nargs="?", help="yt-transcript.py 가 만든 .md")
    ap.add_argument("--outdir", default="summaries")
    ap.add_argument("--out", help="출력 .json 경로를 직접 지정(--outdir 무시)")
    ap.add_argument("--model", help="모델 이름 (기본: claude-opus-5, 또는 YT_LLM_MODEL)")
    ap.add_argument("--selftest", action="store_true",
                    help="자막 없이 모델 호출만 확인하고 끝냄")
    ap.add_argument("--template", help="요약 템플릿 .json (기본: 레포의 template.json)")
    ap.add_argument("--extract", type=int, metavar="N",
                    help="TextRank 로 상위 N개 문단만 골라 모델에 넘김(할루시네이션·토큰 절감)")
    args = ap.parse_args()
    backend = resolve_backend(args.model)

    if args.selftest:
        # setup.sh 가 부른다. 백엔드가 무엇이든 같은 방식으로 확인하기 위해
        # bash 쪽에 HTTP 호출을 또 구현하지 않고 여기서 처리한다
        raw, error = send("Reply with exactly: OK", backend)
        if error or "OK" not in (raw or ""):
            print("[실패] {} — {}".format(describe_backend(backend), error or "응답 없음"))
            return 1
        print("[정상] {}".format(describe_backend(backend)))
        return 0

    if not args.transcript:
        ap.error("자막 .md 경로가 필요합니다 (--selftest 제외)")

    tpl = tmpl.load(args.template)
    meta = parse_transcript(args.transcript)
    if not meta["body"]:
        sys.exit("[에러] 자막 본문이 비어 있습니다: " + args.transcript)

    body = meta["body"]
    if args.extract:
        lines, kept, total = extract.select(args.transcript, args.extract)
        body = "\n\n".join(lines)
        print("[정보] 추출 단계: 문단 {}개 중 {}개 선별 ({:.0f}% 축소)".format(
            total, kept, 100 * (1 - len(body) / len(meta["body"]))))
        meta["source"]["extract"] = {"kept": kept, "total": total}

    print("[정보] 요약 생성 중... ({} · 템플릿 {} v{})".format(
        describe_backend(backend), tpl.get("name", "?"), tpl.get("version", "?")))
    prompt = tmpl.build_prompt(tpl, body)
    summary = call_model(prompt, backend, meta["video"]["duration_sec"], tpl)

    meta["source"]["generated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    meta["source"]["model"] = backend["model"]
    meta["source"]["backend"] = backend["kind"]
    # 어떤 템플릿으로 만든 결과인지 남긴다. 템플릿이 바뀌면 산출물도 달라지기 때문
    meta["source"]["template"] = "{}@{}".format(tpl.get("name", "?"), tpl.get("version", "?"))
    payload = {"video": meta["video"], "source": meta["source"]}
    payload.update(summary)

    path = args.out or os.path.join(args.outdir, safe_name(meta["video"]["title"]) + ".json")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    existed = os.path.exists(path)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)

    print("[{}] {}".format("덮어씀" if existed else "완료", path))
    print("[완료] 핵심 {}개 · 섹션 {}개".format(len(payload["key_points"]), len(payload["sections"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
