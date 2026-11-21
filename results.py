"""검사 결과 저장 — 엑셀(셀 표) + 프로그램에서 다시 여는 json, 왼쪽 '저장된 검색 결과' 목록"""
import base64
import json
import os
import re
import time

import fingerprint as fpm

KIND_NAME = {"exact": "완전히 같은 파일", "same": "같은 영상", "contained": "포함된 영상",
             "partial": "일부 구간 겹침", "audio_only": "소리만 같음"}
REL_NAME = {"same": "같은 영상", "contained_ab": "포함", "contained_ba": "포함", "partial": "일부 겹침",
            "audio_only": "소리만 같음"}


def item_choice(s, iid):
    """영상별 선택. 체크 안 한 세트는 '미정'. 남김 = 고른 1개(pick) + '추가 남김', 나머지는 옮김"""
    ch = s.setdefault("choice", {})
    c = ch.setdefault(iid, {"subs": "move", "folder": "folder", "extra": False})
    c.setdefault("extra", False)
    c.setdefault("subs", "move")
    c.setdefault("folder", "folder")
    if not s.get("checked"):
        c["act"] = "none"
    else:
        c["act"] = "keep" if (iid == s.get("pick") or c["extra"]) else "move"
    return c


def results_dir():
    base = os.path.join(os.path.expanduser("~"), "Documents")
    if os.environ.get("VDF_RESULTS"):
        base = os.environ["VDF_RESULTS"]
    d = os.path.join(base, "중복영상 찾기 결과")
    os.makedirs(d, exist_ok=True)
    return d


def _safe(s):
    s = re.sub(r'[\\/:*?"<>|]+', "_", s).strip(" ._")
    return s[:40] or "결과"


def new_path(res):
    t = time.strftime("%Y-%m-%d_%H%M%S")
    tg = res.get("targets") or ["검사"]
    first = tg[0].rstrip("\\/")
    name = os.path.basename(first) or first
    if len(tg) > 1:
        name += f" 외 {len(tg) - 1}곳"
    return os.path.join(results_dir(), f"{t}_{_safe(name)}.json")


def _enc(res):
    x = dict(res)
    x["sources"] = []
    for s in res.get("sources", []):
        s = dict(s)
        if s.get("password"):
            s["password"] = "b64:" + base64.b64encode(s["password"].encode()).decode()
        x["sources"].append(s)
    return x


def _dec(res):
    for s in res.get("sources", []):
        p = s.get("password") or ""
        if p.startswith("b64:"):
            s["password"] = base64.b64decode(p[4:]).decode()
    return res


def save(res, path=None):
    path = path or res.get("_path") or new_path(res)
    res["_path"] = path
    body = {k: v for k, v in _enc(res).items() if not k.startswith("_")}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False)
    os.replace(tmp, path)
    try:
        write_xlsx(res, os.path.splitext(path)[0] + ".xlsx")
    except PermissionError:
        pass  # 엑셀이 열려 있으면 다음 저장 때 갱신
    return path


def load(path):
    with open(path, encoding="utf-8") as f:
        res = _dec(json.load(f))
    res["_path"] = path
    return res


def list_saved():
    d = results_dir()
    out = []
    for n in os.listdir(d):
        if not n.endswith(".json"):
            continue
        p = os.path.join(d, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({"path": p, "name": os.path.splitext(n)[0], "mtime": st.st_mtime})
    out.sort(key=lambda x: x["name"], reverse=True)
    return out


def summary(res):
    sets = res.get("sets", [])
    pend = sum(1 for s in sets if s.get("status") != "done")
    br = res.get("broken", [])
    return f"중복 {len(sets)}세트(남음 {pend}) · 깨짐 {len(br)}개 · 전체 {res.get('count', 0)}개"


def res_text(info):
    if not info:
        return ""
    h = info.get("height") or 0
    w = info.get("width") or 0
    return f"{w}x{h}"


def rel_text(rel, me_is_a, items):
    """관계 한 줄 설명 (상대 영상 기준)"""
    k = rel["kind"]
    a, b = rel["a"], rel["b"]
    segs = rel.get("segs") or []
    notes = []
    for n in rel.get("notes", []):
        if n == "자막A":
            notes.append("화면 자막 있음" if me_is_a else "상대쪽에 화면 자막")
        elif n == "자막B":
            notes.append("상대쪽에 화면 자막" if me_is_a else "화면 자막 있음")
        else:
            notes.append(n)
    other = items[b if me_is_a else a]["name"]
    if k == "same":
        t = f"'{other}'와 같은 영상"
    elif (k == "contained_ab" and me_is_a) or (k == "contained_ba" and not me_is_a):
        if segs:
            rng = (segs[0][2], segs[-1][3]) if me_is_a else (segs[0][0], segs[-1][1])
            t = f"'{other}'의 {fpm.fmt_t(rng[0])}~{fpm.fmt_t(rng[1])} 안에 포함"
        else:
            t = f"'{other}' 안에 포함"
    elif k in ("contained_ab", "contained_ba"):
        rng = (segs[0][0], segs[-1][1]) if (segs and me_is_a) else ((segs[0][2], segs[-1][3]) if segs else (0, 0))
        t = f"'{other}' 전체를 품고 있음 (내 {fpm.fmt_t(rng[0])}~{fpm.fmt_t(rng[1])})"
    elif k == "partial":
        tot = sum(s[1] - s[0] for s in segs)
        t = f"'{other}'와 약 {fpm.fmt_t(tot)} 겹침"
    else:
        t = f"'{other}'와 소리만 같음"
    if notes:
        t += " · " + ", ".join(notes)
    return t


def write_xlsx(res, path):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    items = res["items"]
    wb = Workbook()
    ws = wb.active
    ws.title = "중복 영상"
    ws.append([f"검사 대상: {', '.join(res.get('targets', []))}"])
    ws.append([f"검사 시각: {res.get('created', '')} · {summary(res)}"])
    ws.append([])
    head = ["세트", "종류", "확인", "선택", "추천", "상태", "파일명", "폴더", "용량(MB)", "길이", "해상도", "코덱", "비트레이트(kbps)",
            "관계", "판정 근거"]
    ws.append(head)
    hr = ws.max_row
    for c in ws[hr]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="44546A")
    fills = [PatternFill("solid", fgColor="F2F2F2"), PatternFill("solid", fgColor="FFFFFF")]
    blue = [PatternFill("solid", fgColor="CFE6F7"), PatternFill("solid", fgColor="E3F1FB")]
    moved = {m.get("item") for m in res.get("moves", []) if not m.get("undone") and m["type"] in ("file", "folder")}
    for s in res.get("sets", []):
        for iid in s["items"]:
            it = items[iid]
            info = it.get("info") or {}
            rel = ""
            basis = ""
            for r in s.get("rels", []):
                if iid in (r["a"], r["b"]):
                    rel = rel_text(r, iid == r["a"], items)
                    basis = r.get("basis", "")
                    break
            if not rel and s["kind"] == "exact":
                rel, basis = "완전히 같은 파일", "파일 해시"
            for g in s.get("exact", []):
                if iid in g and len(g) > 1 and not rel.startswith("완전히"):
                    rel = "완전히 같은 파일 있음 · " + rel
            st = "이동함" if iid in moved else ("정리함" if s.get("status") == "done" else "")
            ck = "확인함" if (s.get("checked") or s.get("status") == "done") else "미확인"
            act = item_choice(s, iid)["act"]
            sel = {"keep": "남김", "move": "옮김"}.get(act, "")
            if act == "keep" and iid != s.get("pick"):
                sel = "남김(추가)"
            ws.append([s["no"], KIND_NAME.get(s["kind"], s["kind"]), ck, sel,
                       "남김 추천" if iid == s.get("keep") else "", st,
                       it["name"], it["dir"], round((it["size"] or 0) / 1048576, 1),
                       fpm.fmt_t(info.get("duration")), res_text(info), info.get("vcodec", ""),
                       int((info.get("bitrate") or 0) / 1000), rel, basis])
            for c in ws[ws.max_row]:
                c.fill = blue[s["no"] % 2] if ck == "확인함" else fills[s["no"] % 2]
    widths = [6, 14, 8, 10, 9, 8, 40, 50, 10, 9, 11, 8, 14, 60, 10]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(hr + 1, 1)
    ws.auto_filter.ref = f"A{hr}:{get_column_letter(len(head))}{ws.max_row}"

    wb2 = wb.create_sheet("깨진 영상")
    wb2.append(["파일명", "폴더", "용량(MB)", "문제", "시점", "상태"])
    for c in wb2[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="833C0B")
    kn = {"broken": "", "unsupported": "[지원 안 됨] ", "readfail": "[읽기 실패] "}
    for b in res.get("broken", []):
        it = items.get(b["id"], {})
        wb2.append([it.get("name"), it.get("dir"), round((it.get("size") or 0) / 1048576, 1),
                    kn.get(b["kind"], "") + b["msg"], fpm.fmt_t(b.get("at")) if b.get("at") else "",
                    "이동함" if b.get("status") == "done" else ""])
    for i, w in enumerate([40, 50, 10, 60, 8, 8], 1):
        wb2.column_dimensions[get_column_letter(i)].width = w

    if res.get("moves"):
        w3 = wb.create_sheet("이동 기록")
        w3.append(["시각", "종류", "원래 위치", "옮긴 위치", "되돌림"])
        for c in w3[1]:
            c.font = Font(bold=True)
        tn = {"file": "영상", "folder": "폴더", "sub": "자막"}
        for m in res["moves"]:
            w3.append([m.get("time"), tn.get(m["type"], m["type"]), m["from"], m["to"], "예" if m.get("undone") else ""])
        for i, w in enumerate([19, 6, 70, 70, 7], 1):
            w3.column_dimensions[get_column_letter(i)].width = w
    for wsx in wb.worksheets:
        for row in wsx.iter_rows():
            for c in row:
                c.alignment = Alignment(vertical="center")
    wb.save(path)
