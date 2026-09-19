#!/usr/bin/env python3
"""요약 JSON 하나를 채널별 형식으로 렌더링한다.

    render.py <요약.json> --to slack        # Block Kit 블록 (메인 메시지)
    render.py <요약.json> --to slack-thread # Block Kit 블록 (상세, 스레드 답글용)
    render.py <요약.json> --to kakao        # 순수 텍스트 (1000자 예산)
    render.py <요약.json> --to md           # 전체 문서

채널마다 문법이 다르기 때문에(슬랙 mrkdwn / 카카오톡 순수 텍스트 / 마크다운)
요약을 '완성된 텍스트'가 아니라 구조로 저장하고, 출력 직전에만 형식을 입힌다.
"""

import argparse
import json
import sys

import template as tmpl

# template.json 의 channels 에서 읽어 온다. main() 에서 실제 값으로 채운다
KAKAO_LIMIT = 1000          # 카카오톡 텍스트 메시지 실용 한도
SLACK_SECTION_LIMIT = 3000  # 슬랙 section 블록 text 한도


def hhmmss(sec):
    sec = int(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return "{:02d}:{:02d}:{:02d}".format(h, m, s) if h else "{:02d}:{:02d}".format(m, s)


def jump(video, t):
    return "{}&t={}s".format(video["url"], int(t))


def esc(text):
    """슬랙 mrkdwn 에서 특수 취급되는 문자만 이스케이프."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- 슬랙 -------------------------------------------------------------------

def render_slack(d):
    video, lines = d["video"], []
    for p in d["key_points"]:
        lines.append("• {}  <{}|{}>".format(esc(p["text"]), jump(video, p["t"]), hhmmss(p["t"])))
    body = "*{}*\n\n{}".format(esc(d["headline"]), "\n".join(lines))
    if len(body) > SLACK_SECTION_LIMIT:
        body = body[:SLACK_SECTION_LIMIT - 1] + "…"

    ctx = "<{}|{}> · {}".format(video["url"], esc(video["channel"]), hhmmss(video["duration_sec"]))
    if d.get("disclaimer"):
        ctx += " · " + esc(d["disclaimer"])
    return {"blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": video["title"][:150], "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": ctx}]},
    ]}


def render_slack_thread(d):
    """상세는 스레드 답글로 내려 채널 소음을 줄인다."""
    video, blocks = d["video"], []
    for sec in d["sections"]:
        lines = ["• {}  <{}|{}>".format(esc(p["text"]), jump(video, p["t"]), hhmmss(p["t"]))
                 for p in sec["points"]]
        text = "*{}*  <{}|{}>\n{}".format(
            esc(sec["title"]), jump(video, sec["t"]), hhmmss(sec["t"]), "\n".join(lines))
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text[:SLACK_SECTION_LIMIT]}})

    figures = d.get("entities", {}).get("figures") or []
    if figures:
        cells = "  ".join("`{} {}`".format(esc(x["label"]), esc(x["value"])) for x in figures)
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "언급된 수치 — " + cells}]})
    return {"blocks": blocks}


# --- 카카오톡 ----------------------------------------------------------------

def render_kakao(d):
    """마크다운 기호가 그대로 노출되므로 장식 없이, 링크는 맨 끝 1개만."""
    video = d["video"]
    out = [video["title"], "", d["headline"], ""]
    for p in d["key_points"]:
        out.append("- {} ({})".format(p["text"], hhmmss(p["t"])))
    out += ["", "{} · {}".format(video["channel"], hhmmss(video["duration_sec"])), video["url"]]
    if d.get("disclaimer"):
        out.append(d["disclaimer"])
    text = "\n".join(out)

    if len(text) > KAKAO_LIMIT:
        # 문장을 중간에서 자르지 않고 핵심 항목을 뒤에서부터 덜어낸다
        keep = list(d["key_points"])
        while keep and len(text) > KAKAO_LIMIT:
            keep.pop()
            out = [video["title"], "", d["headline"], ""]
            out += ["- {} ({})".format(p["text"], hhmmss(p["t"])) for p in keep]
            out += ["", "…전체 {}개 중 {}개".format(len(d["key_points"]), len(keep)),
                    video["url"], d["disclaimer"]]
            text = "\n".join(out)
        print("[경고] 카카오톡 {}자 한도로 핵심 항목 일부를 생략했습니다.".format(KAKAO_LIMIT),
              file=sys.stderr)
    return text


# --- 마크다운 ----------------------------------------------------------------

def render_md(d):
    video = d["video"]
    out = ["# {}".format(video["title"]), "",
           "- **채널**: {}".format(video["channel"]),
           "- **URL**: {}".format(video["url"]),
           "- **길이**: {}".format(hhmmss(video["duration_sec"])),
           "", "> {}".format(d["headline"]), "", "## 핵심 요약", ""]
    for p in d["key_points"]:
        out.append("- {} [`{}`]({})".format(p["text"], hhmmss(p["t"]), jump(video, p["t"])))

    out += ["", "## 상세", ""]
    for sec in d["sections"]:
        out += ["### {} [`{}`]({})".format(sec["title"], hhmmss(sec["t"]), jump(video, sec["t"])), ""]
        for p in sec["points"]:
            out.append("- {} [`{}`]({})".format(p["text"], hhmmss(p["t"]), jump(video, p["t"])))
        out.append("")

    figures = d.get("entities", {}).get("figures") or []
    if figures:
        out += ["## 언급된 수치", "", "| 항목 | 값 | 시점 |", "| --- | --- | --- |"]
        for x in figures:
            out.append("| {} | {} | [`{}`]({}) |".format(
                x["label"], x["value"], hhmmss(x["t"]), jump(video, x["t"])))
        out.append("")

    out += ["---", ""]
    if d.get("disclaimer"):
        out.append("*{}*".format(d["disclaimer"]))
    out.append("원본 자막: `{}`".format(d["source"]["transcript"]))

    # 일부 문단만 보고 만든 요약은 그 사실이 드러나야 한다.
    # 그렇지 않으면 전체를 읽고 만든 요약과 구분할 수 없다
    picked = d["source"].get("extract")
    if picked:
        out.append("※ 이 요약은 자막 {total}개 문단 중 {kept}개만 보고 작성됐습니다.".format(**picked))
    return "\n".join(out)


RENDERERS = {"slack": render_slack, "slack-thread": render_slack_thread,
             "kakao": render_kakao, "md": render_md}


def main():
    ap = argparse.ArgumentParser(description="요약 JSON 을 채널별 형식으로 렌더링")
    ap.add_argument("summary")
    ap.add_argument("--to", required=True, choices=sorted(RENDERERS))
    ap.add_argument("-o", "--out", help="파일로 저장(미지정 시 표준출력)")
    ap.add_argument("--template", help="요약 템플릿 .json (기본: 레포의 template.json)")
    args = ap.parse_args()

    # 채널 한도는 요약을 만들 때 쓴 것과 같은 템플릿에서 읽는다
    global KAKAO_LIMIT, SLACK_SECTION_LIMIT
    channels = tmpl.load(args.template)["channels"]
    KAKAO_LIMIT = channels["kakao_chars"]
    SLACK_SECTION_LIMIT = channels["slack_section_chars"]

    with open(args.summary, encoding="utf-8") as fp:
        data = json.load(fp)

    result = RENDERERS[args.to](data)
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fp:
            fp.write(text + ("\n" if not text.endswith("\n") else ""))
        print("[완료] {} ({}자)".format(args.out, len(text)))
    else:
        print(text)


if __name__ == "__main__":
    main()
