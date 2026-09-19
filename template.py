#!/usr/bin/env python3
"""요약 템플릿을 한 곳에서 읽는다.

프롬프트(summarize.py), 검증(summarize.py), 렌더러(render.py) 가 모두 이 모듈을 통해
같은 template.json 을 본다. 규칙이 코드 여기저기 흩어져 조용히 어긋나는 걸 막기 위해서다.

    python3 template.py        # 현재 템플릿이 만들어내는 프롬프트를 확인
"""

import json
import os

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.json")


def load(path=None):
    """template.json 을 읽는다. --template, YT_TEMPLATE, 기본 경로 순으로 찾는다."""
    path = path or os.environ.get("YT_TEMPLATE") or DEFAULT_PATH
    with open(path, encoding="utf-8") as fp:
        tpl = json.load(fp)
    for key in ("limits", "channels", "schema", "rules"):
        if key not in tpl:
            raise ValueError("템플릿에 '{}' 가 없습니다: {}".format(key, path))
    return tpl


def placeholders(tpl):
    """규칙·스키마 문장에 끼워 넣을 값들. limits 의 [min, max] 는 _min/_max 로 펼친다."""
    out = {"output_language": tpl.get("output_language", "한국어")}
    for key, value in tpl["limits"].items():
        if isinstance(value, list) and len(value) == 2:
            out[key + "_min"], out[key + "_max"] = value
        else:
            out[key] = value
    return out


def _fill(node, values):
    if isinstance(node, str):
        try:
            return node.format(**values)
        except (KeyError, IndexError):
            return node  # 템플릿 작성자가 쓴 중괄호는 건드리지 않는다
    if isinstance(node, list):
        return [_fill(x, values) for x in node]
    if isinstance(node, dict):
        return {k: _fill(v, values) for k, v in node.items()}
    return node


def build_prompt(tpl, transcript_body):
    """템플릿에서 모델에 보낼 프롬프트를 만든다."""
    values = placeholders(tpl)
    schema = json.dumps(_fill(tpl["schema"], values), ensure_ascii=False, indent=2)
    rules = "\n".join("{}. {}".format(i + 1, r)
                      for i, r in enumerate(_fill(tpl["rules"], values)))

    return (
        "당신은 유튜브 영상 자막을 {lang}로 요약합니다.\n\n"
        "아래 자막을 읽고 **JSON 만** 출력하세요. "
        "설명, 인사말, 마크다운 코드펜스 없이 JSON 그 자체만 출력합니다.\n\n"
        "## 출력 스키마\n\n{schema}\n\n## 규칙\n\n{rules}\n\n## 자막\n\n{body}"
    ).format(lang=values["output_language"], schema=schema, rules=rules, body=transcript_body)


def span(tpl, key):
    """limits 의 [min, max] 를 튜플로 꺼낸다."""
    lo, hi = tpl["limits"][key]
    return lo, hi


if __name__ == "__main__":
    t = load()
    print(build_prompt(t, "<여기에 자막 본문이 들어갑니다>"))
