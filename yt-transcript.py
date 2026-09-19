#!/usr/bin/env python3
"""유튜브 영상의 자막을 타임스탬프와 함께 .md 로 추출한다.

기본 동작: 한국어 자막(영어 영상이면 자동 번역본)을 45초 단위로 문단화한 .md 한 개.

사용법:
    yt-transcript.py <URL> [--lang ko] [--format md|json|both] [--outdir .]
                          [--chunk 45] [--no-auto] [--from-json PATH]

옵션:
    --lang    선호 언어 코드(쉼표로 여러 개, 앞에서부터 우선). 기본: ko,en-orig,en
    --format  출력 형식. 기본: md
    --outdir  출력 디렉터리. 기본: 현재 디렉터리
    --chunk   N초 단위로 자막을 병합해 문단화(0이면 원본 세그먼트 유지). 기본: 45
    --no-auto 자동 생성 자막(ASR)은 제외하고 수동 자막만 사용
    --from-json  이미 추출한 .json 을 재가공(네트워크 요청 없음)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def fetch_metadata(url):
    proc = run(["yt-dlp", "--dump-json", "--skip-download", "--no-warnings", url])
    if proc.returncode != 0:
        sys.exit("[에러] 영상 정보를 가져오지 못했습니다:\n" + proc.stderr.strip())
    return json.loads(proc.stdout)


def _warn_fallback(chosen, wanted):
    """1순위 언어를 못 구하고 폴백했으면 조용히 넘어가지 않고 알린다."""
    if wanted and chosen.split("-")[0] != wanted[0].split("-")[0]:
        print("[경고] '{}' 자막이 없어 '{}' 로 대체합니다. 한국어 결과가 아닙니다.".format(
            wanted[0], chosen))
    return chosen


def pick_language(meta, wanted, allow_auto):
    """선호 언어 순서대로 사용 가능한 자막 트랙을 고른다."""
    manual = meta.get("subtitles") or {}
    auto = (meta.get("automatic_captions") or {}) if allow_auto else {}

    for lang in wanted:
        for pool, is_auto in ((manual, False), (auto, True)):
            # 정확히 일치 → ko
            if lang in pool:
                return _warn_fallback(lang, wanted), is_auto
            # 접두사 일치 → ko-KR, en-US, en-orig 등
            for code in pool:
                if code.split("-")[0] == lang:
                    return _warn_fallback(code, wanted), is_auto

    available = sorted(set(manual) | set(auto))
    sys.exit(
        "[에러] 요청한 언어의 자막이 없습니다: {}\n사용 가능한 자막: {}".format(
            ", ".join(wanted), ", ".join(available) or "(없음)"
        )
    )


def download_subtitle(url, lang, is_auto, workdir, retries=4):
    """자막 json3 를 내려받는다. 유튜브가 자주 돌려주는 429 에는 백오프 후 재시도."""
    flag = "--write-auto-subs" if is_auto else "--write-subs"
    cmd = [
        "yt-dlp", flag, "--skip-download", "--no-warnings",
        "--sub-langs", lang, "--sub-format", "json3",
        "-o", os.path.join(workdir, "sub.%(ext)s"), url,
    ]
    last = ""
    for attempt in range(retries):
        proc = run(cmd)
        files = [f for f in os.listdir(workdir) if f.endswith(".json3")]
        if files:
            with open(os.path.join(workdir, files[0]), encoding="utf-8") as fp:
                return json.load(fp)
        last = (proc.stderr or proc.stdout).strip()
        if "429" not in last or attempt == retries - 1:
            break
        wait = 20 * (attempt + 1)
        print("[재시도] 429(요청 과다). {}초 대기 후 {}/{}회차".format(wait, attempt + 2, retries))
        time.sleep(wait)
    sys.exit("[에러] 자막 파일을 내려받지 못했습니다:\n" + last)


def parse_json3(data):
    """json3 이벤트를 {start, end, text} 세그먼트 리스트로 변환한다."""
    segments = []
    for event in data.get("events", []):
        segs = event.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs)
        text = re.sub(r"\s+", " ", text).strip()
        if not text or text == "[음악]":
            continue
        start = event.get("tStartMs", 0) / 1000.0
        dur = event.get("dDurationMs", 0) / 1000.0
        if segments and segments[-1]["text"] == text:
            # 자동 자막의 롤링 중복 제거
            segments[-1]["end"] = max(segments[-1]["end"], start + dur)
            continue
        segments.append({"start": start, "end": start + dur, "text": text})

    # 앞 세그먼트의 end 가 다음 start 를 넘지 않게 정리
    for i in range(len(segments) - 1):
        segments[i]["end"] = min(segments[i]["end"], segments[i + 1]["start"])
    return segments


def chunk_segments(segments, window):
    """window 초 단위로 세그먼트를 묶어 읽기 좋은 문단으로 만든다."""
    if window <= 0 or not segments:
        return segments
    merged = []
    bucket = [segments[0]]
    for seg in segments[1:]:
        if seg["end"] - bucket[0]["start"] <= window:
            bucket.append(seg)
        else:
            merged.append(_flush(bucket))
            bucket = [seg]
    merged.append(_flush(bucket))
    return merged


def _flush(bucket):
    return {
        "start": bucket[0]["start"],
        "end": bucket[-1]["end"],
        "text": " ".join(s["text"] for s in bucket),
    }


def hhmmss(seconds):
    total = int(seconds)
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    return "{:02d}:{:02d}:{:02d}".format(h, m, s)


def safe_name(title):
    name = re.sub(r'[/\\:*?"<>|]', "_", title).strip()
    return (name[:100] or "transcript").rstrip(". ")


def write_markdown(path, meta, lang, is_auto, segments):
    video_url = meta.get("webpage_url", "")
    lines = [
        "# {}".format(meta.get("title", "제목 없음")),
        "",
        "- **채널**: {}".format(meta.get("uploader", "-")),
        "- **URL**: {}".format(video_url),
        "- **길이**: {}".format(hhmmss(meta.get("duration") or 0)),
        "- **자막**: {}{}".format(lang, " (자동 생성)" if is_auto else " (수동)"),
        "",
        "---",
        "",
    ]
    base = video_url.split("&")[0]
    for seg in segments:
        stamp = hhmmss(seg["start"])
        if base:
            sep = "&" if "?" in base else "?"
            link = "[`{}`]({}{}t={}s)".format(stamp, base, sep, int(seg["start"]))
        else:
            link = "`{}`".format(stamp)
        lines.append("{} {}".format(link, seg["text"]))
        lines.append("")
    with open(path, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines))


def write_json(path, meta, lang, is_auto, segments):
    payload = {
        "video_id": meta.get("id"),
        "title": meta.get("title"),
        "uploader": meta.get("uploader"),
        "url": meta.get("webpage_url"),
        "duration": meta.get("duration"),
        "language": lang,
        "auto_generated": is_auto,
        "segment_count": len(segments),
        "segments": [
            {
                "index": i,
                "start": round(s["start"], 3),
                "end": round(s["end"], 3),
                "timestamp": hhmmss(s["start"]),
                "text": s["text"],
            }
            for i, s in enumerate(segments)
        ],
    }
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description="유튜브 자막을 타임스탬프와 함께 추출")
    ap.add_argument("url", nargs="?", help="유튜브 URL (--from-json 사용 시 생략 가능)")
    # 기본값: 한국어(영어 영상이면 자동 번역본) · 45초 문단화 · md 한 개
    ap.add_argument("--lang", default="ko,en-orig,en")
    ap.add_argument("--format", default="md", choices=["md", "json", "both"])
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--chunk", type=float, default=45)
    ap.add_argument("--no-auto", action="store_true")
    ap.add_argument("--from-json", help="이미 추출한 .json 을 재가공(네트워크 요청 없음)")
    args = ap.parse_args()

    if args.from_json:
        with open(args.from_json, encoding="utf-8") as fp:
            cached = json.load(fp)
        meta = {
            "id": cached.get("video_id"), "title": cached.get("title"),
            "uploader": cached.get("uploader"), "webpage_url": cached.get("url"),
            "duration": cached.get("duration"),
        }
        lang = cached.get("language", "unknown")
        is_auto = cached.get("auto_generated", False)
        raw = [{"start": s["start"], "end": s["end"], "text": s["text"]}
               for s in cached.get("segments", [])]
        print("[정보] 캐시 재가공: {} ({}개 세그먼트)".format(args.from_json, len(raw)))
    else:
        if not args.url:
            ap.error("URL 또는 --from-json 중 하나는 필요합니다")
        wanted = [x.strip() for x in args.lang.split(",") if x.strip()]
        meta = fetch_metadata(args.url)
        lang, is_auto = pick_language(meta, wanted, not args.no_auto)
        print("[정보] 자막 트랙: {}{}".format(lang, " (자동 생성)" if is_auto else " (수동)"))
        with tempfile.TemporaryDirectory() as workdir:
            raw = parse_json3(download_subtitle(args.url, lang, is_auto, workdir))

    segments = chunk_segments(raw, args.chunk)
    if not segments:
        sys.exit("[에러] 자막 내용이 비어 있습니다.")

    os.makedirs(args.outdir, exist_ok=True)
    # 언어·청크가 다른 결과끼리 덮어쓰지 않도록 파일명에 반영
    suffix = "_{}".format(lang) + ("_chunk{}s".format(int(args.chunk)) if args.chunk > 0 else "")
    stem = os.path.join(args.outdir, safe_name(meta.get("title", "transcript")) + suffix)

    if args.format in ("md", "both"):
        write_markdown(stem + ".md", meta, lang, is_auto, segments)
        print("[완료] {}.md".format(stem))
    if args.format in ("json", "both"):
        write_json(stem + ".json", meta, lang, is_auto, segments)
        print("[완료] {}.json".format(stem))
    print("[완료] 세그먼트 {}개".format(len(segments)))


if __name__ == "__main__":
    main()
