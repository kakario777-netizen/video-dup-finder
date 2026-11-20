"""검사 전체 흐름: 목록 → 완전 동일 파일(해시) → 영상별 지문·깨짐 확인 → 후보 쌍 → 정밀 비교 → 묶음"""
import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import cache as cachemod
import fingerprint as fpm
import matcher
import sources

DEFAULTS = {"mode": "quick", "broken": True, "precise": False, "audio": True, "sens": "보통", "workers": 0}


def fmt_size(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f}{u}" if u in ("B", "KB") else f"{n:.1f}{u}"
        n /= 1024


class Scan:
    def __init__(self, srcs, opts, cb=None, cache=None):
        self.srcs = {s.id: s for s in srcs}
        self.opts = dict(DEFAULTS, **(opts or {}))
        self.cb = cb or (lambda *a: None)
        self.cache = cache or cachemod.Cache()
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def stopped(self):
        return self._stop.is_set()

    def say(self, stage, text, frac=None):
        self.cb(stage, text, frac)

    def log(self, msg):
        """기록 파일(문제 생겼을 때 원인 확인용) — 이 PC 안에만 저장"""
        try:
            p = os.path.join(cachemod.data_dir(), "log.txt")
            if os.path.exists(p) and os.path.getsize(p) > 2_000_000:
                os.replace(p, p + ".old")
            with open(p, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
        except Exception:
            pass

    # ─────────────────────────────────────
    def run(self):
        t0 = time.time()
        opts = self.opts
        quick = opts.get("mode") == "quick"
        mode = ("p" if opts["precise"] else "k") + ("a" if opts["audio"] else "n")
        self.log(f"검사 시작 ({'빠른 검사' if quick else '내용 검사'}): " + ", ".join(s.label() for s in self.srcs.values()))
        items = []
        for s in self.srcs.values():
            self.say("list", f"목록 읽는 중: {s.label()}")
            for it in s.walk(self.stopped):
                items.append(it)
                if len(items) % 200 == 0:
                    self.say("list", f"영상 파일 {len(items)}개 찾음")
        if self.stopped():
            return None
        for i, it in enumerate(items):
            it["id"] = f"i{i}"
            it["key"] = self.cache.key(it, fpm.FP_VERSION, mode)
        self.say("list", f"영상 파일 {len(items)}개")
        self.log(f"영상 파일 {len(items)}개")
        byid = {it["id"]: it for it in items}

        # 1) 완전히 같은 파일
        exact_groups = self._exact(items)
        if self.stopped():
            return None
        copy_of = {}
        for g in exact_groups:
            for x in g[1:]:
                copy_of[x["id"]] = g[0]["id"]
        reps = [it for it in items if it["id"] not in copy_of]
        self.log(f"완전히 같은 파일 {len(exact_groups)}묶음")

        if quick:
            # 빠른 검사: 영상을 열지 않음 → 완전히 같은 파일 + 0바이트 파일만
            broken = [{"id": it["id"], "kind": "broken", "msg": "빈 파일 (0바이트)", "at": 0}
                      for it in items if it["size"] == 0]
            sets = self._group(byid, exact_groups, [])
            self.log(f"검사 끝 {time.time() - t0:.0f}초")
            return self._result(t0, items, sets, broken)

        # 2) 영상 분석
        broken = []
        ent = {}
        self._analyze(reps, mode, ent, broken)
        if self.stopped():
            return None
        for cid, rid in copy_of.items():
            c, r = byid[cid], byid[rid]
            c["info"], c["thumb"] = r.get("info"), r.get("thumb")
            rb = next((b for b in broken if b["id"] == rid), None)
            if rb:
                broken.append(dict(rb, id=cid))

        # 3) 비교
        ids = [it["id"] for it in reps if it["id"] in ent]
        vids = [ent[i] for i in ids]
        self.say("match", "비슷한 영상 후보 찾는 중…", 0)
        pairs = sorted(matcher.candidates(vids))
        rels = []
        last = time.time()
        for k, (a, b) in enumerate(pairs):
            if self.stopped():
                return None
            ia, ib = ids[a], ids[b]
            ea, eb = vids[a], vids[b]
            # 짧은 쪽을 A로
            swap = (ea["info"]["duration"] or 0) > (eb["info"]["duration"] or 0)
            if swap:
                ia, ib, ea, eb = ib, ia, eb, ea
            try:
                r = matcher.judge(ea, eb, opts["sens"])
            except Exception as e:  # 비교 실패는 건너뜀
                r = None
                print("judge error", e)
            if r:
                r["a"], r["b"] = ia, ib
                rels.append(r)
            if time.time() - last > 0.3:
                last = time.time()
                self.say("match", f"비교 {k + 1}/{len(pairs)} · 겹침 {len(rels)}건", (k + 1) / max(1, len(pairs)))
        self.say("match", f"비교 끝 · 겹침 {len(rels)}건", 1)

        sets = self._group(byid, exact_groups, rels)
        self.log(f"검사 끝 {time.time() - t0:.0f}초 · 겹침 {len(rels)}건")
        if not opts["broken"]:
            broken = [b for b in broken if b["kind"] == "readfail"]
        return self._result(t0, items, sets, broken)

    def _result(self, t0, items, sets, broken):
        opts = self.opts
        res = {
            "version": 1,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed": round(time.time() - t0, 1),
            "targets": [s.label() for s in self.srcs.values()],
            "sources": [sources.to_dict(s) for s in self.srcs.values()],
            "source_ids": [s.id for s in self.srcs.values()],
            "options": opts,
            "items": {it["id"]: _slim(it) for it in items},
            "sets": sets,
            "broken": broken,
            "count": len(items),
        }
        return res

    # ─────────────────────────────────────
    def _exact(self, items):
        """크기 같은 파일 → 앞·가운데·뒤 조각 해시 → (일반 폴더는) 전체 해시로 확정. 영상으로 열지 않음"""
        by_size = {}
        for it in items:
            if it["size"] > 0:
                by_size.setdefault(it["size"], []).append(it)
        cand = [g for g in by_size.values() if len(g) > 1]
        total = sum(len(g) for g in cand)
        self.log(f"같은 크기 파일 {total}개")
        done = 0
        groups = []
        qgroups = []
        for g in cand:
            byq = {}
            for it in g:
                if self.stopped():
                    return []
                c = self.cache.get(it["key"])
                q = c["qhash"] if c and c.get("qhash") else None
                if not q:
                    try:
                        q = fpm.quick_hash(self.srcs[it["src"]], it)
                    except Exception as e:
                        self.log(f"조각 해시 실패: {it['path']} · {e}")
                        q = None
                    if q:
                        if c:
                            self.cache.set_field(it["key"], "qhash", q)
                        else:
                            self.cache.put(it["key"], qhash=q)
                done += 1
                self.say("hash", f"같은 크기 파일 빠르게 비교 {done}/{total} · {it['name']}", done / max(1, total) * 0.3)
                if q:
                    byq.setdefault(q, []).append(it)
            qgroups += [x for x in byq.values() if len(x) > 1]
        # 전체 해시 (진행률은 읽은 용량 기준)
        need = [it for qg in qgroups if all(self.srcs[x["src"]].kind == "local" for x in qg) for it in qg]
        tot_b = sum(it["size"] for it in need) or 1
        st = {"b": 0, "t0": time.time(), "last": 0}

        def prog(n, name):
            st["b"] += n
            now = time.time()
            if now - st["last"] > 0.25:
                st["last"] = now
                fr = st["b"] / tot_b
                el = now - st["t0"]
                eta = f" · 남은 시간 약 {fpm.fmt_t(el / fr * (1 - fr))}" if fr > 0.01 and el > 3 else ""
                self.say("hash", f"완전히 같은지 끝까지 확인 {fmt_size(st['b'])}/{fmt_size(tot_b)}{eta} · {name}",
                         0.3 + 0.7 * fr)

        for qg in qgroups:
            if all(self.srcs[x["src"]].kind == "local" for x in qg):
                byf = {}
                for it in qg:
                    if self.stopped():
                        return []
                    c = self.cache.get(it["key"]) or {}
                    fh = c.get("fhash")
                    if not fh:
                        self.log(f"전체 해시: {it['path']} ({fmt_size(it['size'])})")
                        try:
                            fh = fpm.full_hash(self.srcs[it["src"]], it, self.stopped,
                                               lambda n, nm=it["name"]: prog(n, nm))
                        except Exception as e:
                            self.log(f"전체 해시 실패: {it['path']} · {e}")
                            fh = None
                        if fh:
                            if self.cache.get(it["key"]):
                                self.cache.set_field(it["key"], "fhash", fh)
                            else:
                                self.cache.put(it["key"], fhash=fh)
                    else:
                        prog(it["size"], it["name"])
                    if fh:
                        byf.setdefault(fh, []).append(it)
                groups += [x for x in byf.values() if len(x) > 1]
            else:
                groups.append(qg)  # FTP 는 조각 해시까지만 (전체를 내려받지 않음)
        for g in groups:
            g.sort(key=lambda x: (len(x["path"]), x["path"]))
        return groups

    def _analyze(self, reps, mode, ent, broken):
        opts = self.opts
        n = len(reps)
        workers = opts.get("workers") or max(1, min(4, (os.cpu_count() or 2) // 2))
        if any(s.kind == "ftp" for s in self.srcs.values()):
            workers = min(workers, 2)
        total_bytes = sum(it["size"] for it in reps) or 1
        state = {"done": 0, "bytes": 0, "t0": time.time(), "fresh": 0}
        lock = threading.Lock()

        def one(it):
            if self.stopped():
                return
            src = self.srcs[it["src"]]
            c = self.cache.get(it["key"])
            if c and (c.get("info") or c.get("broken")) and (c.get("video") is not None or c.get("broken")):
                info, br = c.get("info"), c.get("broken")
                it["info"], it["thumb"] = info, c.get("thumb")
                if info and c.get("video") is not None and not br:
                    ent[it["id"]] = {"info": info, "vid": c["video"], "aud": c.get("audio")}
                if br:
                    broken.append(dict(br, id=it["id"]))
                return
            url = src.input_url(it)
            self.log(f"읽기 시작: {it['path']} ({fmt_size(it['size'])})")
            t_file = time.time()
            info = br = None
            vfeat = afeat = None
            thumb = None
            try:
                info = fpm.probe(url, it["size"])
            except fpm.Broken as e:
                br = {"kind": e.kind, "msg": e.msg, "at": 0}
            except Exception as e:
                br = {"kind": "readfail", "msg": f"읽기 실패: {e}"[:150], "at": 0}
            if info:
                try:
                    r = fpm.analyze(url, info, precise=opts["precise"], want_audio=opts["audio"], stop=self.stopped)
                    if self.stopped():
                        return
                    vfeat, afeat = r["video"], (r["audio"] if opts["audio"] and info.get("has_audio") else None)
                    j = fpm.judge_broken(info, r, opts["precise"])
                    if j:
                        br = {"kind": j[0], "msg": j[1], "at": j[2]}
                except Exception as e:
                    br = {"kind": "readfail", "msg": f"분석 실패: {e}"[:150], "at": 0}
                tp = os.path.join(cachemod.thumb_dir(), hashlib.md5(it["key"].encode()).hexdigest() + ".jpg")
                if os.path.exists(tp) or fpm.thumbnail(url, info.get("duration") or 0, tp):
                    thumb = tp
            it["info"], it["thumb"] = info, thumb
            store = br if (br and br["kind"] != "readfail") else None
            if not (br and br["kind"] == "readfail"):
                old = c or {}
                self.cache.put(it["key"], info=info, broken=store, video=vfeat, audio=afeat,
                               qhash=old.get("qhash"), fhash=old.get("fhash"), thumb=thumb)
            if info and vfeat is not None and len(vfeat["t"]) and not (br and br["kind"] == "broken"):
                ent[it["id"]] = {"info": info, "vid": vfeat, "aud": afeat}
            if br:
                broken.append(dict(br, id=it["id"]))
            self.log(f"읽기 끝 {time.time() - t_file:.0f}초: {it['name']}" + (f" · {br['msg']}" if br else ""))
            with lock:
                state["fresh"] += 1

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(one, it): it for it in reps}
            for f in as_completed(futs):
                it = futs[f]
                try:
                    f.result()
                except Exception as e:
                    broken.append({"id": it["id"], "kind": "readfail", "msg": f"오류: {e}"[:150], "at": 0})
                with lock:
                    state["done"] += 1
                    state["bytes"] += it["size"]
                    frac = state["bytes"] / total_bytes
                    el = time.time() - state["t0"]
                    eta = ""
                    if frac > 0.02 and el > 5:
                        left = el / frac * (1 - frac)
                        eta = f" · 남은 시간 약 {fpm.fmt_t(left)}"
                    self.say("analyze", f"영상 읽는 중 {state['done']}/{n}{eta} · {it['name']}", frac)
                if self.stopped():
                    for x in futs:
                        x.cancel()
                    break

    def _group(self, byid, exact_groups, rels):
        uf = matcher.UF()
        rel_of = {}
        for g in exact_groups:
            for x in g[1:]:
                uf.union(g[0]["id"], x["id"])
        strong = [r for r in rels if r["kind"] in ("same", "contained_ab", "contained_ba")]
        for r in strong:
            uf.union(r["a"], r["b"])
            rel_of[(r["a"], r["b"])] = r
        comps = {}
        for x in list(uf.p):
            comps.setdefault(uf.find(x), []).append(x)
        sets = []
        exact_ids = {frozenset(x["id"] for x in g) for g in exact_groups}
        for members in comps.values():
            if len(members) < 2:
                continue
            members = sorted(set(members))
            # 다른 영상을 품고 있는(가장 긴) 것 → 화질 → 비트레이트 → 용량 순으로 남길 것 추천
            rl = [r for r in strong if r["a"] in members]
            is_exact = frozenset(members) in exact_ids
            if is_exact:
                keep = min(members, key=lambda i: (len(byid[i]["path"]), byid[i]["path"]))
            else:
                keep = max(members, key=lambda i: matcher.quality_score(byid[i].get("info") or {}, byid[i]["size"]))
            kinds = {r["kind"] for r in rl}
            if is_exact:
                kind = "exact"
            elif kinds and kinds <= {"same"}:
                kind = "same"
            else:
                kind = "contained" if kinds & {"contained_ab", "contained_ba"} else "same"
            sets.append({"kind": kind, "items": members, "keep": keep, "rels": rl,
                         "exact": [sorted(x["id"] for x in g) for g in exact_groups
                                   if g[0]["id"] in members],
                         "status": "pending"})
        grouped = {}
        for s in sets:
            for m in s["items"]:
                grouped[m] = s
        for r in rels:
            if r["kind"] in ("partial", "audio_only"):
                if r["a"] in grouped and grouped.get(r["a"]) is grouped.get(r["b"]):
                    continue
                keep = max((r["a"], r["b"]), key=lambda i: matcher.quality_score(byid[i].get("info") or {}, byid[i]["size"]))
                sets.append({"kind": r["kind"], "items": [r["a"], r["b"]], "keep": keep, "rels": [r],
                             "exact": [], "status": "pending"})
        order = {"exact": 0, "same": 1, "contained": 2, "partial": 3, "audio_only": 4}
        sets.sort(key=lambda s: (order[s["kind"]], -sum(byid[i]["size"] for i in s["items"])))
        for k, s in enumerate(sets):
            s["no"] = k + 1
        return sets


def _slim(it):
    return {k: it.get(k) for k in ("id", "src", "path", "name", "dir", "size", "mtime", "info", "thumb")}
