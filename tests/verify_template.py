#!/usr/bin/env python3
"""요약 템플릿이 실제로 강제되는지 확인한다.

SCHEMA_PROMPT 가 명시한 규칙을 하나씩 위반한 페이로드를 만들어
validate() 와 각 렌더러에 통과시킨 뒤, 결과를 세 가지로 분류한다.

  차단  — validate() 가 잡아서 실패시킴 (템플릿이 강제됨)
  통과  — 규칙 위반인데 아무 일도 없이 지나감 (템플릿이 강제되지 않음)
  터짐  — 렌더 단계에서 예외 (검증 구멍이 실제 크래시로 이어짐)
"""
import copy, importlib.util, json, os, sys, contextlib, io

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sm = load("sm", "summarize.py")
rd = load("rd", "render.py")
TPL = load("tmpl_mod", "template.py").load()
BASE = json.load(open("tests/fixtures/base_summary.json", encoding="utf-8"))


def over_count(d):
    d["key_points"] = (d["key_points"] * 2)[:8]          # 규칙: 3~5개


def under_count(d):
    d["sections"] = d["sections"][:1]                     # 규칙: 3~6개


def long_headline(d):
    d["headline"] = "가" * 300                            # 규칙: 100자 이내


def missing_text(d):
    d["key_points"][0].pop("text")                        # 스키마: text 필수


def missing_points(d):
    d["sections"][0].pop("points")                        # 스키마: points 필수


def missing_label(d):
    d["entities"]["figures"][0].pop("label")              # 스키마: label 필수


def t_out_of_range(d):
    d["key_points"][0]["t"] = 999999                      # 영상 길이 밖


def t_negative(d):
    d["key_points"][0]["t"] = -5


def kp_not_list(d):
    d["key_points"] = "핵심 요약입니다"                     # 스키마: 배열


def section_not_dict(d):
    d["sections"][0] = "섹션 제목"                      # 스키마: 객체


CASES = [
    ("key_points 8개 (규칙 3~5개)", over_count),
    ("sections 1개 (규칙 3~6개)", under_count),
    ("headline 300자 (규칙 100자 이내)", long_headline),
    ("key_points[0].text 누락", missing_text),
    ("sections[0].points 누락", missing_points),
    ("figures[0].label 누락", missing_label),
    ("t=999999 (영상 길이 밖)", t_out_of_range),
    ("t=-5 (음수)", t_negative),
    ("key_points 가 배열이 아닌 문자열", kp_not_list),
    ("sections[0] 이 객체가 아닌 문자열", section_not_dict),
]

DURATION = BASE["video"]["duration_sec"]
print("영상 길이: {}초\n".format(DURATION))
print("{:<34} {:<10} {}".format("위반 내용", "validate", "렌더러(md/kakao/slack)"))
print("-" * 76)

for label, fn in CASES:
    data = copy.deepcopy(BASE)
    fn(data)

    # 1단계: check() 가 잡아내는가 → fatal 이면 모델에 되먹여 재시도(=차단)
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            fatal, soft = sm.check(copy.deepcopy(data), DURATION, TPL)
            checked = None if fatal else sm.repair(copy.deepcopy(data), DURATION, soft, TPL)
        verdict = "차단(재시도)" if fatal else ("보정" if (soft or err.getvalue().strip()) else "통과")
    except Exception as exc:
        verdict, checked = "터짐({})".format(type(exc).__name__), None

    # 2단계: validate 를 통과한 값이 렌더러에서 살아남는가
    results = []
    if checked is not None:
        for name, fn2 in (("md", rd.render_md), ("kakao", rd.render_kakao), ("slack", rd.render_slack)):
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    fn2(copy.deepcopy(checked))
                results.append(name + ":ok")
            except Exception as exc:
                results.append("{}:{}".format(name, type(exc).__name__))
    else:
        results = ["-"]

    print("{:<34} {:<10} {}".format(label, verdict, " ".join(results)))
