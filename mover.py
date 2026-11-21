"""정리(이동) — 지우지 않고 '중복자료' / '깨진영상' 폴더로 옮김. 원래 폴더 구조 유지, 기록 남겨 되돌리기 가능"""
import os
import time

import sources

DUP_FOLDER = "중복자료"
BROKEN_FOLDER = "깨진영상"


def side_info(src, item, roots):
    """같은 폴더의 자막 파일, 그 폴더에 영상이 몇 개인지, 폴더째 옮길 수 있는지"""
    return side_from_entries(item, src.list_dir(item["dir"]), roots)


def fill_side(res, get_src, ids, stop=None):
    """여러 영상의 자막·폴더 정보를 한 번에 채움 (같은 폴더는 한 번만 읽음) → it['side']"""
    roots = [d.get("path") or d.get("root") or "" for d in res.get("sources", [])]
    cache = {}
    for iid in ids:
        if stop and stop():
            return
        it = res["items"][iid]
        key = (it["src"], it["dir"])
        if key not in cache:
            try:
                cache[key] = get_src(it["src"]).list_dir(it["dir"])
            except Exception:
                cache[key] = None
        if cache[key] is not None:
            it["side"] = side_from_entries(it, cache[key], roots)


def side_from_entries(item, ents, roots):
    d = item["dir"]
    subs = [n for n, isdir in ents if not isdir and n != item["name"] and sources.is_sub_for(item["name"], n)]
    videos = [n for n, isdir in ents if not isdir and sources.is_video(n)]
    subdirs = [n for n, isdir in ents if isdir]
    norm = lambda p: p.rstrip("/\\").lower()  # noqa
    is_root = norm(d) in {norm(r) for r in roots} or norm(d) == "" or len(norm(d)) <= 3
    lone = len(videos) == 1 and not subdirs and not is_root
    return {"subs": subs, "videos": len(videos), "lone_folder": lone, "others": len(ents) - len(videos) - len(subs)}


def dest_root(src, custom, folder):
    if custom:
        return os.path.join(custom, folder) if src.kind == "local" else src.join(custom, folder)
    return src.default_dest(folder)


def _log(res, entry):
    res.setdefault("moves", []).append(entry)


def move_item(res, src, item, folder, custom_dest=None, subs_mode="move", subs=None, whole_folder=False,
              keeper=None, reason=""):
    """한 영상 정리. subs_mode: move(같이 옮김) / keep(그대로 둠) / attach(남길 영상 옆으로)
    → 이동 기록 목록"""
    root = dest_root(src, custom_dest, folder)
    batch = f"{time.time():.3f}"
    done = []
    if whole_folder:
        d = item["dir"]
        to = src.join(root, src.rel_for_dest(d))
        if subs_mode == "attach" and keeper and subs:
            for n in subs:
                done.append(_attach(src, item, n, keeper, batch))
        to = src.move(d, to)
        done.append({"src": src.id, "from": d, "to": to, "type": "folder", "batch": batch, "reason": reason,
                     "item": item.get("id")})
    else:
        to = src.move(item["path"], src.join(root, src.rel_for_dest(item["path"])))
        done.append({"src": src.id, "from": item["path"], "to": to, "type": "file", "batch": batch,
                     "reason": reason, "item": item.get("id")})
        for n in subs or []:
            p = src.join(item["dir"], n)
            if subs_mode == "move":
                t = src.move(p, src.join(src.dirname(to), n))
                done.append({"src": src.id, "from": p, "to": t, "type": "sub", "batch": batch, "reason": reason,
                             "item": item.get("id")})
            elif subs_mode == "attach" and keeper:
                done.append(_attach(src, item, n, keeper, batch))
    for e in done:
        e["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _log(res, e)
    return done


def _attach(src, item, sub_name, keeper, batch):
    """자막을 남길 영상 옆으로, 남길 영상 이름에 맞춰 옮김 (예: 남길영상.ko.srt)"""
    stem = os.path.splitext(item["name"])[0]
    suffix = sub_name[len(stem):] if sub_name.lower().startswith(stem.lower()) else os.path.splitext(sub_name)[1]
    kstem = os.path.splitext(keeper["name"])[0]
    p = src.join(item["dir"], sub_name)
    t = src.move(p, src.join(keeper["dir"], kstem + suffix))
    return {"src": src.id, "from": p, "to": t, "type": "sub", "batch": batch, "reason": "자막 붙이기",
            "item": item.get("id")}


def undo(res, get_src, batch=None):
    """이동 되돌리기 (batch 없으면 전부) → (성공 수, 실패 목록)"""
    moves = res.get("moves", [])
    sel = [m for m in moves if not m.get("undone") and (batch is None or m["batch"] == batch)]
    ok, fail = 0, []
    for m in reversed(sel):
        src = get_src(m["src"])
        try:
            if src.exists(m["from"]):
                raise RuntimeError("원래 자리에 같은 이름이 있음")
            src.move(m["to"], m["from"])
            m["undone"] = True
            ok += 1
        except Exception as e:
            fail.append(f"{os.path.basename(m['to'])}: {e}")
    return ok, fail
