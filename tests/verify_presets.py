#!/usr/bin/env python3
"""요약 프리셋(짧게·기본·길게·쉽게)이 프롬프트와 검증·보정에 함께 적용되는지 확인한다.

모델은 부르지 않는다. tests/fixtures/base_summary.json 을 프리셋별 한도로 검사·보정한다.
"""
import contextlib, copy, importlib.util, io, json, os, subprocess, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)
import template as tmpl

spec = importlib.util.spec_from_file_location("sm", os.path.join(REPO, "summarize.py"))
sm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sm)

BASE = json.load(open("tests/fixtures/base_summary.json", encoding="utf-8"))
TPL = tmpl.load()
failures = []


def check(name, got, want):
    ok = got == want
    print("{} {}".format("통과" if ok else "실패", name))
    if not ok:
        print("     기대: {!r}\n     실제: {!r}".format(want, got))
        failures.append(name)


def prompt(name):
    return tmpl.build_prompt(tmpl.apply_preset(TPL, name), "<자막>")


check("프리셋 4종", tmpl.preset_names(TPL), ["default", "short", "long", "easy"])
check("default 는 원래 템플릿과 같은 프롬프트", prompt("default"), tmpl.build_prompt(TPL, "<자막>"))
check("short 한도가 프롬프트에 들어감",
      ("`key_points` 는 2~3개" in prompt("short"), "`sections` 는 2~3개" in prompt("short")), (True, True))
check("long 한도가 프롬프트에 들어감",
      ("`key_points` 는 4~7개" in prompt("long"), "`sections` 는 5~10개" in prompt("long")), (True, True))
check("easy 는 한도 그대로, 규칙만 덧붙음",
      (tmpl.apply_preset(TPL, "easy")["limits"], "**쉽게 씁니다.**" in prompt("easy")), (TPL["limits"], True))
check("원본 템플릿은 바뀌지 않음", (TPL["limits"]["key_points"], len(TPL["rules"])),
      (json.load(open("template.json", encoding="utf-8"))["limits"]["key_points"], 8))

try:
    tmpl.apply_preset(TPL, "nope")
    check("없는 프리셋은 거절", "통과됨", "ValueError")
except ValueError:
    check("없는 프리셋은 거절", "ValueError", "ValueError")

check("presets 가 없는 옛 템플릿은 default 만", tmpl.preset_names({"limits": {}}), ["default"])

# 검증·보정이 프리셋 한도를 따른다: 핵심 7개 · 섹션 8개짜리 요약
data = copy.deepcopy(BASE)
data["key_points"] = (data["key_points"] * 3)[:7]
data["sections"] = (data["sections"] * 4)[:8]
dur = data["video"]["duration_sec"]
for name, want in (("short", (3, 3)), ("default", (5, 6)), ("long", (7, 8))):
    tpl = tmpl.apply_preset(TPL, name)
    d = copy.deepcopy(data)
    with contextlib.redirect_stderr(io.StringIO()):
        _, soft = sm.check(d, dur, tpl)
        sm.repair(d, dur, soft, tpl)
    check("{} 보정 결과 핵심·섹션 수".format(name), (len(d["key_points"]), len(d["sections"])), want)

proc = subprocess.run([sys.executable, "summarize.py", "x.md", "--preset", "nope"],
                      capture_output=True, text=True, env=dict(os.environ, YT_LLM_BASE_URL=""))
check("summarize.py 는 없는 프리셋에 모델을 부르지 않고 멈춤",
      (proc.returncode != 0, "프리셋 'nope'" in proc.stderr), (True, True))

print()
print("실패 {}건".format(len(failures)) if failures else "모두 통과")
sys.exit(1 if failures else 0)
