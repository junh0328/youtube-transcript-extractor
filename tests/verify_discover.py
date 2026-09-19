#!/usr/bin/env python3
"""discover.py 의 판정 규칙을 네트워크 없이 확인한다.

yt-dlp 를 부르는 부분(list_recent·fetch_exact)은 건드리지 않고,
그 결과를 받아 판정하는 순수 함수만 가짜 입력으로 돌린다.
"""
import datetime as dt, importlib.util, os, re, sys, tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("dc", os.path.join(REPO, "discover.py"))
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)

failures = []


def check(name, got, want):
    ok = got == want
    print("{} {}".format("통과" if ok else "실패", name))
    if not ok:
        print("     기대: {!r}\n     실제: {!r}".format(want, got))
        failures.append(name)


# --- 채널 주소 → 동영상 탭
check("핸들 주소에 /videos 를 붙인다",
      dc.videos_tab("https://www.youtube.com/@UpbitOfficial/"), "https://www.youtube.com/@UpbitOfficial/videos")
check("이미 탭이 있으면 그대로 둔다",
      dc.videos_tab("https://www.youtube.com/@x/streams"), "https://www.youtube.com/@x/streams")

# --- --known: 줄 어디에 있든 영상 ID 를 뽑는다
with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fp:
    fp.write("url: https://www.youtube.com/watch?v=AAAAAAAAAAA\n"
             "https://youtu.be/BBBBBBBBBBB?t=3\n"
             "CCCCCCCCCCC\n"
             "https://www.youtube.com/watch?v=DDDDDDDDDDD&t=88s\n"
             "문장 안의 주소 https://www.youtube.com/shorts/EEEEEEEEEEE 도\n")
    known_path = fp.name
check("frontmatter·짧은 주소·ID·쿼리·shorts 에서 ID 추출",
      sorted(dc.read_known([known_path])),
      ["AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC", "DDDDDDDDDDD", "EEEEEEEEEEE"])
os.unlink(known_path)

# --- 채널 목록 파일: 주석·빈 줄·이름표
with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fp:
    fp.write("# 주석\n\nhttps://www.youtube.com/@a   이름표\nhttps://www.youtube.com/@b # 꼬리 주석\n")
    ch_path = fp.name
check("채널 목록 파일 읽기", dc.read_channels([ch_path, "https://www.youtube.com/@c"]),
      ["https://www.youtube.com/@a", "https://www.youtube.com/@b", "https://www.youtube.com/@c"])
os.unlink(ch_path)

# --- 최종 판정: 정확한 업로드일·원제·방송 상태
since = dt.date(2026, 9, 13)
metas = [
    {"id": "new00000001", "title": "오늘 영상", "channel": "A", "upload_date": "20260919", "duration": 100},
    {"id": "edge0000001", "title": "경계일 영상", "channel": "A", "upload_date": "20260913", "duration": 100},
    {"id": "old00000001", "title": "근사 날짜로는 들어왔지만 실제로는 오래됨", "channel": "A", "upload_date": "20260912"},
    {"id": "zip00000001", "title": "9월 3주차 인사이트 토크 모음.zip | 업비트", "channel": "A", "upload_date": "20260919"},
    {"id": "live0000001", "title": "라이브", "channel": "A", "upload_date": "20260919", "live_status": "is_live"},
    {"id": "nodate00001", "title": "날짜 없음", "channel": "A"},
]
picked = dc.select(metas, since, [re.compile(r"모음\.zip")])
check("경계일 포함 · 오래된 것·제외 패턴·방송 중·날짜 없음 제외 · 오래된 순 정렬",
      [p["id"] for p in picked], ["edge0000001", "new00000001"])
check("출력 필드", sorted(picked[0]), ["channel", "duration", "id", "title", "upload_date", "url"])
check("URL 정규화", picked[0]["url"], "https://www.youtube.com/watch?v=edge0000001")

print()
print("실패 {}건".format(len(failures)) if failures else "모두 통과")
sys.exit(1 if failures else 0)
