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
      ("`key_points` 는 4~7개" in prompt("long"), "`sections` 는 5~14개" in prompt("long")), (True, True))
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
data["key_points"] = copy.deepcopy((data["key_points"] * 3)[:7])


def numbered(sections, n):
    """서로 다른 객체·제목의 섹션 n개 (리스트 곱은 같은 객체를 공유해 병합 검사가 틀어진다)."""
    out = [copy.deepcopy(sections[i % len(sections)]) for i in range(n)]
    for i, sec in enumerate(out):
        sec["title"] = "섹션{:02d}".format(i)
        sec["t"] = i * 60
    return out


data["sections"] = numbered(data["sections"], 8)
dur = data["video"]["duration_sec"]
for name, want in (("short", (3, 3)), ("default", (5, 6)), ("long", (7, 8))):
    tpl = tmpl.apply_preset(TPL, name)
    d = copy.deepcopy(data)
    with contextlib.redirect_stderr(io.StringIO()):
        _, soft = sm.check(d, dur, tpl)
        sm.repair(d, dur, soft, tpl)
    check("{} 보정 결과 핵심·섹션 수".format(name), (len(d["key_points"]), len(d["sections"])), want)
    check("{} 보정 뒤에도 마지막 섹션(영상 후반부)이 남음".format(name),
          d["sections"][-1]["title"].endswith(data["sections"][-1]["title"]), True)

# 섹션이 넘치면 뒤를 자르기 전에 합쳐 다시 쓰게 한다 (자르면 영상 후반부가 빠진다)
def fake_sender(replies):
    sent = []
    def send(prompt, backend):
        sent.append(prompt)
        return json.dumps(replies[min(len(sent), len(replies)) - 1], ensure_ascii=False), None
    return send, sent

tpl_long = tmpl.apply_preset(TPL, "long")
over, fit = copy.deepcopy(BASE), copy.deepcopy(BASE)
over["sections"] = numbered(over["sections"], 17)
fit["sections"] = numbered(fit["sections"], 8)
orig_send = sm.send
sm.send, sent = fake_sender([over, fit])
with contextlib.redirect_stderr(io.StringIO()):
    got = sm.call_model("P", {}, dur, tpl_long)
check("섹션 초과는 합쳐 다시 요청 (2회 호출 · 8개 · 되먹임에 상한)",
      (len(sent), len(got["sections"]), "상한 14개" in sent[1]), (2, 8, True))
sm.send, sent = fake_sender([over])
err = io.StringIO()
with contextlib.redirect_stderr(err):
    got = sm.call_model("P", {}, dur, tpl_long)
check("끝까지 넘치면 그때 이웃끼리 합쳐 맞추고 경고 · 마지막 섹션(결론)이 남음",
      (len(sent), len(got["sections"]), "합쳐" in err.getvalue(),
       got["sections"][-1]["t"] == over["sections"][-1]["t"] or got["sections"][-1]["title"].endswith(over["sections"][-1]["title"])),
      (3, 14, True, True))
sm.send = orig_send

# 장면 후보(scenes[])는 켰을 때만 프롬프트에 들어가고, 프리셋 한도는 그대로 둔다
scn = tmpl.with_scenes(tmpl.apply_preset(TPL, "long"))
check("scenes 는 기본 프롬프트에 없음", "`scenes`" in prompt("default"), False)
check("with_scenes: 규칙·스키마·한도가 붙고 프리셋 한도는 유지",
      ("`scenes` 는 **화면을 봐야 이해되는 시각** 0~6개" in tmpl.build_prompt(scn, "<자막>"),
       "scenes" in scn["schema"], scn["limits"]["sections"]), (True, True, [5, 14]))
with contextlib.redirect_stderr(io.StringIO()):
    got = sm.repair_scenes([{"t": 300, "caption": "차트"}, {"t": "100"}, {"t": 105, "caption": "겹침"},
                            "깨짐", {"caption": "시각 없음"}, {"t": 99999, "caption": "끝"}], 1200, 6)
check("scenes 정리: 시각순 · 15초 안 겹침 제거 · 깨진 항목 제거 · 길이로 누름",
      got, [{"t": 100, "caption": ""}, {"t": 300, "caption": "차트"}, {"t": 1200, "caption": "끝"}])
with contextlib.redirect_stderr(io.StringIO()):
    check("scenes 상한", len(sm.repair_scenes([{"t": i * 60} for i in range(10)], 1200, 6)), 6)
    d = copy.deepcopy(BASE)
    sm.repair(d, dur, [], tmpl.apply_preset(TPL, "default"))
check("scenes 를 요청하지 않으면 repair 가 만들지 않음", "scenes" in d, False)

proc = subprocess.run([sys.executable, "summarize.py", "x.md", "--preset", "nope"],
                      capture_output=True, text=True, env=dict(os.environ, YT_LLM_BASE_URL=""))
check("summarize.py 는 없는 프리셋에 모델을 부르지 않고 멈춤",
      (proc.returncode != 0, "프리셋 'nope'" in proc.stderr), (True, True))

print()
print("실패 {}건".format(len(failures)) if failures else "모두 통과")
sys.exit(1 if failures else 0)
