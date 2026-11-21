"""영상 한 개를 읽어 '내용 지문'을 만드는 곳
- 화면 지문: 일정 간격 화면을 64x64 흑백으로 받아 검은 테두리 제거 → 아래 자막 구역 제외 → 64비트 dHash
            (+ 가운데 부분 지문, 좌우반전 지문, 아래쪽 글자량)
- 소리 지문: 5512Hz 모노로 받아 33개 주파수 대역 에너지 변화 → 32비트 (약 0.046초 간격)
- 깨짐 확인: 열리는지, 영상 트랙, 실제 끝까지 읽히는지, 읽는 중 오류가 나는 지점"""
import hashlib
import json
import os
import re
import tempfile
import threading
import time

import numpy as np

import fftools

FP_VERSION = 4
SIDE = 64
AUDIO_SR = 5512
AUDIO_WIN = 2048
AUDIO_HOP = 256
AUDIO_STEP = AUDIO_HOP / AUDIO_SR

ERR_PAT = re.compile(r"(error while decoding|invalid nal|corrupt|concealing|missing picture|non-existing pps|"
                     r"no frame!|invalid data found|partial file|truncat|moov atom not found|"
                     r"decode_slice_header error|illegal|out of range|error splitting|packet mismatch|"
                     r"invalid block|could not find ref|ac-tex damaged|mb type|overread)", re.I)
DRM_PAT = re.compile(r"(drm|encrypt|protected|playready|widevine|fairplay|cenc|decryption key)", re.I)


class Broken(Exception):
    def __init__(self, kind, msg):
        super().__init__(msg)
        self.kind = kind      # broken / unsupported
        self.msg = msg


def _ratio(s):
    try:
        a, b = str(s).split("/")
        return float(a) / float(b) if float(b) else 0.0
    except Exception:
        try:
            return float(s)
        except Exception:
            return 0.0


def probe(url, size):
    if size == 0:
        raise Broken("broken", "빈 파일 (0바이트)")
    rc, out, err = fftools.run([fftools.FFPROBE, "-v", "error", "-print_format", "json",
                                "-show_format", "-show_streams", url], timeout=120)
    if rc != 0:
        if DRM_PAT.search(err):
            raise Broken("unsupported", "잠긴 영상(DRM) — 열 수 없는 형식")
        return _probe_fail(err)
    try:
        d = json.loads(out.decode("utf-8", "replace"))
    except Exception:
        raise Broken("broken", "영상 정보를 읽을 수 없음")
    streams = d.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    fmt = d.get("format", {})
    dur = _ratio(fmt.get("duration") or (v or {}).get("duration") or (a or {}).get("duration") or 0)
    if v is None:
        if a is not None:
            raise Broken("broken", "화면 없이 소리만 있음")
        raise Broken("broken", "영상 트랙이 없음")
    tag = (v.get("codec_tag_string") or "").lower()
    if tag in ("encv", "drmi", "drms") or DRM_PAT.search(err or ""):
        raise Broken("unsupported", "잠긴 영상(DRM) — 열 수 없는 형식")
    cname = v.get("codec_name") or ""
    if not cname or cname == "none":
        raise Broken("unsupported", f"지원 안 되는 코덱 ({tag or '알 수 없음'})")
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    if w <= 0 or h <= 0:
        raise Broken("broken", "화면 크기 정보가 없음 (손상)")
    sar = _ratio(v.get("sample_aspect_ratio") or "1/1") or 1.0
    br = int(fmt.get("bit_rate") or 0) or int(v.get("bit_rate") or 0)
    if not br and dur > 0:
        br = int(size * 8 / dur)
    return {
        "duration": dur, "vcodec": cname, "width": w, "height": h,
        "dar": round(w * sar / h, 3) if h else 0,
        "fps": _ratio(v.get("avg_frame_rate")) or _ratio(v.get("r_frame_rate")),
        "bitrate": br, "has_audio": a is not None, "acodec": (a or {}).get("codec_name", ""),
        "sub_tracks": len(subs), "format": fmt.get("format_name", ""),
        "start": _ratio(fmt.get("start_time") or 0),
    }


def _probe_fail(err):
    low = err.lower()
    if "moov atom not found" in low:
        raise Broken("broken", "파일 끝부분이 없음 (다운로드·복사가 덜 됨)")
    if "invalid data found" in low:
        raise Broken("broken", "열리지 않음 (파일 손상 또는 영상이 아닌 파일)")
    if "no such file" in low or "connection" in low or "i/o error" in low or "server returned" in low:
        raise Broken("readfail", "파일을 읽지 못함 (접근 오류)")
    raise Broken("broken", "열리지 않음 — " + (err.strip().splitlines() or ["알 수 없는 오류"])[-1][:120])


# ───────────────────────── 크기 조절 도우미 ─────────────────────────
def _area_matrix(n_in, n_out):
    m = np.zeros((n_out, n_in), np.float32)
    edges = np.linspace(0, n_in, n_out + 1)
    for i in range(n_out):
        a, b = edges[i], edges[i + 1]
        lo, hi = int(np.floor(a)), int(np.ceil(b))
        for j in range(lo, min(hi, n_in)):
            m[i, j] = min(b, j + 1) - max(a, j)
        m[i] /= max(m[i].sum(), 1e-6)
    return m


def _resize(stack, h, w):
    """(n,H,W) → (n,h,w) 면적 평균"""
    ry = _area_matrix(stack.shape[1], h)
    rx = _area_matrix(stack.shape[2], w)
    return np.einsum("ij,njk,lk->nil", ry, stack.astype(np.float32), rx, optimize=True)


def _dhash(stack):
    """(n,H,W) → uint64[n]"""
    if stack.shape[1] < 2 or stack.shape[2] < 2:
        return np.zeros(len(stack), np.uint64)
    s = _resize(stack, 8, 9)
    bits = (s[:, :, 1:] > s[:, :, :-1]).reshape(len(stack), 64)
    packed = np.packbits(bits, axis=1)  # (n,8)
    return packed.view(">u8").reshape(-1).astype(np.uint64)


def _content_box(frames, info_mask):
    """검은 테두리(레터박스·필러박스) 범위 → (r0,r1,c0,c1)"""
    use = frames[info_mask] if info_mask.sum() >= 3 else frames
    if len(use) == 0:
        return 0, SIDE, 0, SIDE
    rows = np.median(use.mean(axis=2), axis=0)
    cols = np.median(use.mean(axis=1), axis=0)
    lim = SIDE * 3 // 10

    def edge(v, rev):
        idx = range(SIDE - 1, SIDE - 1 - lim, -1) if rev else range(lim)
        k = 0
        for i in idx:
            if v[i] < 20:
                k += 1
            else:
                break
        return k

    t, b, l, r = edge(rows, False), edge(rows, True), edge(cols, False), edge(cols, True)
    return t, SIDE - b, l, SIDE - r


def video_features(frames, times):
    """frames (n,64,64) uint8 → 지문 묶음"""
    n = len(frames)
    if n == 0:
        return {"t": np.zeros(0, np.float32), "h": np.zeros(0, np.uint64), "c": np.zeros(0, np.uint64),
                "f": np.zeros(0, np.uint64), "g": np.zeros((0, 10, 16), np.uint8), "ok": np.zeros(0, bool), "box": [0, 64, 0, 64]}
    fr = frames.astype(np.float32)
    std = fr.reshape(n, -1).std(axis=1)
    ok = std > 7.0
    r0, r1, c0, c1 = _content_box(fr, ok)
    if r1 - r0 < 16 or c1 - c0 < 16:
        r0, r1, c0, c1 = 0, SIDE, 0, SIDE
    cont = fr[:, r0:r1, c0:c1]
    H, W = cont.shape[1], cont.shape[2]
    cut = int(round(H * 0.8))
    main = cont[:, :cut, :]
    center = cont[:, H // 4: H - H // 4, W // 4: W - W // 4]
    h = _dhash(main)
    c = _dhash(center)
    f = _dhash(main[:, :, ::-1])
    # 밝기 격자(10x16): 아래 2줄 = 자막 구역. 같은 장면끼리 비교해 화면에 박힌 자막 찾기
    g = np.clip(_resize(cont, 10, 16), 0, 255).astype(np.uint8)
    # 화면 안 정보가 너무 적은(거의 단색) 장면은 비교에서 뺌
    mstd = main.reshape(n, -1).std(axis=1)
    ok = ok & (mstd > 6.0)
    return {"t": np.asarray(times, np.float32), "h": h, "c": c, "f": f, "g": g, "ok": ok, "box": [r0, r1, c0, c1]}


# ───────────────────────── 소리 지문 ─────────────────────────
_BANDS = None


def _band_matrix():
    global _BANDS
    if _BANDS is None:
        freqs = np.fft.rfftfreq(AUDIO_WIN, 1.0 / AUDIO_SR)
        edges = np.geomspace(300, 2000, 34)
        m = np.zeros((len(freqs), 33), np.float32)
        for b in range(33):
            m[(freqs >= edges[b]) & (freqs < edges[b + 1]), b] = 1.0
        _BANDS = m
    return _BANDS


def audio_features(pcm):
    """int16 모노 5512Hz → (fp uint32[n], ok bool[n])"""
    x = pcm.astype(np.float32) / 32768.0
    if len(x) < AUDIO_WIN * 2:
        return np.zeros(0, np.uint32), np.zeros(0, bool)
    nfr = 1 + (len(x) - AUDIO_WIN) // AUDIO_HOP
    win = np.hanning(AUDIO_WIN).astype(np.float32)
    B = _band_matrix()
    energies = np.empty((nfr, 33), np.float32)
    loud = np.empty(nfr, np.float32)
    CH = 4096
    for s in range(0, nfr, CH):
        e = min(nfr, s + CH)
        idx = (np.arange(s, e)[:, None] * AUDIO_HOP) + np.arange(AUDIO_WIN)[None, :]
        seg = x[idx] * win
        spec = np.abs(np.fft.rfft(seg, axis=1)).astype(np.float32) ** 2
        energies[s:e] = spec @ B
        loud[s:e] = np.sqrt((seg ** 2).mean(axis=1))
    d = energies[:, :-1] - energies[:, 1:]
    bits = (d[1:] - d[:-1]) > 0  # (n-1,32)
    w = (1 << np.arange(31, -1, -1, dtype=np.uint64)).astype(np.uint64)
    fp = (bits.astype(np.uint64) * w).sum(axis=1).astype(np.uint32)
    ok = loud[1:] > 0.004
    return fp, ok


# ───────────────────────── 한 번에 읽기 ─────────────────────────
def analyze(url, info, precise=False, want_audio=True, stop=None):
    """→ dict(video=..., audio=(fp, ok), last_t, errors=[(t,msg)], fallback)"""
    dur = info.get("duration") or 0
    res = _run(url, info, keyonly=not precise, want_audio=want_audio, stop=stop, step=0.5)
    # 키프레임이 너무 드문 영상(화면 녹화, 일부 H.265 등)은 1초 간격으로 다시
    if not precise and dur > 15 and len(res["times"]) < max(8, dur / 8):
        res2 = _run(url, info, keyonly=False, want_audio=False, stop=stop, step=1.0)
        if len(res2["times"]) > len(res["times"]):
            res2["audio"] = res["audio"]
            res2["errors"] = res["errors"] + res2["errors"]
            res = res2
    frames, times = res["frames"], res["times"]
    vf = video_features(frames, times)
    return {"video": vf, "audio": res["audio"], "last_t": res["last_t"], "errors": res["errors"],
            "frames_read": len(times), "killed": res.get("killed", False)}


def _run(url, info, keyonly, want_audio, stop=None, step=1.0):
    args = [fftools.FFMPEG, "-nostdin", "-hide_banner", "-nostats", "-loglevel", "info"]
    if keyonly:
        args += ["-skip_frame", "nokey"]
    args += ["-i", url, "-map", "0:v:0"]
    vf = f"scale={SIDE}:{SIDE}:flags=area,format=gray,showinfo"
    if not keyonly:
        vf = f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{step})',{vf}"
    args += ["-vf", vf, "-fps_mode", "passthrough", "-an", "-sn", "-dn", "-f", "rawvideo", "pipe:1"]
    apath = None
    if want_audio and info.get("has_audio"):
        fd, apath = tempfile.mkstemp(prefix="vdf_a_", suffix=".raw")
        os.close(fd)
        args += ["-map", "0:a:0?", "-vn", "-sn", "-dn", "-ac", "1", "-ar", str(AUDIO_SR), "-f", "s16le", "-y", apath]
    p = fftools.popen(args)
    times, errors = [], []
    last = {"t": 0.0, "active": time.time()}
    killed = {"v": False}

    def read_err():
        for raw in iter(p.stderr.readline, b""):
            ln = raw.decode("utf-8", "replace")
            last["active"] = time.time()
            if "showinfo" in ln and "pts_time:" in ln:
                m = re.search(r"pts_time:\s*(-?[\d.]+)", ln)
                if m:
                    try:
                        tt = float(m.group(1))
                        times.append(tt)
                        last["t"] = tt
                    except ValueError:
                        pass
                continue
            if ERR_PAT.search(ln) and "showinfo" not in ln:
                if len(errors) < 500:
                    errors.append((last["t"], ln.strip()[:160]))

    te = threading.Thread(target=read_err, daemon=True)
    te.start()
    fsz = SIDE * SIDE
    buf = bytearray()
    chunks = []
    limit_idle = 180

    def watchdog():
        while p.poll() is None:
            time.sleep(1)
            if (stop and stop()) or time.time() - last["active"] > limit_idle:
                killed["v"] = True
                try:
                    p.kill()
                except Exception:
                    pass
                return

    threading.Thread(target=watchdog, daemon=True).start()
    while True:
        c = p.stdout.read(65536)
        if not c:
            break
        last["active"] = time.time()
        buf += c
        k = len(buf) // fsz
        if k:
            chunks.append(np.frombuffer(bytes(buf[:k * fsz]), np.uint8).reshape(k, SIDE, SIDE))
            del buf[:k * fsz]
    p.wait()
    te.join(timeout=5)
    frames = np.concatenate(chunks) if chunks else np.zeros((0, SIDE, SIDE), np.uint8)
    n = min(len(frames), len(times))
    frames, tarr = frames[:n], np.asarray(times[:n], np.float32)
    if n:
        tarr = tarr - np.float32(info.get("start") or 0.0)
    audio = (np.zeros(0, np.uint32), np.zeros(0, bool))
    if apath:
        try:
            if os.path.getsize(apath) > 0:
                pcm = np.fromfile(apath, np.int16)
                audio = audio_features(pcm)
        finally:
            try:
                os.remove(apath)
            except OSError:
                pass
    return {"frames": frames, "times": tarr, "audio": audio, "errors": errors,
            "last_t": float(tarr[-1]) if n else 0.0, "killed": killed["v"]}


def judge_broken(info, res, precise=False):
    """읽은 결과로 깨짐 판정 → None 또는 (kind, msg, at)"""
    dur = info.get("duration") or 0
    last = res["last_t"]
    errs = res["errors"]
    if res.get("killed"):
        return None
    if res["frames_read"] == 0:
        return ("broken", "화면을 한 장도 읽지 못함 (손상)", 0.0)
    # 키프레임 간격만큼은 끝이 덜 읽힐 수 있으니 여유를 둠
    slack = max(12.0, dur * 0.03) if not precise else max(4.0, dur * 0.01)
    if dur > 20 and last < dur - slack:
        return ("broken", f"중간에 끊김 — {fmt_t(last)}까지만 있음 (전체 {fmt_t(dur)})", last)
    heavy = errs
    if len(heavy) >= (3 if precise else 6):
        t0 = heavy[0][0]
        return ("broken", f"재생 중 깨짐 — {fmt_t(t0)} 부근부터 오류 {len(heavy)}회", t0)
    return None


def fmt_t(s):
    s = int(max(0, s or 0))
    h, m, x = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{x:02d}" if h else f"{m}:{x:02d}"


# ───────────────────────── 썸네일 ─────────────────────────
def thumbnail(url, dur, out_path, at=None, width=320):
    t = at if at is not None else (min(max(dur * 0.12, 1.0), max(dur - 1, 0)) if dur else 0)
    try:
        os.remove(out_path)
    except OSError:
        pass
    args = [fftools.FFMPEG, "-nostdin", "-v", "error", "-y"]
    if t > 0:
        args += ["-ss", f"{t:.2f}"]
    args += ["-i", url, "-frames:v", "1", "-vf", f"scale={width}:-2:flags=bicubic", "-q:v", "4", out_path]
    try:
        rc, _, _ = fftools.run(args, timeout=60)
        return rc == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except Exception:
        return False


# ───────────────────────── 파일 해시 ─────────────────────────
def quick_hash(src, item):
    """크기 + 앞·가운데·뒤 256KB → 빠른 해시"""
    size = item["size"]
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    blk = 262144
    for off in sorted({0, max(0, size // 2 - blk // 2), max(0, size - blk)}):
        h.update(src.read_range(item, off, min(blk, size)))
    return h.hexdigest()


def full_hash(src, item, stop=None, progress=None):
    """파일 전체 바이트 해시 (영상으로 열지 않고 데이터만 읽음)"""
    h = hashlib.blake2b(digest_size=20)
    with src.open_stream(item) as f:
        while True:
            if stop and stop():
                return None
            c = f.read(4 << 20)
            if not c:
                break
            h.update(c)
            if progress:
                progress(len(c))
    return h.hexdigest()
