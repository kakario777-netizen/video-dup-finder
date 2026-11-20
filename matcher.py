"""지문끼리 비교해 중복 관계 찾기
1) 후보 고르기: 화면 지문 조각(21비트)·소리 지문 값이 많이 겹치는 영상 쌍만 골라냄 (전체 쌍 비교 방지)
2) 정밀 비교: 시간 차이(오프셋) 투표로 같은 장면이 같은 순서로 나오는지 확인
   → 앞뒤 길이 다름, 일부 구간 포함, 중간을 잘라낸 편집본(오프셋 여러 개)까지 잡음
3) 분류: 같은 영상 / 포함 / 일부 겹침 / 소리만 같음"""
import numpy as np

from fingerprint import AUDIO_STEP

try:
    _popcnt = np.bitwise_count
except AttributeError:  # numpy < 2.0
    _T8 = np.array([bin(i).count("1") for i in range(256)], np.uint8)

    def _popcnt(x):
        return _T8[x.view(np.uint8)].reshape(*x.shape, -1).sum(-1)

SENS = {  # 민감도: (프레임 일치 허용 비트, 덮음 기준)
    "보통": (10, 0.72),
    "높음": (12, 0.62),
    "엄격": (8, 0.82),
}


# ───────────────────────── 후보 고르기 ─────────────────────────
def _pairs_from_keys(per_video_keys, n_videos, cap, min_shared):
    if not per_video_keys:
        return {}
    keys = np.concatenate([k for _, k in per_video_keys])
    vids = np.concatenate([np.full(len(k), v, np.int64) for v, k in per_video_keys])
    if len(keys) == 0:
        return {}
    order = np.argsort(keys, kind="stable")
    keys, vids = keys[order], vids[order]
    bounds = np.flatnonzero(np.diff(keys)) + 1
    starts = np.concatenate([[0], bounds])
    ends = np.concatenate([bounds, [len(keys)]])
    sizes = ends - starts
    codes = []
    m2 = sizes == 2
    if m2.any():
        a, b = vids[starts[m2]], vids[starts[m2] + 1]
        codes.append(np.minimum(a, b) * n_videos + np.maximum(a, b))
    big = np.flatnonzero((sizes > 2) & (sizes <= cap))
    for g in big:
        vv = vids[starts[g]:ends[g]]
        i, j = np.triu_indices(len(vv), 1)
        a, b = vv[i], vv[j]
        codes.append(np.minimum(a, b) * n_videos + np.maximum(a, b))
    if not codes:
        return {}
    allc = np.concatenate(codes)
    u, cnt = np.unique(allc, return_counts=True)
    keep = cnt >= min_shared
    out = {}
    for c, k in zip(u[keep], cnt[keep]):
        a, b = divmod(int(c), n_videos)
        if a != b:
            out[(a, b)] = int(k)
    return out


def _vkeys(h, ok, salt):
    hh = h[ok]
    if len(hh) == 0:
        return np.zeros(0, np.uint64)
    parts = [((hh >> np.uint64(0)) & np.uint64(0x1FFFFF)),
             ((hh >> np.uint64(21)) & np.uint64(0x1FFFFF)),
             ((hh >> np.uint64(42)) & np.uint64(0x3FFFFF))]
    ks = [p | (np.uint64(salt * 3 + i) << np.uint64(23)) for i, p in enumerate(parts)]
    return np.unique(np.concatenate(ks))


def candidates(videos, progress=None):
    """videos: [{vid: features|None, aud: (fp,ok)|None}] → set((i,j))"""
    n = len(videos)
    cap = max(40, int(n * 0.08))
    vk, fk = [], []
    for i, v in enumerate(videos):
        f = v.get("vid")
        if f is None or len(f["h"]) == 0:
            continue
        ok = f["ok"]
        vk.append((i, np.concatenate([_vkeys(f["h"], ok, 0), _vkeys(f["c"], ok, 1)])))
        fk.append((i, _vkeys(f["f"], ok, 0)))
    pairs = set(_pairs_from_keys(vk, n, cap, 3).keys())
    # 좌우반전: 한쪽의 반전 지문 vs 다른 쪽 일반 지문
    if fk:
        mixed = [(i, k) for i, k in vk] + [(i + n, k) for i, k in fk]
        for (a, b) in _pairs_from_keys(mixed, 2 * n, cap * 2, 3):
            if (a < n) != (b < n):
                x, y = a % n, b % n
                if x != y:
                    pairs.add((min(x, y), max(x, y)))
    if progress:
        progress("화면 후보", len(pairs))
    ak = []
    for i, v in enumerate(videos):
        a = v.get("aud")
        if a is None or len(a[0]) == 0:
            continue
        fp, ok = a
        vals = np.unique(fp[ok][::2].astype(np.uint64))
        vals = vals[(vals != 0) & (vals != 0xFFFFFFFF)]
        ak.append((i, vals))
    apairs = _pairs_from_keys(ak, n, cap, 6)
    pairs |= set(apairs.keys())
    return pairs


# ───────────────────────── 화면 정밀 비교 ─────────────────────────
def _sub(f, maxn=2400):
    idx = np.flatnonzero(f["ok"])
    if len(idx) > maxn:
        idx = idx[np.linspace(0, len(idx) - 1, maxn).astype(int)]
    return idx


def _gap(t):
    if len(t) < 3:
        return 2.0
    d = np.diff(np.sort(t))
    d = d[d > 0]
    return float(np.median(d)) if len(d) else 2.0


def _segments(ta, tb, tol):
    """같은 오프셋으로 일치한 (tA,tB) 점들 → 구간 [(a0,a1,b0,b1)] (긴 공백에서 끊음)"""
    if len(ta) == 0:
        return []
    o = np.argsort(ta)
    ta, tb = ta[o], tb[o]
    off = float(np.median(tb - ta))
    segs = []
    s = 0
    for k in range(1, len(ta) + 1):
        if k == len(ta) or ta[k] - ta[k - 1] > max(30.0, tol * 8):
            a0, a1 = float(ta[s]), float(ta[k - 1])
            segs.append((a0, a1, a0 + off, a1 + off))
            s = k
    return segs


def _merge_segs(segs, tol):
    """오프셋이 거의 같고 겹치거나 붙어 있는 구간 합치기"""
    segs = sorted(segs)
    out = []
    for s in segs:
        if out:
            p = out[-1]
            if abs((s[2] - s[0]) - (p[2] - p[0])) <= tol * 3 and s[0] <= p[1] + max(30.0, tol * 8):
                out[-1] = (p[0], max(p[1], s[1]), min(p[2], s[2]), max(p[3], s[3]))
                continue
        out.append(s)
    return out


def _n_edits(segs, durA, tol):
    """의미 있는 크기의 구간이 서로 다른 오프셋으로 몇 개인지 (2개 이상이면 편집본)"""
    big = [s for s in segs if s[1] - s[0] >= max(8.0, 0.1 * durA)]
    offs = []
    for s in big:
        o = s[2] - s[0]
        if not any(abs(o - x) <= tol * 3 for x in offs):
            offs.append(o)
    return len(offs)


def compare_video(fa, fb, thr):
    """→ dict(covA, covB, segs, flip, subdiff, subwho, ...) 또는 None"""
    ia, ib = _sub(fa), _sub(fb)
    if len(ia) < 3 or len(ib) < 3:
        return None
    ta, tb = fa["t"][ia].astype(np.float64), fb["t"][ib].astype(np.float64)
    tol = max(1.5, 0.6 * max(_gap(ta), _gap(tb)))
    best = None
    for flip in (False, True):
        ha = (fa["f"] if flip else fa["h"])[ia]
        dh = _popcnt(ha[:, None] ^ fb["h"][ib][None, :])
        if not flip:
            dc = _popcnt(fa["c"][ia][:, None] ^ fb["c"][ib][None, :])
            d = np.minimum(dh, dc + 1)
        else:
            d = dh
        m = d <= thr
        if m.sum() < 3:
            continue
        ii, jj = np.nonzero(m)
        offs = tb[jj] - ta[ii]
        per = np.bincount(ii, minlength=len(ia)).astype(np.float64)
        w = 1.0 / per[ii]
        lo = offs.min()
        nb = int((offs.max() - lo) / tol) + 3
        hist = np.bincount(((offs - lo) / tol).astype(int), weights=w, minlength=nb)
        sm = hist + np.concatenate([[0], hist[:-1]]) + np.concatenate([hist[1:], [0]])
        cands = np.flatnonzero(sm >= 3.0)
        strong = []
        for c in cands[np.argsort(-sm[cands])][:40]:
            center = lo + (c + 0.5) * tol
            if any(abs(center - s) < tol * 1.5 for s in strong):
                continue
            sel = np.abs(offs - center) <= tol * 1.5
            span = ta[ii[sel]].max() - ta[ii[sel]].min() if sel.any() else 0
            durA = ta[-1] - ta[0]
            if span >= min(6.0, max(1.0, 0.3 * durA)) and len(np.unique(ii[sel])) >= 3:
                strong.append(center)
        if not strong:
            continue
        sel = np.zeros(len(offs), bool)
        for s in strong:
            sel |= np.abs(offs - s) <= tol * 1.5
        ma = np.zeros(len(ia), bool)
        mb = np.zeros(len(ib), bool)
        ma[ii[sel]] = True
        mb[jj[sel]] = True
        segs = []
        for c0 in strong:
            m0 = np.abs(offs - c0) <= tol * 1.5
            segs += _segments(ta[ii[m0]], tb[jj[m0]], tol)
        segs = _merge_segs(segs, tol)
        r = {"covA": float(ma.mean()), "covB": float(mb.mean()), "flip": flip, "segs": segs,
             "nA": len(ia), "nB": len(ib), "mA": int(ma.sum()), "mB": int(mb.sum()),
             "offsets": _n_edits(segs, ta[-1] - ta[0], tol)}
        # 같은 장면끼리 아래쪽(자막 구역) 밝기 차이 비교 → 화면에 박힌 자막
        si, sj = ii[sel], jj[sel]
        close = d[si, sj] <= min(6, thr)
        if close.sum() >= 3:
            si, sj = si[close], sj[close]
        ga = fa["g"][ia][si].astype(np.int16)
        gb = fb["g"][ib][sj].astype(np.int16)
        if flip:
            ga = ga[:, :, ::-1]
        if len(ga) >= 3:
            db = np.percentile(np.abs(ga[:, 8:] - gb[:, 8:]).reshape(len(ga), -1), 90, axis=1)
            dm = np.percentile(np.abs(ga[:, :8] - gb[:, :8]).reshape(len(ga), -1), 90, axis=1)
            r["subdiff"] = float(np.median(db)) - 1.5 * float(np.median(dm))
            r["subwho"] = float(np.median(ga[:, 8:].std(axis=2).mean(1) - gb[:, 8:].std(axis=2).mean(1))
                                + 0.5 * np.median(ga[:, 8:].mean((1, 2)) - gb[:, 8:].mean((1, 2))))
        if best is None or r["covA"] + r["covB"] > best["covA"] + best["covB"]:
            best = r
    return best


# ───────────────────────── 소리 정밀 비교 ─────────────────────────
def compare_audio(aa, ab):
    fa, oka = aa
    fb, okb = ab
    if len(fa) < 200 or len(fb) < 200:
        return None
    ia = np.flatnonzero(oka)
    if len(ia) < 100 or okb.sum() < 100:
        return None
    ob = np.argsort(fb, kind="stable")
    sb = fb[ob]
    qa = ia[::3]
    lo = np.searchsorted(sb, fa[qa], "left")
    hi = np.searchsorted(sb, fa[qa], "right")
    cnt = hi - lo
    use = (cnt > 0) & (cnt <= 8)
    if use.sum() < 5:
        return None
    pi, pj = [], []
    for k in np.flatnonzero(use):
        for x in range(lo[k], hi[k]):
            pi.append(qa[k])
            pj.append(ob[x])
    pi, pj = np.asarray(pi), np.asarray(pj)
    offs = pj - pi
    u, c = np.unique(offs, return_counts=True)
    # 이웃 오프셋(±2프레임) 합산
    order = np.argsort(-c)
    strong = []
    for k in order[:30]:
        o = int(u[k])
        tot = c[(u >= o - 2) & (u <= o + 2)].sum()
        if tot < 5:
            break
        if any(abs(o - s) <= 4 for s in strong):
            continue
        strong.append(o)
    if not strong:
        return None
    na, nb = len(fa), len(fb)
    BLK = 64
    ma = np.zeros(na, bool)
    mb = np.zeros(nb, bool)
    segs = []
    for o in strong:
        best_o, best_ber = o, 1.0
        for oo in range(o - 2, o + 3):
            a0, a1 = max(0, -oo), min(na, nb - oo)
            if a1 - a0 < BLK:
                continue
            ber = _popcnt((fa[a0:a1] ^ fb[a0 + oo:a1 + oo]).astype(np.uint32)).mean() / 32.0
            if ber < best_ber:
                best_o, best_ber = oo, ber
        o = best_o
        a0, a1 = max(0, -o), min(na, nb - o)
        if a1 - a0 < BLK:
            continue
        diff = _popcnt((fa[a0:a1] ^ fb[a0 + o:a1 + o]).astype(np.uint32)).astype(np.float32) / 32.0
        nblk = len(diff) // BLK
        if nblk == 0:
            continue
        bl = diff[:nblk * BLK].reshape(nblk, BLK).mean(axis=1)
        good = bl < 0.33
        okblk = (oka[a0:a0 + nblk * BLK].reshape(nblk, BLK).mean(axis=1) > 0.3)
        good &= okblk
        if good.sum() < 3:
            continue
        for b in np.flatnonzero(good):
            s = a0 + b * BLK
            ma[s:s + BLK] = True
            mb[s + o:s + o + BLK] = True
        gi = np.flatnonzero(good)
        # 연속 블록 묶어서 구간
        st = gi[0]
        for x in range(1, len(gi) + 1):
            if x == len(gi) or gi[x] != gi[x - 1] + 1 and gi[x] - gi[x - 1] > 8:
                s0, s1 = a0 + st * BLK, a0 + (gi[x - 1] + 1) * BLK
                segs.append((s0 * AUDIO_STEP, s1 * AUDIO_STEP, (s0 + o) * AUDIO_STEP, (s1 + o) * AUDIO_STEP))
                if x < len(gi):
                    st = gi[x]
    if not segs:
        return None
    covA = float(ma[oka].mean()) if oka.any() else 0.0
    covB = float(mb[okb].mean()) if okb.any() else 0.0
    return {"covA": covA, "covB": covB, "segs": segs, "offsets": len(strong)}


# ───────────────────────── 쌍 판정 ─────────────────────────
def judge(ea, eb, sens="보통"):
    """ea, eb: {info, vid, aud} → 관계 dict 또는 None (A 기준은 호출자가 정한 순서 그대로)"""
    thr, cov = SENS.get(sens, SENS["보통"])
    v = compare_video(ea["vid"], eb["vid"], thr) if ea.get("vid") is not None and eb.get("vid") is not None else None
    a = compare_audio(ea["aud"], eb["aud"]) if ea.get("aud") is not None and eb.get("aud") is not None else None
    vis_ok = (ea.get("vid") is not None and eb.get("vid") is not None
              and ea["vid"]["ok"].sum() >= 8 and eb["vid"]["ok"].sum() >= 8)
    vA = v["covA"] if v else 0.0
    vB = v["covB"] if v else 0.0
    aA = a["covA"] if a else 0.0
    aB = a["covB"] if a else 0.0
    notes = []
    basis = "화면"
    if vis_ok:
        cA, cB = vA, vB
        if a and vA >= 0.35:
            cA, cB = max(vA, min(aA, vA + 0.35)), max(vB, min(aB, vB + 0.35))
            basis = "화면+소리"
        if max(vA, vB) < 0.25 and a and max(aA, aB) >= cov:
            return _rel("audio_only", aA, aB, a["segs"], "소리", ["화면은 다르고 소리만 같음"], a)
        segs = v["segs"] if v else []
    else:
        if not a:
            return None
        cA, cB, segs, basis = aA, aB, a["segs"], "소리"
    if not segs:
        return None
    da = ea["info"]["duration"] or 1
    db = eb["info"]["duration"] or 1
    matchedA = sum(s[1] - s[0] for s in segs)
    if cA >= cov and cB >= cov:
        kind = "same"
    elif cA >= cov:
        kind = "contained_ab"   # A가 B 안에
    elif cB >= cov:
        kind = "contained_ba"   # B가 A 안에
    elif matchedA >= 60 and (cA >= 0.2 or cB >= 0.2):
        kind = "partial"
    else:
        return None
    src = v if (v and basis != "소리") else a
    if src and src.get("offsets", 1) >= 2 and kind != "partial":
        notes.append("중간 편집(잘린 부분) 있음")
    if v and v.get("flip"):
        notes.append("좌우 반전")
    if v and kind != "audio_only":
        if v.get("subdiff", 0) > 10.0:
            notes.append("자막A" if v.get("subwho", 0) > 0 else "자막B")
    r = _rel(kind, cA, cB, segs, basis, notes, src)
    r["durA"], r["durB"] = da, db
    return r


def _rel(kind, cA, cB, segs, basis, notes, src):
    segs = sorted(segs)
    return {"kind": kind, "covA": round(float(cA), 3), "covB": round(float(cB), 3), "basis": basis,
            "segs": [[round(float(x), 1) for x in s] for s in segs[:12]], "notes": notes}


def flip_rel(r):
    """A,B 뒤바꾼 관계"""
    x = dict(r)
    x["covA"], x["covB"] = r["covB"], r["covA"]
    x["segs"] = [[s[2], s[3], s[0], s[1]] for s in r["segs"]]
    x["kind"] = {"contained_ab": "contained_ba", "contained_ba": "contained_ab"}.get(r["kind"], r["kind"])
    x["notes"] = [{"자막A": "자막B", "자막B": "자막A"}.get(n, n) for n in r["notes"]]
    if "durA" in r:
        x["durA"], x["durB"] = r["durB"], r["durA"]
    return x


# ───────────────────────── 묶기 ─────────────────────────
class UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def quality_score(info, size):
    px = (info.get("width") or 0) * (info.get("height") or 0)
    return (round(info.get("duration") or 0, -1), px, info.get("bitrate") or 0, size)
