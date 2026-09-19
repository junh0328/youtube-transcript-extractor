#!/usr/bin/env python3
"""요약에 나온 수치가 원문 자막에 실제로 있는지 대조한다. LLM 없이 순수 계산.

    grounding.py <요약.json> [--window 90] [--verbose]

요약의 각 주장에는 `t`(초)가 붙어 있다. 그 시점 앞뒤 구간의 자막만 꺼내
주장 속 숫자가 거기에 실제로 등장하는지 확인한다.

숫자를 고르는 이유: 지어낸 목표가·비율처럼 가장 해로운 할루시네이션이 숫자이고,
숫자는 번역·전사 표기가 달라도 정규화하면 비교할 수 있기 때문이다.
('90K', '$90,000', '9만 달러' 는 모두 90000 으로 맞춘다)
"""

import argparse
import json
import re
import sys

SCALE_KO = {"천": 10 ** 3, "만": 10 ** 4, "억": 10 ** 8, "조": 10 ** 12}
SCALE_EN = {"k": 10 ** 3, "m": 10 ** 6, "b": 10 ** 9, "t": 10 ** 12}
# 한국어 화자는 영어 자릿수를 음차해 말하기도 한다 ('10밀리언 단위')
SCALE_KO_LOAN = {"밀리언": 10 ** 6, "빌리언": 10 ** 9, "트릴리언": 10 ** 12}

# 숫자로 세지 않을 값. 연도·한 자리 수는 우연히 일치할 확률이 높아 신호가 되지 못한다
TRIVIAL = set(range(0, 10))


SCALE_WORD = {"thousand": 10 ** 3, "million": 10 ** 6,
              "billion": 10 ** 9, "trillion": 10 ** 12}

# 단위가 붙은 숫자.
#   'the 90ks'  → 구어체 복수형 s
#   '76.5K의롱' → 한국어는 조사가 단위에 바로 붙는다. 뒤가 한글이어도 단위로 인정해야 한다
#   차단할 것은 'MB' 처럼 영숫자가 이어져 다른 토큰이 되는 경우뿐이다
SCALED = [
    (re.compile(r"([\d,]*\d(?:\.\d+)?)\s*(밀리언|빌리언|트릴리언)"), SCALE_KO_LOAN, None),
    (re.compile(r"([\d,]*\d(?:\.\d+)?)\s*([천만억조])"), SCALE_KO, None),
    (re.compile(r"([\d,]*\d(?:\.\d+)?)\s*([KkMmBbTt])s?(?![A-Za-z0-9])"), SCALE_EN, str.lower),
    (re.compile(r"([\d,]*\d(?:\.\d+)?)\s*(thousand|million|billion|trillion)s?", re.I),
     SCALE_WORD, str.lower),
]
BARE = re.compile(r"[\d,]*\d(?:\.\d+)?")


def _to_float(text):
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


# '68~70K' 는 68K~70K 라는 뜻이다. 앞 숫자에도 단위를 붙여준다
RANGE = re.compile(
    r"(\d[\d,.]*)\s*[~\-–]\s*(\d[\d,.]*)\s*([KkMmBbTt만억조]|thousand|million|billion|trillion)",
    re.I)


def expand_ranges(text):
    return RANGE.sub(lambda m: "{}{}~{}{}".format(
        m.group(1), m.group(3), m.group(2), m.group(3)), text)


def _aliases(value):
    """같은 값의 허용 표기들. 하나라도 원문에 있으면 근거가 있는 것으로 본다."""
    base = int(value) if value == int(value) else value
    out = {base}
    # 0.618 같은 소수 비율은 말할 때 앞의 '0.' 을 빼고 '618' 이라 한다
    if isinstance(base, float) and 0 < base < 1 and base * 1000 == int(base * 1000):
        out.add(int(base * 1000))
    return {x for x in out if x not in TRIVIAL}


def _extract(text):
    """(위치, 별칭집합) 목록. 단위에 먹힌 자릿수는 맨숫자로 다시 세지 않는다."""
    if not text:
        return []
    text = expand_ranges(text)
    out, consumed = [], []

    for pattern, scale, key in SCALED:
        for m in pattern.finditer(text):
            value = _to_float(m.group(1))
            unit = key(m.group(2)) if key else m.group(2)
            if value is not None and unit in scale:
                out.append((m.start(), _aliases(value * scale[unit])))
                consumed.append((m.start(1), m.end(1)))

    for m in BARE.finditer(text):
        if any(s <= m.start() < e for s, e in consumed):
            continue
        value = _to_float(m.group())
        if value is not None:
            out.append((m.start(), _aliases(value)))

    return [(pos, aliases) for pos, aliases in out if aliases]


def normalize_numbers(text):
    """원문 쪽: 등장하는 모든 표기를 평평한 집합으로."""
    found = set()
    for _, aliases in _extract(text):
        found |= aliases
    return found


def logical_numbers(text):
    """요약 쪽: 논리적 숫자 하나당 별칭집합 하나. 이중 계산을 막는다."""
    return [aliases for _, aliases in _extract(text)]


def parse_transcript(path):
    """자막 .md 를 [(초, 본문)] 으로 읽는다."""
    rows = []
    with open(path, encoding="utf-8") as fp:
        for line in fp:
            m = re.match(r"\[`[\d:]+`\]\([^)]*[?&]t=(\d+)s\)\s*(.+)", line.strip())
            if m:
                rows.append((int(m.group(1)), m.group(2)))
    return rows


def window_text(rows, t, window):
    """t 앞뒤 window 초 구간의 자막을 이어붙인다."""
    return " ".join(text for sec, text in rows if t - window <= sec <= t + window)


def claims(summary):
    """대조할 주장들을 (경로, 문자열, t) 로 펼친다."""
    out = []
    for i, p in enumerate(summary.get("key_points", [])):
        out.append(("key_points[{}]".format(i), p.get("text", ""), p.get("t", 0)))
    for i, sec in enumerate(summary.get("sections", [])):
        for j, p in enumerate(sec.get("points", [])):
            out.append(("sections[{}].points[{}]".format(i, j), p.get("text", ""), p.get("t", 0)))
    for i, fig in enumerate(summary.get("entities", {}).get("figures", [])):
        label = "{} {}".format(fig.get("label", ""), fig.get("value", ""))
        out.append(("figures[{}]".format(i), label, fig.get("t", 0)))
    return out


def audit(summary, rows, window):
    """주장별로 숫자가 원문에 있는지 확인한다."""
    results, whole = [], None
    for where, text, t in claims(summary):
        groups = logical_numbers(text)
        if not groups:
            continue
        local = normalize_numbers(window_text(rows, t, window))
        for aliases in groups:
            shown = sorted(aliases, key=str)[0]
            if aliases & local:
                verdict = "근거 있음"
            else:
                if whole is None:  # 구간엔 없지만 영상 어딘가엔 있을 수 있다
                    whole = normalize_numbers(" ".join(x[1] for x in rows))
                verdict = "구간 밖" if aliases & whole else "원문에 없음"
            results.append({"where": where, "t": t, "value": shown,
                            "verdict": verdict, "text": text})
    return results


def main():
    ap = argparse.ArgumentParser(description="요약의 수치를 원문 자막과 대조")
    ap.add_argument("summary")
    ap.add_argument("--window", type=int, default=90, help="대조할 앞뒤 구간(초)")
    ap.add_argument("--verbose", action="store_true", help="근거 있는 항목도 모두 출력")
    args = ap.parse_args()

    with open(args.summary, encoding="utf-8") as fp:
        summary = json.load(fp)

    transcript = summary.get("source", {}).get("transcript")
    rows = parse_transcript(transcript)
    if not rows:
        sys.exit("[에러] 자막을 읽지 못했습니다: {}".format(transcript))

    results = audit(summary, rows, args.window)
    counts = {"근거 있음": 0, "구간 밖": 0, "원문에 없음": 0}
    for r in results:
        counts[r["verdict"]] += 1

    total = len(results)
    print("대조 대상 수치 {}개 (앞뒤 {}초 구간)".format(total, args.window))
    if not total:
        print("  검사할 숫자가 없습니다.")
        return 0
    for verdict, n in counts.items():
        print("  {:<10} {:>3}개  ({:.0f}%)".format(verdict, n, 100 * n / total))

    flagged = [r for r in results if r["verdict"] != "근거 있음"]
    if flagged or args.verbose:
        print()
        for r in (results if args.verbose else flagged):
            print("  [{}] {} t={} · {}".format(r["verdict"], r["value"], r["t"], r["where"]))
            print("      {}".format(r["text"][:80]))

    return 1 if counts["원문에 없음"] else 0


if __name__ == "__main__":
    sys.exit(main())
