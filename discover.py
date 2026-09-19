#!/usr/bin/env python3
"""채널 목록에서 최근 N일 안에 올라온 새 영상을 찾는다.

자막·요약은 하지 않는다. 결과(JSON 한 줄 = 영상 하나)를 받아 yt-transcript.py 나
다른 도구에 넘기는 것은 호출하는 쪽의 몫이다. 상태를 저장하지 않으므로 "이미 처리한 영상"은
--known 으로 넘긴다.

사용법:
    discover.py <채널 URL | 채널 목록 파일> ... [--days 7] [--known FILE]
                [--exclude 정규식] [--scan 30]

    채널 목록 파일: 한 줄에 채널 URL 하나. `#` 뒤는 주석, 빈 줄 무시.
                    URL 뒤에 공백을 두고 적은 글자는 이름표로 보고 무시한다.
    --known   이미 처리한 영상. 영상 ID 나 유튜브 URL 이 들어 있는 텍스트 파일(여러 번 지정 가능).
              줄 안 어디에 있든 ID 를 뽑아 쓰므로 frontmatter 를 grep 한 결과를 그대로 넘겨도 된다.
    --exclude 원제(채널이 붙인 원래 제목)에 걸리면 뺀다. 여러 번 지정 가능.

출력: stdout 에 JSON Lines — {"id","url","title","channel","upload_date","duration"}, 업로드일 오름차순.
      진행 상황·경고는 stderr.
종료 코드: 0 = 모든 채널 조회 성공, 1 = 인자 오류 또는 모든 채널 실패, 2 = 일부 채널만 실패(결과는 출력됨)

동작:
    1) 채널의 동영상 탭을 목록으로만 훑는다(--flat-playlist). 영상마다 요청하지 않아 채널당 1초 안팎.
       이 단계의 날짜는 "3일 전" 같은 표시를 거꾸로 계산한 근사값이라 하루 여유를 두고 거른다.
       제목도 시청자 언어로 번역된 것일 수 있어 여기서는 쓰지 않는다.
    2) --known 에 있는 영상을 뺀다.
    3) 남은 것만 한 번에 메타데이터를 다시 받아 정확한 업로드일·원제로 최종 판정한다.
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys

VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|shorts/|^)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")
TABS = ("/videos", "/streams", "/shorts")


def log(msg):
    print(msg, file=sys.stderr)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def read_channels(args):
    """인자 하나하나가 URL 이면 그대로, 파일이면 그 안의 URL 들을 쓴다."""
    out = []
    for arg in args:
        if os.path.isfile(arg):
            with open(arg, encoding="utf-8") as fp:
                for line in fp:
                    line = line.split("#", 1)[0].strip()
                    if line:
                        out.append(line.split()[0])
        else:
            out.append(arg)
    return out


def videos_tab(url):
    """채널 주소를 동영상 탭 주소로. 이미 탭이 붙어 있으면 그대로 둔다."""
    url = url.rstrip("/")
    return url if url.endswith(TABS) else url + "/videos"


def read_known(paths):
    known = set()
    for path in paths or []:
        with open(path, encoding="utf-8") as fp:
            for line in fp:
                known.update(VIDEO_ID.findall(line.strip()))
    return known


def list_recent(channel_url, since, scan):
    """목록만 훑어 (id, 근사 업로드일) 을 돌려준다. 실패하면 None."""
    proc = run(["yt-dlp", "--flat-playlist", "--no-warnings",
                "--playlist-end", str(scan),
                "--extractor-args", "youtubetab:approximate_date",
                "--print", "%(id)s\t%(upload_date)s", videos_tab(channel_url)])
    if proc.returncode != 0:
        last = (proc.stderr.strip().splitlines() or [""])[-1]
        log("[경고] 채널 목록을 받지 못했습니다: {}\n  {}".format(channel_url, last))
        return None
    # 근사 날짜는 하루쯤 어긋나므로 하루 여유를 둔다. 최종 판정은 fetch_exact 뒤에 한다
    margin = (since - dt.timedelta(days=1)).strftime("%Y%m%d")
    found = []
    for line in proc.stdout.splitlines():
        vid, _, date = line.partition("\t")
        if not vid:
            continue
        if date.isdigit() and date < margin:
            break  # 동영상 탭은 최신순이다 — 여기부터는 모두 범위 밖
        found.append(vid)
    return found


def fetch_exact(ids):
    """영상들의 정확한 메타데이터를 한 번의 yt-dlp 호출로 받는다."""
    if not ids:
        return []
    fields = "%(.{id,title,channel,uploader,upload_date,duration,live_status})j"
    proc = run(["yt-dlp", "--skip-download", "--no-warnings", "--ignore-errors",
                "--print", fields]
               + ["https://www.youtube.com/watch?v=" + v for v in ids])
    metas = []
    for line in proc.stdout.splitlines():
        try:
            metas.append(json.loads(line))
        except ValueError:
            continue
    missing = set(ids) - {m.get("id") for m in metas}
    errors = proc.stderr.splitlines()
    for vid in sorted(missing):
        reason = next((e for e in errors if vid in e), "")
        if "members" in reason:
            log("[제외] 멤버십 전용: {}".format(vid))   # 받을 수 없는 게 정상이다
        else:
            log("[경고] 영상 정보를 받지 못해 건너뜁니다: {} — {}".format(vid, reason.split(": ", 2)[-1]))
    return metas


def select(metas, since, excludes):
    """정확한 업로드일·원제로 최종 판정한다."""
    cutoff = since.strftime("%Y%m%d")
    picked = []
    for m in metas:
        title = m.get("title") or ""
        if m.get("live_status") in ("is_live", "is_upcoming"):
            log("[제외] 방송 중·예정: {}".format(title))
            continue
        if (m.get("upload_date") or "") < cutoff:
            continue
        hit = next((p for p in excludes if p.search(title)), None)
        if hit:
            log("[제외] /{}/ : {}".format(hit.pattern, title))
            continue
        picked.append({
            "id": m["id"],
            "url": "https://www.youtube.com/watch?v=" + m["id"],
            "title": title,
            "channel": m.get("channel") or m.get("uploader") or "",
            "upload_date": m.get("upload_date"),
            "duration": m.get("duration"),
        })
    return sorted(picked, key=lambda x: (x["upload_date"], x["id"]))


def main():
    ap = argparse.ArgumentParser(description="채널 목록에서 최근 N일 안에 올라온 새 영상을 찾는다")
    ap.add_argument("channels", nargs="+", help="채널 URL 또는 채널 목록 파일")
    ap.add_argument("--days", type=int, default=7, help="오늘 포함 며칠 전까지 볼지. 기본 7")
    ap.add_argument("--known", action="append", help="이미 처리한 영상 ID·URL 이 담긴 파일")
    ap.add_argument("--exclude", action="append", default=[], help="원제에 걸리면 뺄 정규식")
    ap.add_argument("--scan", type=int, default=30, help="채널마다 최신 몇 개까지 훑을지. 기본 30")
    args = ap.parse_args()

    channels = read_channels(args.channels)
    if not channels:
        ap.error("채널이 없습니다")
    since = dt.date.today() - dt.timedelta(days=args.days - 1)
    excludes = [re.compile(p) for p in args.exclude]
    known = read_known(args.known)

    candidates, failed = [], 0
    for url in channels:
        ids = list_recent(url, since, args.scan)
        if ids is None:
            failed += 1
            continue
        fresh = [v for v in ids if v not in known and v not in candidates]
        log("[정보] {} — 최근 {}개 중 새 영상 후보 {}개".format(url, len(ids), len(fresh)))
        candidates += fresh

    for item in select(fetch_exact(candidates), since, excludes):
        print(json.dumps(item, ensure_ascii=False))

    if failed == len(channels):
        sys.exit(1)
    if failed:
        sys.exit(2)


if __name__ == "__main__":
    main()
