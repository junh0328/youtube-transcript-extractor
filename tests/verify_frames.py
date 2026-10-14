#!/usr/bin/env python3
"""frames.py 의 시각 해석·번호·기록 규칙과 render.py 의 장면 배치를 네트워크 없이 확인한다.

yt-dlp·ffmpeg 를 부르는 부분(stream_info·grab)은 건드리지 않는다.
"""
import importlib.util, json, os, sys, tempfile, unicodedata

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
spec = importlib.util.spec_from_file_location("fr", os.path.join(REPO, "frames.py"))
fr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fr)
import render

failures = []


def check(name, got, want):
    ok = got == want
    print("{} {}".format("통과" if ok else "실패", name))
    if not ok:
        print("     기대: {!r}\n     실제: {!r}".format(want, got))
        failures.append(name)


# --- 시각 해석
check("SS · MM:SS · HH:MM:SS", [fr.parse_spec(t)[0] for t in ("45", "08:20", "1:02:15")], [45, 500, 3735])
check("= 뒤는 캡션", fr.parse_spec("02:40=화자가 그린 개념도"), (160, "화자가 그린 개념도"))
check("표시 형식", [fr.label(500), fr.label(3735)], ["08:20", "1:02:15"])

# --- 영상 주소 찾기: 요약 json · 자막 md · frontmatter md
with tempfile.TemporaryDirectory() as d:
    paths = {
        "요약.json": json.dumps({"video": {"url": "https://www.youtube.com/watch?v=AAAAAAAAAAA"}}),
        "자막.md": "# 제목\n\n- **채널**: x\n- **URL**: https://www.youtube.com/watch?v=BBBBBBBBBBB\n",
        "vault.md": "---\ntitle: x\nurl: https://www.youtube.com/watch?v=CCCCCCCCCCC\n---\n",
    }
    for name, text in paths.items():
        with open(os.path.join(d, name), "w", encoding="utf-8") as fp:
            fp.write(text)
    check("요약 json · 자막 md · frontmatter 에서 주소",
          [fr.source_url(os.path.join(d, n))[-11:] for n in paths],
          ["AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"])

# --- 번호: 같은 이름의 기존 이미지 다음. 파일 이름이 NFD 로 저장돼 있어도 센다
with tempfile.TemporaryDirectory() as d:
    for f in ("제목 01.jpg", unicodedata.normalize("NFD", "제목 03.jpg"), "제목 끝 07.jpg", "다른 09.jpg"):
        open(os.path.join(d, f), "w").close()
    check("기존 번호 다음 (NFD 포함, 이름이 다른 파일은 제외)", fr.next_index(d, "제목"), 4)
    check("폴더가 없으면 1", fr.next_index(os.path.join(d, "없음"), "제목"), 1)

# --- 기록: 같은 파일은 덮어쓰고, 지운 이미지는 뺀다
with tempfile.TemporaryDirectory() as d:
    os.mkdir(os.path.join(d, "assets"))
    for f in ("a 01.jpg", "a 03.jpg"):
        open(os.path.join(d, "assets", f), "w").close()
    summary = os.path.join(d, "a.json")
    with open(summary, "w", encoding="utf-8") as fp:
        json.dump({"video": {}, "frames": [
            {"t": 300, "file": "assets/a 01.jpg", "caption": "옛 캡션"},
            {"t": 100, "file": "assets/a 02.jpg", "caption": "지운 장면"},
        ]}, fp)
    fr.attach(summary, [{"t": 300, "file": "assets/a 01.jpg", "caption": "새 캡션"},
                        {"t": 50, "file": "assets/a 03.jpg", "caption": ""}])
    with open(summary, encoding="utf-8") as fp:
        got = json.load(fp)["frames"]
    check("덮어쓰기 · 지운 이미지 제거 · 시각순 정렬",
          [(f["t"], f["caption"]) for f in got], [(50, ""), (300, "새 캡션")])

# --- 배치: 그 시각이 속한 섹션. 첫 섹션보다 앞이면 첫 섹션
sections = [{"t": 0}, {"t": 200}, {"t": 600}]
frames = [{"t": 650}, {"t": 199}, {"t": 200}, {"t": 10}]
check("섹션 시작 시각 기준 배치",
      [[f["t"] for f in g] for g in render.place_frames(sections, frames)], [[10, 199], [200], [650]])
check("첫 섹션보다 앞선 장면", [[f["t"] for f in g] for g in render.place_frames([{"t": 60}], [{"t": 5}])], [[5]])

# --- 렌더: 공백·한글 경로는 인코딩, 캡션이 없으면 시각으로 대체
doc = {"video": {"title": "t", "channel": "c", "url": "https://www.youtube.com/watch?v=X", "duration_sec": 900},
       "headline": "h", "key_points": [], "source": {"transcript": "x.md"},
       "sections": [{"title": "s", "t": 0, "points": [{"text": "p", "t": 0}]}],
       "frames": [{"t": 160, "file": "assets/제목 01.jpg", "caption": "개념도"},
                  {"t": 170, "file": "assets/제목 02.jpg", "caption": ""}]}
md = render.render_md(doc)
check("이미지 링크 인코딩", "![개념도](assets/%EC%A0%9C%EB%AA%A9%2001.jpg)" in md, True)
check("캡션 줄에 점프 링크", "*[`02:40`](https://www.youtube.com/watch?v=X&t=160s) 개념도*" in md, True)
check("캡션 없는 장면", "![02:50 장면](" in md and "*[`02:50`](https://www.youtube.com/watch?v=X&t=170s)*" in md, True)
del doc["frames"]
check("frames 가 없으면 이미지 없음", "![" in render.render_md(doc), False)

print()
print("실패 {}건".format(len(failures)) if failures else "모두 통과")
sys.exit(1 if failures else 0)
