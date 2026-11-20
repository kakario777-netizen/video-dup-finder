"""자체 점검 — 여러 형태의 중복 영상과 깨진 영상을 만들어 제대로 찾는지 확인"""
import os
import shutil
import tempfile
import time

import cache as cachemod
import fftools
import scanner
import sources

AUD1 = ("aevalsrc='0.3*sin(2*PI*t*(500+300*sin(floor(t*3)*12.9898)))"
        "+0.2*sin(2*PI*t*(1100+500*sin(floor(t*5)*78.233)))':s=44100:d={d}")
AUD2 = ("aevalsrc='0.3*sin(2*PI*t*(700+250*sin(floor(t*4)*45.164)))"
        "+0.2*sin(2*PI*t*(1300+400*sin(floor(t*7)*93.989)))':s=44100:d={d}")


def ff(args, log):
    rc, _, err = fftools.run([fftools.FFMPEG, "-y", "-v", "error", "-nostdin", *args], timeout=600)
    if rc != 0:
        log("ffmpeg 실패:", err[-300:])
        raise RuntimeError("테스트 영상 만들기 실패")


def make(d, log):
    j = lambda n: os.path.join(d, n)  # noqa
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-g", "48", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k"]
    # 원본 A (60초)
    ff(["-f", "lavfi", "-i", "mandelbrot=s=640x360:r=24:end_pts=600", "-f", "lavfi", "-i", AUD1.format(d=60),
        "-t", "60", *enc, "-crf", "20", j("원본 A.mp4")], log)
    # 다른 영상 C (40초)
    ff(["-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=40", "-f", "lavfi", "-i", AUD2.format(d=40),
        "-vf", "hue=H=t*0.3", "-t", "40", *enc, "-crf", "22", j("다른 영상 C.mp4")], log)
    os.makedirs(j("복사본"), exist_ok=True)
    shutil.copy(j("원본 A.mp4"), j("복사본/원본 A 복사.mp4"))
    with open(j("복사본/원본 A 복사.srt"), "w", encoding="utf-8") as f:
        f.write("1\n00:00:01,000 --> 00:00:02,000\n안녕\n")
    # 저화질 H.265
    ff(["-i", j("원본 A.mp4"), "-vf", "scale=320:180", "-c:v", "libx265", "-preset", "ultrafast", "-crf", "34",
        "-x265-params", "log-level=error", "-c:a", "aac", "-b:a", "64k", j("A 저화질.mkv")], log)
    # 화면에 박힌 자막(아래쪽 글자 모양 흰 상자들, 2초마다 바뀜)
    boxes = []
    for k in range(22):
        x = 110 + k * 19
        boxes.append(f"drawbox=x={x}:y=318:w={8 + (k * 7) % 9}:h=20:c=white:t=fill:"
                     f"enable='lt(mod(t+{k % 5}\\,4)\\,3)'")
    ff(["-i", j("원본 A.mp4"), "-vf", ",".join(boxes), *enc, "-crf", "23", j("A 자막.mp4")], log)
    # 일부 구간 (15~45초)
    ff(["-ss", "15", "-i", j("원본 A.mp4"), "-t", "30", *enc, "-crf", "23", j("A 일부.mp4")], log)
    # 중간 컷 편집본 (0~20 + 35~60)
    ff(["-i", j("원본 A.mp4"), "-filter_complex",
        "[0:v]trim=0:20,setpts=PTS-STARTPTS[v1];[0:v]trim=35:60,setpts=PTS-STARTPTS[v2];"
        "[0:a]atrim=0:20,asetpts=PTS-STARTPTS[a1];[0:a]atrim=35:60,asetpts=PTS-STARTPTS[a2];"
        "[v1][a1][v2][a2]concat=n=2:v=1:a=1[v][a]", "-map", "[v]", "-map", "[a]", *enc, "-crf", "23",
        j("A 편집본.mp4")], log)
    # 좌우반전 + 레터박스(4:3)
    ff(["-i", j("원본 A.mp4"), "-vf", "hflip,scale=640:360,pad=640:480:0:60", *enc, "-crf", "23",
        j("A 반전 4대3.mp4")], log)
    # 앞뒤에 다른 내용이 붙은 긴 영상 (A가 안에 포함)
    ff(["-f", "lavfi", "-i", "gradients=s=640x360:r=24:d=20:speed=0.03:seed=7", "-f", "lavfi", "-i", AUD2.format(d=20),
        "-t", "20", *enc, "-crf", "23", j("_pre.mp4")], log)
    ff(["-i", j("_pre.mp4"), "-i", j("원본 A.mp4"), "-i", j("_pre.mp4"), "-filter_complex",
        "[0:v][0:a][1:v][1:a][2:v][2:a]concat=n=3:v=1:a=1[v][a]", "-map", "[v]", "-map", "[a]", *enc, "-crf", "23",
        j("긴 영상 B.mp4")], log)
    os.remove(j("_pre.mp4"))
    # 깨진 영상들
    with open(j("원본 A.mp4"), "rb") as f:
        data = f.read()
    with open(j("깨짐_반쪽.mp4"), "wb") as f:
        f.write(data[: len(data) // 2])
    open(j("깨짐_빈파일.mp4"), "wb").close()
    with open(j("깨짐_가짜.avi"), "wb") as f:
        f.write(b"this is not a video" * 1000)
    # mkv 를 중간에서 자른 것(앞부분은 열림)
    with open(j("A 저화질.mkv"), "rb") as f:
        mk = f.read()
    with open(j("깨짐_끊김.mkv"), "wb") as f:
        f.write(mk[: int(len(mk) * 0.4)])


def run(log=print):
    d = tempfile.mkdtemp(prefix="vdf_test_")
    t0 = time.time()
    make(d, log)
    log(f"샘플 생성 {time.time() - t0:.1f}s")
    cdb = cachemod.Cache(os.path.join(d, "_cache.db"))
    msgs = []
    sc = scanner.Scan([sources.LocalSource(d)], {"broken": True, "audio": True}, lambda *a: msgs.append(a), cdb)
    t1 = time.time()
    res = sc.run()
    log(f"검사 {time.time() - t1:.1f}s · 파일 {res['count']}")
    items = res["items"]
    name = lambda i: items[i]["name"]  # noqa
    ok = True
    main = None
    for s in res["sets"]:
        names = sorted(name(i) for i in s["items"])
        log(f"세트 {s['no']} [{s['kind']}] 남김={name(s['keep'])} : {names}")
        for r in s["rels"]:
            log(f"   {name(r['a'])} ↔ {name(r['b'])} {r['kind']} A{r['covA']:.2f} B{r['covB']:.2f} {r['basis']} {r['notes']} {r['segs'][:2]}")
        if "원본 A.mp4" in names and s["kind"] not in ("partial", "audio_only"):
            main = s
    for b in res["broken"]:
        log(f"깨짐: {name(b['id'])} [{b['kind']}] {b['msg']}")
    want = {"원본 A.mp4", "원본 A 복사.mp4", "A 저화질.mkv", "A 자막.mp4", "A 일부.mp4", "A 편집본.mp4",
            "A 반전 4대3.mp4", "긴 영상 B.mp4"}
    got = {name(i) for i in main["items"]} if main else set()
    if got != want:
        log("FAIL 묶음 누락/오탐:", sorted(want - got), sorted(got - want))
        ok = False
    if main and name(main["keep"]) != "긴 영상 B.mp4":
        log("FAIL 남길 영상 추천이 '긴 영상 B'가 아님")
        ok = False
    if any("다른 영상 C.mp4" in [name(i) for i in s["items"]] for s in res["sets"]):
        log("FAIL 다른 영상 C 가 중복으로 잡힘")
        ok = False
    if main:
        notes = " ".join(" ".join(r["notes"]) for r in main["rels"])
        for w in ("좌우 반전", "중간 편집", "자막"):
            if w not in notes:
                log("FAIL 표시 누락:", w)
                ok = False
    bnames = {name(b["id"]) for b in res["broken"]}
    for w in ("깨짐_반쪽.mp4", "깨짐_빈파일.mp4", "깨짐_가짜.avi", "깨짐_끊김.mkv"):
        if w not in bnames:
            log("FAIL 깨진 영상 못 찾음:", w)
            ok = False
    bad = bnames - {"깨짐_반쪽.mp4", "깨짐_빈파일.mp4", "깨짐_가짜.avi", "깨짐_끊김.mkv"}
    if bad:
        log("FAIL 멀쩡한 영상을 깨짐으로:", bad)
        ok = False
    # 두 번째 검사는 캐시로 빨라야 함
    t2 = time.time()
    scanner.Scan([sources.LocalSource(d)], {"broken": True, "audio": True}, None, cdb).run()
    log(f"재검사(캐시) {time.time() - t2:.1f}s")
    cdb.close()
    log("selftest ok" if ok else "selftest FAIL")
    return ok, d, res


if __name__ == "__main__":
    run()
