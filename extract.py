#!/usr/bin/env python3
"""자막에서 중요한 문단만 골라낸다. LLM 없이 TextRank 로 계산한다.

    extract.py <자막.md> --top 12

요약 전에 이 단계를 끼우면 모델이 보는 원문이 좁아진다.
모델이 볼 수 없는 내용은 지어낼 수도 없으므로 할루시네이션 여지가 줄고,
입력 토큰도 함께 줄어든다.

형태소 분석기를 쓰지 않고 **문자 3-gram** 으로 문단 간 유사도를 잰다.
언어를 가리지 않고(영어·한국어 모두) 외부 의존성이 없으며,
한국어 조사 변화('비트코인이'/'비트코인은')에도 어느 정도 견딘다.
"""

import argparse
import math
import re
import sys
from collections import Counter

LINE = re.compile(r"^\[`[\d:]+`\]\([^)]*[?&]t=(\d+)s\)\s*(.+)$")


def read_chunks(path):
    """자막 .md 에서 [(초, 본문, 원본줄)] 을 읽는다."""
    chunks = []
    with open(path, encoding="utf-8") as fp:
        for raw in fp:
            m = LINE.match(raw.strip())
            if m:
                chunks.append((int(m.group(1)), m.group(2), raw.rstrip("\n")))
    return chunks


def ngrams(text, n=3):
    """문자 n-gram 빈도. 공백을 눌러 붙여 띄어쓰기 차이에 덜 민감하게 만든다."""
    flat = re.sub(r"\s+", " ", text).strip().lower()
    return Counter(flat[i:i + n] for i in range(max(0, len(flat) - n + 1)))


def weighted_vectors(texts, n=3):
    """n-gram 에 IDF 가중치를 준다.

    가중치가 없으면 공백이나 조사처럼 어디에나 있는 조각이 유사도를 지배해서
    모든 문단이 고만고만하게 닮아 보이고, 결국 중요도 순위가 거의 무작위가 된다.
    """
    grams = [ngrams(t, n) for t in texts]
    total = len(grams)
    df = Counter()
    for g in grams:
        for key in g:
            df[key] += 1
    return [{k: v * math.log(1 + total / (1 + df[k])) for k, v in g.items()} for g in grams]


def cosine(a, b):
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[g] * b[g] for g in common)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def textrank(texts, damping=0.85, rounds=40):
    """문단 유사도 그래프에 PageRank 를 돌려 점수를 매긴다."""
    n = len(texts)
    if n <= 1:
        return [1.0] * n

    vectors = weighted_vectors(texts)
    sim = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            s = cosine(vectors[i], vectors[j])
            sim[i][j] = sim[j][i] = s

    # 행 정규화. 이웃이 없는 문단은 균등하게 퍼뜨린다
    weights = []
    for row in sim:
        total = sum(row)
        weights.append([v / total for v in row] if total else [1.0 / n] * n)

    score = [1.0 / n] * n
    for _ in range(rounds):
        nxt = [(1 - damping) / n] * n
        for i in range(n):
            for j in range(n):
                if weights[i][j]:
                    nxt[j] += damping * score[i] * weights[i][j]
        if max(abs(a - b) for a, b in zip(score, nxt)) < 1e-6:
            score = nxt
            break
        score = nxt
    return score


def select(path, top):
    """상위 top 개 문단을 골라 원본 줄 그대로, 시간순으로 돌려준다."""
    if not isinstance(top, int) or top < 1:
        sys.exit("[에러] 선별할 문단 수는 1 이상이어야 합니다: {}".format(top))
    chunks = read_chunks(path)
    if not chunks:
        # 타임스탬프 링크가 없는 자막(예: URL 없이 --from-json 으로 만든 것)은 선별할 수 없다
        sys.exit("[에러] 타임스탬프 문단을 찾지 못했습니다(형식 불일치): {}".format(path))
    if top >= len(chunks):
        return [c[2] for c in chunks], len(chunks), len(chunks)

    scores = textrank([c[1] for c in chunks])
    ranked = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)[:top]
    kept = sorted(ranked)  # 시간순으로 되돌려 흐름을 유지한다
    return [chunks[i][2] for i in kept], len(kept), len(chunks)


def main():
    ap = argparse.ArgumentParser(description="자막에서 중요 문단만 TextRank 로 선별")
    ap.add_argument("transcript")
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    lines, kept, total = select(args.transcript, args.top)
    print("[정보] 문단 {}개 중 {}개 선별".format(total, kept), file=sys.stderr)
    print("\n\n".join(lines))


if __name__ == "__main__":
    main()
