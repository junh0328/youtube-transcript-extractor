#!/usr/bin/env python3
"""기본 모델 전송이 실패하면 백업 모델(YT_LLM_FALLBACK_MODEL)로 넘어가는지 확인한다.

모델은 부르지 않는다. 가짜 send 가 모델 이름별로 정해 둔 응답을 돌려준다.
"""
import contextlib, importlib.util, io, json, os, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)
import template as tmpl
import frames as fr

spec = importlib.util.spec_from_file_location("sm", os.path.join(REPO, "summarize.py"))
sm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sm)
sys.modules["summarize"] = sm  # frames.review 가 import summarize 로 같은 모듈을 쓰게 한다

BASE = json.load(open("tests/fixtures/base_summary.json", encoding="utf-8"))
TPL = tmpl.load()
DUR = BASE["video"]["duration_sec"]
sm.time.sleep = fr.time.sleep = lambda s: None
failures = []


def check(name, got, want):
    ok = got == want
    print("{} {}".format("통과" if ok else "실패", name))
    if not ok:
        print("     기대: {!r}\n     실제: {!r}".format(want, got))
        failures.append(name)


def fake(replies):
    """replies: {모델: [응답 또는 에러 문자열("HTTP ..."), ...]}. 목록이 다 떨어지면 마지막 값을 되풀이한다."""
    calls = []
    def send(prompt, backend):
        model = backend["model"]
        calls.append(model)
        seq = replies[model]
        r = seq[min(calls.count(model), len(seq)) - 1]
        return (None, r) if r.startswith("HTTP") else (r, None)
    return send, calls


def backend(fallback="gemini-2.5-flash"):
    return {"kind": "openai", "base_url": "x", "api_key": "", "model": "gemini-3.5-flash", "fallback": fallback}


OK = json.dumps(BASE, ensure_ascii=False)
E503 = "HTTP 503: high demand"

# 1. 503 이 2번 이어지면 백업으로 넘어가 요약을 받고, 기록할 모델도 백업이 된다
sm.send, calls = fake({"gemini-3.5-flash": [E503], "gemini-2.5-flash": [OK]})
b = backend()
err = io.StringIO()
with contextlib.redirect_stderr(err):
    got = sm.call_model("P", b, DUR, TPL)
check("503 2회 뒤 백업으로 전환 (호출 순서 · 기록 모델 · [전환] 로그)",
      (calls, b["model"], "[전환]" in err.getvalue(), bool(got.get("sections"))),
      (["gemini-3.5-flash", "gemini-3.5-flash", "gemini-2.5-flash"], "gemini-2.5-flash", True, True))

# 2. 404 는 기다리지 않고 바로 넘어간다
sm.send, calls = fake({"gemini-3.5-flash": ["HTTP 404: not found"], "gemini-2.5-flash": [OK]})
with contextlib.redirect_stderr(io.StringIO()):
    sm.call_model("P", backend(), DUR, TPL)
check("404 는 1회 만에 전환", calls, ["gemini-3.5-flash", "gemini-2.5-flash"])

# 3. 한 번만 실패하고 회복하면 기본 모델을 그대로 쓴다
sm.send, calls = fake({"gemini-3.5-flash": [E503, OK]})
b = backend()
with contextlib.redirect_stderr(io.StringIO()):
    sm.call_model("P", b, DUR, TPL)
check("1회 실패 뒤 회복하면 전환 없음", (calls, b["model"]), (["gemini-3.5-flash"] * 2, "gemini-3.5-flash"))

# 4. 스키마가 깨진 응답은 전환 조건이 아니다 — 같은 모델에 되먹여 다시 쓰게 한다
sm.send, calls = fake({"gemini-3.5-flash": ["모르겠습니다", "{}", OK]})
with contextlib.redirect_stderr(io.StringIO()):
    sm.call_model("P", backend(), DUR, TPL)
check("스키마 실패는 전환 없이 기본 모델로 재시도", calls, ["gemini-3.5-flash"] * 3)

# 5. 백업이 없으면 지금처럼 3회 시도 뒤 끝낸다
sm.send, calls = fake({"gemini-3.5-flash": [E503]})
code = None
with contextlib.redirect_stderr(io.StringIO()):
    try:
        sm.call_model("P", backend(fallback=""), DUR, TPL)
    except SystemExit:
        code = "exit"
check("백업 없으면 3회 뒤 종료", (calls, code), (["gemini-3.5-flash"] * 3, "exit"))

# 6. 장면 판별(frames.review)도 같은 규칙으로 넘어간다
picks = json.dumps({"picks": [{"candidate": 1, "pick": 1, "reason": "r"}]})
send, calls = fake({"gemini-3.5-flash": [E503], "gemini-2.5-flash": [picks]})
with contextlib.redirect_stderr(io.StringIO()):
    got = fr.review([("c", [(10, "a.jpg")])], backend(), send=send)
check("장면 판별도 2회 실패 뒤 백업으로 전환", (calls, got),
      (["gemini-3.5-flash", "gemini-3.5-flash", "gemini-2.5-flash"], [(1, "r")]))

# 7. 기본 모델과 같은 이름의 백업은 무시한다
os.environ.update(YT_LLM_BASE_URL="x", YT_LLM_MODEL="m", YT_LLM_FALLBACK_MODEL="m")
check("백업이 기본 모델과 같으면 비움", sm.resolve_backend(None)["fallback"], "")

print("\n{}".format("모두 통과" if not failures else "실패 {}건".format(len(failures))))
sys.exit(1 if failures else 0)
