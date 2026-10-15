#!/usr/bin/env python3
"""유튜브 영상의 지정 시각 장면을 이미지로 잘라 낸다.

    frames.py <URL | 요약.json | 자막.md> <시각> [<시각> ...] [--review [--model M]] [--attach]
              [--outdir DIR] [--name NAME] [--start N] [--height 720] [--summary-dir summaries]
    frames.py <요약.json> --scenes [--review [--model M]] [--attach]
              [--outdir DIR] [--name NAME] [--start N] [--height 720]

시각은 SS · MM:SS · HH:MM:SS 이고, `08:20=설명` 처럼 뒤에 캡션을 붙일 수 있다.
영상 전체를 받지 않는다. yt-dlp 가 알려 준 스트림 주소에서 ffmpeg 로 그 시각의
한 장면만 잘라 내므로 장면당 수 초다.

산출물:
    <outdir>/<name> NN.jpg   NN 은 같은 이름의 기존 이미지 다음 번호부터

기본 위치는 요약과 같은 채널 폴더의 assets/ 다 (summaries/<채널>/assets/<제목> NN.jpg).
요약 .json 을 넘기면 그 파일 옆 assets/ 에 그 파일 이름으로 쓴다.

--attach (요약 .json 일 때만): 뽑은 장면을 요약의 frames[] 에 기록한다.
render.py --to md 가 각 장면을 캡션과 닮은 근처 항목(없으면 장면 직전 항목) 바로 아래에 넣는다.

어느 시각을 뽑을지는 사람이 고르는 것이 기본이다. 같은 구간이라도 몇 초 차이로 도식이 덜
그려졌거나 빈 화면이 잡히므로, 뽑은 뒤 열어 보고 아니면 몇 초 옮겨 다시 뽑는다.

--scenes: 시각 대신 요약의 장면 후보(scenes[], summarize.py --scenes 가 자막을 보고 고른 것)를 쓴다.
--review: 후보마다 REVIEW_OFFSETS(0·10·20초 뒤)에서 이미지를 한 장씩 찍고, --model(기본:
          YT_LLM_MODEL, 없으면 claude-opus-5)로 고른 모델이 이미지를 보고 가장 나은 한 장을
          고르거나 모두 버린다. 사람이 열어 보고 거르던 일을 대신한다.
          자막의 시각은 문단(45초) 단위라 한 장만 찍으면 빗나가기 쉬워서 여러 장을 찍는다.
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata

HERE = os.path.dirname(os.path.realpath(__file__))


def _load_safe_name():
    # 파일 이름 규칙을 yt-transcript.py 와 하나로 맞춘다 (이름에 '-' 가 있어 import 로는 못 부른다)
    spec = importlib.util.spec_from_file_location("yt_transcript", os.path.join(HERE, "yt-transcript.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.safe_name


safe_name = _load_safe_name()

REVIEW_OFFSETS = (0, 10, 20)  # --review 때 후보 시각에서 몇 초 뒤를 더 찍을지

REVIEW_PROMPT = """유튜브 영상에서 잘라 낸 장면 후보입니다. 영상 요약 문서에 붙일 이미지를 고릅니다.
후보마다 같은 구간에서 몇 초 간격으로 찍은 이미지가 여러 장 있습니다.
후보마다 가장 나은 이미지 한 장을 고르고, 붙일 만한 이미지가 없으면 0 을 고르세요.

붙일 만한 이미지: 슬라이드·차트·표·도식·코드·시연 결과처럼 화면 자체가 내용을 전하고, 글자와 선이 다 그려져 알아볼 수 있는 것.
버릴 이미지: 말하는 사람의 얼굴·상반신만 보이는 것, 검거나 빈 화면, 화면 전환 중이라 흐린 것, 도식이 덜 그려진 것.
여러 장이 다 괜찮으면 정보가 가장 많이 보이는 것을 고릅니다.

JSON 만 출력합니다:
{"picks": [{"candidate": 1, "pick": 2, "reason": "고른 이유 또는 버린 이유 한 줄"}]}
pick 은 그 후보 안의 이미지 번호(1부터)이고, 없으면 0 입니다. 모든 후보에 대해 하나씩 답합니다."""


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
        die("스트림 정보를 받지 못했습니다. URL 을 확인하고, yt-dlp 를 최신으로 올린 뒤"
            "(brew upgrade yt-dlp) 다시 실행하세요:\n" + proc.stderr.strip())
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


def scene_specs(summary_path):
    """요약의 scenes[] 를 (초, 캡션) 목록으로."""
    with open(summary_path, encoding="utf-8") as fp:
        scenes = json.load(fp).get("scenes")
    if scenes is None:
        die("요약에 scenes 가 없습니다. summarize.py --scenes 로 다시 요약하세요: {}".format(summary_path))
    return [(int(s["t"]), s.get("caption") or "") for s in scenes]


def burst(sec, duration, offsets):
    """후보 시각 sec 에서 offsets 만큼 뒤의 시각들. 영상 끝을 넘는 것은 뺀다."""
    out = [sec + o for o in offsets if not duration or sec + o < duration]
    return out or [sec]


def review(groups, backend, send=None, retries=2):
    """후보별 사진 묶음을 모델에게 보여 주고 고른 사진 번호 목록을 돌려준다.

    groups: [(캡션, [(초, 경로), ...]), ...]  →  [(번호 또는 0, 이유), ...]  (번호는 1부터)
    retries 번 시도해도 응답을 못 받으면 None 을 돌려준다. main() 은 이때 장면을 모두 버린다.
    잘못 고른 장면은 안 붙이느니만 못하기 때문이다.
    """
    import summarize as sm
    send = send or sm.send_images
    parts = [("text", REVIEW_PROMPT)]
    for i, (caption, shots) in enumerate(groups, 1):
        parts.append(("text", "\n## 후보 {}{}".format(i, " — 자막상 예상: " + caption if caption else "")))
        for j, (sec, path) in enumerate(shots, 1):
            parts += [("text", "이미지 {}-{} ({})".format(i, j, label(sec))), ("image", path)]
    attempt = fails = 0
    while attempt < retries:
        raw, error = send(parts, backend)
        fails = fails + 1 if error else 0
        if error and sm.switch_model(backend, error, fails, last=attempt == retries - 1):
            attempt = fails = 0
            continue
        data = sm.extract_json(raw) if raw else None
        picks = data.get("picks") if isinstance(data, dict) else None
        if isinstance(picks, list):
            out = [(0, "판단 없음")] * len(groups)
            for p in picks:
                if not isinstance(p, dict):
                    continue
                c, k = sm._as_seconds(p.get("candidate")), sm._as_seconds(p.get("pick"))
                if c is not None and 1 <= c <= len(groups) and k is not None:
                    k = k if 0 <= k <= len(groups[c - 1][1]) else 0
                    out[c - 1] = (k, str(p.get("reason") or "").strip())
            return out
        print("[재시도] 장면 판별 응답 없음 — {} ({}/{}회차)".format(
            error or "올바른 JSON 이 아닙니다", attempt + 1, retries), file=sys.stderr)
        if attempt < retries - 1:
            time.sleep(15)
        attempt += 1
    return None


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
    ap.add_argument("times", nargs="*", help="SS · MM:SS · HH:MM:SS (뒤에 =캡션 가능)")
    ap.add_argument("--scenes", action="store_true",
                    help="시각 대신 요약 .json 의 장면 후보(scenes[])를 씀 (summarize.py --scenes 로 만든 요약)")
    ap.add_argument("--review", action="store_true",
                    help="후보마다 이미지 3장을 찍어 모델이 보고 한 장을 고르거나 버림")
    ap.add_argument("--model", help="--review 에 쓸 모델 (기본: YT_LLM_MODEL, 없으면 claude-opus-5)")
    ap.add_argument("--attach", action="store_true", help="요약 .json 의 frames[] 에 기록")
    ap.add_argument("--outdir", help="저장 폴더 (기본: summaries/<채널>/assets)")
    ap.add_argument("--name", help="파일 이름 앞부분 (기본: 영상 제목)")
    ap.add_argument("--start", type=int, help="첫 번호 (기본: 같은 이름의 기존 이미지 다음)")
    ap.add_argument("--height", type=int, default=720, help="최대 세로 해상도 (기본 720)")
    ap.add_argument("--summary-dir", default="summaries", help="기본 저장 위치의 상위 폴더")
    ap.add_argument("--no-hint", action="store_true", help=argparse.SUPPRESS)  # yt-transcript.py 가 렌더까지 할 때
    args = ap.parse_args()
    if bool(args.times) == args.scenes:
        ap.error("시각을 적거나 --scenes 중 하나만 쓰세요")

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
    if args.scenes and not summary:
        die("--scenes 는 요약 .json 을 넘겼을 때만 쓸 수 있습니다")

    specs = scene_specs(summary) if args.scenes else [parse_spec(t) for t in args.times]
    if not specs:
        print("[정보] 장면 후보가 없습니다. 자막에 화면을 가리키는 말이 없어 모델이 고르지 않았습니다.")
        print("[안내] 필요하면 시각을 직접 적어 뽑으세요: python3 frames.py \"{}\" 08:20 --attach".format(
            args.source))
        return
    backend = None
    if args.review:
        import summarize as sm
        backend = sm.resolve_backend(args.model)
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
    offsets = REVIEW_OFFSETS if args.review else (0,)
    with tempfile.TemporaryDirectory() as tmp:
        # 먼저 임시 폴더에 찍고, 남길 것만 번호를 매겨 옮긴다 (버린 장면 때문에 번호가 비지 않게)
        groups = []
        for n, (sec, caption) in enumerate(specs):
            shots = []
            for k, t in enumerate(burst(sec, info.get("duration"), offsets)):
                out = os.path.join(tmp, "{:03d}-{}.jpg".format(n, k))
                ok, err = grab(info, t, out)
                if not ok:  # 서명된 주소가 만료됐거나 거부됐으면 한 번만 새로 받아 다시 시도한다
                    info = stream_info(url, args.height)
                    ok, err = grab(info, t, out)
                if ok:
                    shots.append((t, out))
                else:
                    print("[실패] {} — {}".format(label(t), err.splitlines()[-1] if err else "알 수 없음"))
                    failed += 1
            groups.append((caption, shots))

        live = [g for g in groups if g[1]]
        if args.review and live:
            print("[정보] 장면 판별 중... (후보 {}개 · 이미지 {}장 · {})".format(
                len(live), sum(len(g[1]) for g in live), sm.describe_backend(backend)))
            verdicts = review(live, backend)
            if verdicts is None:
                print("[경고] 장면 판별 응답을 받지 못해 장면을 붙이지 않았습니다. "
                      "--review 없이 다시 실행하면 후보 시각마다 한 장씩 붙습니다.")
                verdicts = [(0, "판별 실패")] * len(live)
        else:
            verdicts = [(1, "")] * len(live)

        for (caption, shots), (pick, reason) in zip(live, verdicts):
            why = " — " + reason if reason else ""
            if not pick:
                print("[제외] {}{}".format(label(shots[0][0]), why))
                continue
            sec, tmp_path = shots[pick - 1]
            out = os.path.join(outdir, "{} {:02d}.jpg".format(name, idx))
            shutil.move(tmp_path, out)
            print("[저장] {} → {}{}".format(label(sec), out, why))
            done.append({"t": sec, "file": out, "caption": caption})
            idx += 1

    if args.attach and done:
        base = os.path.dirname(os.path.abspath(summary))
        attach(summary, [{"t": d["t"], "file": os.path.relpath(os.path.abspath(d["file"]), base),
                          "caption": d["caption"]} for d in done])
        print("[완료] {} 에 장면 {}장 기록".format(summary, len(done)))
        if not args.no_hint:
            print("[안내] 읽기용 md 갱신: python3 render.py \"{}\" --to md -o \"{}\"".format(
                summary, os.path.splitext(summary)[0] + ".md"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
