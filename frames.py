#!/usr/bin/env python3
"""유튜브 영상의 지정 시각 장면을 이미지로 잘라 낸다.

    frames.py <URL | 요약.json | 자막.md> <시각> [<시각> ...]
              [--attach] [--outdir DIR] [--name NAME] [--start N] [--height 720]

시각은 SS · MM:SS · HH:MM:SS 이고, `08:20=설명` 처럼 뒤에 캡션을 붙일 수 있다.
영상 전체를 받지 않는다. yt-dlp 가 알려 준 스트림 주소에서 ffmpeg 로 그 시각의
한 장면만 잘라 내므로 장면당 수 초다.

산출물:
    <outdir>/<name> NN.jpg   NN 은 같은 이름의 기존 이미지 다음 번호부터

기본 위치는 요약과 같은 채널 폴더의 assets/ 다 (summaries/<채널>/assets/<제목> NN.jpg).
요약 .json 을 넘기면 그 파일 옆 assets/ 에 그 파일 이름으로 쓴다.

--attach (요약 .json 일 때만): 뽑은 장면을 요약의 frames[] 에 기록한다.
render.py --to md 가 각 장면을 그 시각이 속한 섹션 아래에 넣는다.

어느 시각을 뽑을지는 사람이 고른다. 같은 구간이라도 몇 초 차이로 도식이 덜 그려졌거나
빈 화면이 잡히므로, 뽑은 뒤 열어 보고 아니면 몇 초 옮겨 다시 뽑는다.
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata

HERE = os.path.dirname(os.path.realpath(__file__))


def _load_safe_name():
    # 파일 이름 규칙을 yt-transcript.py 와 하나로 맞춘다 (이름에 '-' 가 있어 import 로는 못 부른다)
    spec = importlib.util.spec_from_file_location("yt_transcript", os.path.join(HERE, "yt-transcript.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.safe_name


safe_name = _load_safe_name()


def die(msg):
    sys.exit("[에러] " + msg)


def parse_spec(text):
    """'08:20=설명' → (500, '설명'). 캡션이 없으면 ''."""
    stamp, _, caption = text.partition("=")
    parts = stamp.strip().split(":")
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        die("시각 형식이 아닙니다: {} (SS · MM:SS · HH:MM:SS)".format(text))
    sec = 0
    for p in parts:
        sec = sec * 60 + int(p)
    return sec, caption.strip()


def label(sec):
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return "{}:{:02d}:{:02d}".format(h, m, s) if h else "{:02d}:{:02d}".format(m, s)


def source_url(path):
    """요약 .json · 자막 .json · 자막 .md · frontmatter 에 url 이 있는 .md 에서 영상 주소를 찾는다."""
    with open(path, encoding="utf-8") as fp:
        text = fp.read()
    if path.endswith(".json"):
        data = json.loads(text)
        url = (data.get("video") or {}).get("url") or data.get("url")
    else:
        m = re.search(r"^- \*\*URL\*\*: (\S+)|^url: (\S+)\s*$", text[:3000], re.M)
        url = m and (m.group(1) or m.group(2))
    if not url:
        die("영상 주소를 찾지 못했습니다: {}".format(path))
    return url


def next_index(outdir, name):
    """같은 이름의 기존 이미지 번호 다음 값. macOS 파일 이름은 NFD 로 올 수 있어 맞춰 비교한다."""
    pat = re.compile(re.escape(unicodedata.normalize("NFC", name)) + r" (\d{2,})\.[A-Za-z0-9]+$")
    used = [0]
    if os.path.isdir(outdir):
        for f in os.listdir(outdir):
            m = pat.match(unicodedata.normalize("NFC", f))
            if m:
                used.append(int(m.group(1)))
    return max(used) + 1


def stream_info(url, height):
    """yt-dlp 가 고른 영상 스트림 정보. 주소와 함께 그 주소가 요구하는 HTTP 헤더가 들어 있다."""
    fmt = "bv*[height<={h}][ext=mp4]/bv*[height<={h}]/b[height<={h}]/b".format(h=height)
    proc = subprocess.run(["yt-dlp", "-j", "-f", fmt, "--no-warnings", url],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        die("스트림 정보를 받지 못했습니다:\n" + proc.stderr.strip())
    info = json.loads(proc.stdout)
    if not info.get("url"):
        die("스트림 주소가 없습니다 (형식 {})".format(info.get("format_id")))
    return info


def grab(info, sec, out):
    # 헤더(User-Agent 등) 없이 부르면 403 을 주는 형식이 있다
    headers = info.get("http_headers") or {}
    cmd = ["ffmpeg", "-loglevel", "error"]
    if headers.get("User-Agent"):
        cmd += ["-user_agent", headers["User-Agent"]]
    rest = "".join("{}: {}\r\n".format(k, v) for k, v in headers.items() if k != "User-Agent")
    if rest:
        cmd += ["-headers", rest]
    # -ss 를 -i 앞에 두면 그 시각 근처로 바로 건너뛰어 영상 전체를 받지 않는다
    cmd += ["-ss", str(sec), "-i", info["url"], "-frames:v", "1", "-q:v", "3", "-y", out]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode == 0 and os.path.exists(out), proc.stderr.strip()


def attach(summary_path, entries):
    """요약 JSON 의 frames[] 에 장면을 더한다. 같은 파일은 새 값으로 바꾸고,
    열어 보고 지운 이미지는 기록에서도 뺀다 (남겨 두면 md 에 깨진 링크가 된다)."""
    with open(summary_path, encoding="utf-8") as fp:
        data = json.load(fp)
    base = os.path.dirname(os.path.abspath(summary_path))
    new = {e["file"] for e in entries}
    frames = [f for f in data.get("frames") or []
              if f["file"] not in new and os.path.exists(os.path.join(base, f["file"]))]
    data["frames"] = sorted(frames + entries, key=lambda f: f["t"])
    with open(summary_path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description="유튜브 영상의 지정 시각 장면을 이미지로 저장")
    ap.add_argument("source", help="유튜브 URL, 요약 .json, 또는 자막 .md")
    ap.add_argument("times", nargs="+", help="SS · MM:SS · HH:MM:SS (뒤에 =캡션 가능)")
    ap.add_argument("--attach", action="store_true", help="요약 .json 의 frames[] 에 기록")
    ap.add_argument("--outdir", help="저장 폴더 (기본: summaries/<채널>/assets)")
    ap.add_argument("--name", help="파일 이름 앞부분 (기본: 영상 제목)")
    ap.add_argument("--start", type=int, help="첫 번호 (기본: 같은 이름의 기존 이미지 다음)")
    ap.add_argument("--height", type=int, default=720, help="최대 세로 해상도 (기본 720)")
    ap.add_argument("--summary-dir", default="summaries", help="기본 저장 위치의 상위 폴더")
    args = ap.parse_args()

    for tool in ("yt-dlp", "ffmpeg"):
        if not shutil.which(tool):
            die("{} 가 없습니다: brew install {}  (또는 ./setup.sh)".format(tool, tool))

    is_url = re.match(r"https?://", args.source)
    summary = None
    if not is_url:
        if not os.path.isfile(args.source):
            die("파일이 없습니다: {}".format(args.source))
        if args.source.endswith(".json"):
            with open(args.source, encoding="utf-8") as fp:
                if "video" in json.load(fp):
                    summary = args.source
    if args.attach and not summary:
        die("--attach 는 요약 .json 을 넘겼을 때만 쓸 수 있습니다")

    specs = [parse_spec(t) for t in args.times]
    url = args.source if is_url else source_url(args.source)
    info = stream_info(url, args.height)

    # 요약 .json 이면 그 옆에, 아니면 yt-transcript.py 와 같은 채널·제목 규칙으로 놓는다
    if summary:
        outdir = args.outdir or os.path.join(os.path.dirname(summary), "assets")
        name = args.name or os.path.splitext(os.path.basename(summary))[0]
    else:
        channel = safe_name(info.get("uploader") or "미상")
        outdir = args.outdir or os.path.join(args.summary_dir, channel, "assets")
        name = args.name or safe_name(info.get("title") or info.get("id") or "frame")
    name = unicodedata.normalize("NFC", name)
    os.makedirs(outdir, exist_ok=True)
    idx = args.start or next_index(outdir, name)

    done, failed = [], 0
    for sec, caption in specs:
        out = os.path.join(outdir, "{} {:02d}.jpg".format(name, idx))
        ok, err = grab(info, sec, out)
        if not ok:  # 서명된 주소가 만료됐거나 거부됐으면 한 번만 새로 받아 다시 시도한다
            info = stream_info(url, args.height)
            ok, err = grab(info, sec, out)
        if not ok:
            print("[실패] {} — {}".format(label(sec), err.splitlines()[-1] if err else "알 수 없음"))
            failed += 1
            continue
        print("[frame] {} → {}".format(label(sec), out))
        done.append({"t": sec, "file": out, "caption": caption})
        idx += 1

    if args.attach and done:
        base = os.path.dirname(os.path.abspath(summary))
        attach(summary, [{"t": d["t"], "file": os.path.relpath(os.path.abspath(d["file"]), base),
                          "caption": d["caption"]} for d in done])
        print("[완료] {} 에 장면 {}장 기록".format(summary, len(done)))
        print("[안내] 읽기용 md 갱신: python3 render.py \"{}\" --to md -o \"{}\"".format(
            summary, os.path.splitext(summary)[0] + ".md"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
