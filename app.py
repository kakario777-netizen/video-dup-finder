"""중복 영상 찾기 — 화면"""
import base64
import hashlib
import json
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import cache as cachemod
import fftools
import fingerprint as fpm
import mover
import results
import scanner
import sources
import updater
from version import VERSION

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:  # 끌어다 놓기 부품이 없어도 동작
    TkinterDnD = None
    DND_FILES = None

APP_NAME = "중복 영상 찾기"
FONT = "맑은 고딕" if sys.platform == "win32" else "DejaVu Sans"
TW, TH = 176, 99  # 목록 썸네일 (16:9)
KIND_COLOR = {"exact": "#c0392b", "same": "#d35400", "contained": "#8e44ad", "partial": "#2471a3",
              "audio_only": "#7f8c8d"}


def cfg_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    d = os.path.join(base, "VideoDupFinder")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "settings.json")


def load_cfg():
    c = {"targets": [], "mode": "quick", "thumbs": True, "broken": True, "precise": False, "audio": True, "sens": "보통", "workers": 0,
         "dest": "", "geometry": "1480x920", "hide_done": False}
    try:
        with open(cfg_path(), encoding="utf-8") as f:
            c.update(json.load(f))
    except Exception:
        pass
    for t in c["targets"]:
        p = t.get("password") or ""
        if p.startswith("b64:"):
            t["password"] = base64.b64decode(p[4:]).decode()
    return c


def save_cfg(c):
    x = dict(c)
    x["targets"] = []
    for t in c["targets"]:
        t = dict(t)
        if t.get("password"):
            t["password"] = "b64:" + base64.b64encode(t["password"].encode()).decode()
        x["targets"].append(t)
    try:
        with open(cfg_path(), "w", encoding="utf-8") as f:
            json.dump(x, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def fit_image(path, w, h, bg=(24, 24, 24)):
    """비율 유지(늘리거나 찌그러뜨리지 않음)로 상자 안에 맞춤"""
    canvas = Image.new("RGB", (w, h), bg)
    try:
        im = Image.open(path).convert("RGB")
        im.thumbnail((w, h), Image.LANCZOS)
        canvas.paste(im, ((w - im.width) // 2, (h - im.height) // 2))
    except Exception:
        pass
    return canvas


def label_info(info, size, mtime=0):
    if not info:
        t = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else ""
        return " · ".join(x for x in (scanner.fmt_size(size), f"{size:,} 바이트", t and "수정 " + t) if x)
    br = info.get("bitrate") or 0
    parts = [fpm.fmt_t(info.get("duration")), results.res_text(info), (info.get("vcodec") or "").upper(),
             scanner.fmt_size(size)]
    if br:
        parts.append(f"{br / 1e6:.1f}Mbps")
    return " · ".join(p for p in parts if p)


class Scrollable(ttk.Frame):
    """세로 스크롤 목록 (마우스 휠 지원)"""

    def __init__(self, master, on_bottom=None, bg="#f4f5f7"):
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0, bg=bg)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.sb.pack(side="right", fill="y")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win, width=e.width))
        self.on_bottom = on_bottom
        self.bind_all_wheel()

    def _on_scroll(self, a, b):
        self.sb.set(a, b)
        if self.on_bottom and float(b) > 0.92:
            self.on_bottom()

    def bind_all_wheel(self):
        def wheel(e):
            w = e.widget
            while w is not None:
                if w is self.canvas or w is self.inner:
                    break
                try:
                    w = w.master
                except Exception:
                    w = None
            if w is None:
                return
            d = -1 if (getattr(e, "num", 0) == 4 or e.delta > 0) else 1
            self.canvas.yview_scroll(d * 3, "units")

        self.canvas.bind_all("<MouseWheel>", wheel, add="+")
        self.canvas.bind_all("<Button-4>", wheel, add="+")
        self.canvas.bind_all("<Button-5>", wheel, add="+")

    def clear(self):
        for w in self.inner.winfo_children():
            w.destroy()
        self.canvas.yview_moveto(0)


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_cfg()
        root.title(f"{APP_NAME}  v{VERSION}")
        root.geometry(self.cfg.get("geometry") or "1480x920")
        root.minsize(1100, 700)
        try:
            root.iconbitmap(os.path.join(fftools.app_dir(), "icon.ico"))
        except Exception:
            pass
        self.cache = cachemod.Cache()
        self.q = []
        self.scan = None
        self.res = None
        self.srcs = {}
        self.imgs = {}
        self.rendered = 0
        self.cards = {}
        self.view_sets = []
        self._style()
        self._build()
        self._refresh_targets()
        self._refresh_saved()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._poll)
        if not fftools.available():
            messagebox.showerror(APP_NAME, "영상 엔진(FFmpeg)을 찾지 못했습니다. 프로그램을 다시 설치해주세요.")

    # ───────────────────────── 모양 ─────────────────────────
    def _style(self):
        st = ttk.Style()
        try:
            st.theme_use("vista" if sys.platform == "win32" else "clam")
        except Exception:
            pass
        self.root.option_add("*Font", (FONT, 10))
        st.configure(".", font=(FONT, 10))
        st.configure("Title.TLabel", font=(FONT, 11, "bold"))
        st.configure("Big.TButton", font=(FONT, 11, "bold"), padding=(14, 6))
        st.configure("Treeview", rowheight=26, font=(FONT, 10))
        st.configure("Treeview.Heading", font=(FONT, 10, "bold"))
        st.configure("TNotebook.Tab", font=(FONT, 10, "bold"), padding=(14, 5))

    def _build(self):
        r = self.root
        top = ttk.Frame(r, padding=(10, 8, 10, 4))
        top.pack(fill="x")
        # 검사할 곳
        tf = ttk.LabelFrame(top, text=" 검사할 곳 (폴더를 끌어다 놓아도 됩니다) ", padding=6)
        tf.pack(side="left", fill="both", expand=True)
        self.tlist = tk.Listbox(tf, height=3, activestyle="none", font=(FONT, 10), selectmode="extended")
        self.tlist.pack(side="left", fill="both", expand=True)
        bf = ttk.Frame(tf)
        bf.pack(side="left", fill="y", padx=(6, 0))
        ttk.Button(bf, text="폴더 추가", command=self.add_folder).pack(fill="x")
        ttk.Button(bf, text="FTP 추가", command=self.add_ftp).pack(fill="x", pady=3)
        ttk.Button(bf, text="빼기", command=self.remove_target).pack(fill="x")
        if TkinterDnD and DND_FILES:
            try:
                self.tlist.drop_target_register(DND_FILES)
                self.tlist.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass
        # 옵션
        of = ttk.LabelFrame(top, text=" 검사 방식 ", padding=6)
        of.pack(side="left", fill="y", padx=8)
        self.v_mode = tk.StringVar(value=self.cfg.get("mode") or "quick")
        self.v_broken = tk.BooleanVar(value=self.cfg["broken"])
        self.v_audio = tk.BooleanVar(value=self.cfg["audio"])
        self.v_precise = tk.BooleanVar(value=self.cfg["precise"])
        self.v_sens = tk.StringVar(value=self.cfg["sens"])
        self.v_thumbs = tk.BooleanVar(value=self.cfg.get("thumbs", True))
        ttk.Radiobutton(of, text="빠른 검사 — 완전히 같은 파일만 (영상을 열지 않음)", value="quick",
                        variable=self.v_mode, command=self._mode_changed).grid(row=0, column=0, sticky="w")
        self.w_thumbs = ttk.Checkbutton(of, text="찾은 중복만 썸네일 1장", variable=self.v_thumbs)
        self.w_thumbs.grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Radiobutton(of, text="내용 검사 — 화질·길이·자막이 달라도 찾음 (느림)", value="content",
                        variable=self.v_mode, command=self._mode_changed).grid(row=1, column=0, columnspan=2, sticky="w")
        self.opt_ws = []
        sub = ttk.Frame(of)
        sub.grid(row=2, column=0, columnspan=2, sticky="w", padx=(22, 0))
        for k, (t, v) in enumerate((("깨진 영상도 찾기", self.v_broken), ("소리도 비교", self.v_audio),
                                    ("정밀 검사 (모든 장면)", self.v_precise))):
            w = ttk.Checkbutton(sub, text=t, variable=v)
            w.grid(row=0, column=k, sticky="w", padx=(0, 8))
            self.opt_ws.append(w)
        sf = ttk.Frame(sub)
        sf.grid(row=1, column=0, columnspan=3, sticky="w")
        ttk.Label(sf, text="찾는 민감도").pack(side="left")
        w = ttk.Combobox(sf, textvariable=self.v_sens, values=["보통", "높음", "엄격"], width=6, state="readonly")
        w.pack(side="left", padx=4)
        self.opt_ws.append(w)
        ttk.Label(sf, text="(높음: 많이 찾음 · 엄격: 확실한 것만)", foreground="#777").pack(side="left")
        self._mode_changed()
        # 실행
        rf = ttk.Frame(top)
        rf.pack(side="left", fill="y")
        self.btn_start = ttk.Button(rf, text="검사 시작", style="Big.TButton", command=self.start_scan)
        self.btn_start.pack(fill="x")
        self.btn_stop = ttk.Button(rf, text="중지", command=self.stop_scan, state="disabled")
        self.btn_stop.pack(fill="x", pady=4)
        rr = ttk.Frame(top)
        rr.pack(side="right", fill="y", padx=(8, 0))
        self.upd_btn = ttk.Button(rr, text="업데이트 확인", command=self.check_update)
        self.upd_btn.pack(fill="x")
        ttk.Button(rr, text="설정", command=self.open_settings).pack(fill="x", pady=4)

        pf = ttk.Frame(r, padding=(10, 0, 10, 4))
        pf.pack(fill="x")
        self.prog = ttk.Progressbar(pf, mode="determinate", maximum=1000)
        self.prog.pack(fill="x")
        self.status_lbl = ttk.Label(pf, text="검사할 폴더를 추가하고 '검사 시작'을 눌러주세요.", foreground="#444")
        self.status_lbl.pack(anchor="w", pady=(2, 0))

        body = ttk.PanedWindow(r, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=(2, 10))
        # 왼쪽: 저장된 검색 결과
        left = ttk.Frame(body, padding=(0, 0, 6, 0))
        body.add(left, weight=0)
        ttk.Label(left, text="저장된 검색 결과", style="Title.TLabel").pack(anchor="w")
        lf = ttk.Frame(left)
        lf.pack(fill="both", expand=True, pady=4)
        self.saved = tk.Listbox(lf, width=34, activestyle="none", font=(FONT, 9), exportselection=False)
        ssb = ttk.Scrollbar(lf, command=self.saved.yview)
        self.saved.configure(yscrollcommand=ssb.set)
        self.saved.pack(side="left", fill="both", expand=True)
        ssb.pack(side="right", fill="y")
        self.saved.bind("<Double-Button-1>", lambda e: self.open_saved())
        self.saved.bind("<Return>", lambda e: self.open_saved())
        sb2 = ttk.Frame(left)
        sb2.pack(fill="x")
        ttk.Button(sb2, text="열기", command=self.open_saved).pack(side="left", fill="x", expand=True)
        ttk.Button(sb2, text="엑셀로 보기", command=self.open_saved_xlsx).pack(side="left", fill="x", expand=True, padx=2)
        ttk.Button(sb2, text="폴더", command=lambda: fftools.open_file(results.results_dir())).pack(side="left")
        # 오른쪽: 결과
        right = ttk.Frame(body)
        body.add(right, weight=1)
        self.res_title = ttk.Label(right, text="결과 없음", style="Title.TLabel")
        self.res_title.pack(anchor="w")
        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True, pady=(4, 0))
        # 탭1: 중복
        t1 = ttk.Frame(self.nb, padding=4)
        self.nb.add(t1, text="중복 영상")
        fb = ttk.Frame(t1)
        fb.pack(fill="x", pady=(0, 4))
        ttk.Label(fb, text="보기:").pack(side="left")
        self.v_filter = tk.StringVar(value="전체")
        cb = ttk.Combobox(fb, textvariable=self.v_filter, state="readonly", width=16,
                          values=["전체"] + list(results.KIND_NAME.values()))
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self.render_sets())
        self.v_chk = tk.StringVar(value="전체")
        cb2 = ttk.Combobox(fb, textvariable=self.v_chk, state="readonly", width=10, values=["전체", "미확인만", "확인한 것만"])
        cb2.pack(side="left", padx=4)
        cb2.bind("<<ComboboxSelected>>", lambda e: self.render_sets())
        self.v_hide = tk.BooleanVar(value=self.cfg.get("hide_done", False))
        ttk.Checkbutton(fb, text="정리 끝난 세트 숨기기", variable=self.v_hide, command=self.render_sets).pack(side="left", padx=8)
        self.btn_all = ttk.Button(fb, text="한번에 정리", style="Big.TButton", command=self.organize_all)
        self.btn_all.pack(side="right")
        self.sum_lbl = ttk.Label(fb, text="", foreground="#555")
        self.sum_lbl.pack(side="right", padx=8)
        self.list = Scrollable(t1, on_bottom=self._render_more)
        self.list.pack(fill="both", expand=True)
        # 탭2: 깨진 영상
        t2 = ttk.Frame(self.nb, padding=4)
        self.nb.add(t2, text="깨진 영상")
        bt = ttk.Frame(t2)
        bt.pack(fill="x", pady=(0, 4))
        ttk.Button(bt, text="전체 선택", command=lambda: self._broken_sel(True)).pack(side="left")
        ttk.Button(bt, text="선택 해제", command=lambda: self._broken_sel(False)).pack(side="left", padx=4)
        self.v_bsubs = tk.BooleanVar(value=True)
        self.v_bfolder = tk.BooleanVar(value=True)
        ttk.Checkbutton(bt, text="자막 파일도 같이", variable=self.v_bsubs).pack(side="left", padx=8)
        ttk.Checkbutton(bt, text="영상 하나뿐인 폴더는 폴더째", variable=self.v_bfolder).pack(side="left")
        ttk.Button(bt, text="선택한 것 '깨진영상' 폴더로 이동", command=self.move_broken).pack(side="right")
        cols = ("chk", "name", "msg", "at", "size", "dir", "st")
        self.btree = ttk.Treeview(t2, columns=cols, show="headings", selectmode="extended")
        for c, t, w in zip(cols, ["선택", "파일명", "문제", "시점", "용량", "폴더", "상태"],
                           [46, 280, 360, 60, 80, 380, 70]):
            self.btree.heading(c, text=t)
            self.btree.column(c, width=w, anchor="center" if c in ("chk", "at", "size", "st") else "w",
                              stretch=c in ("dir", "msg"))
        bsb = ttk.Scrollbar(t2, command=self.btree.yview)
        self.btree.configure(yscrollcommand=bsb.set)
        self.btree.pack(side="left", fill="both", expand=True)
        bsb.pack(side="right", fill="y")
        self.btree.bind("<Button-1>", self._broken_click)
        self.btree.bind("<Double-Button-1>", self._broken_dbl)
        self.btree.tag_configure("done", foreground="#999")
        self.btree.tag_configure("uns", foreground="#2471a3")
        self.bchk = {}
        # 탭3: 이동 기록
        t3 = ttk.Frame(self.nb, padding=4)
        self.nb.add(t3, text="이동 기록")
        mt = ttk.Frame(t3)
        mt.pack(fill="x", pady=(0, 4))
        ttk.Label(mt, text="옮긴 파일은 지워지지 않았습니다. 필요 없으면 '중복자료'·'깨진영상' 폴더에서 직접 지우세요.",
                  foreground="#555").pack(side="left")
        ttk.Button(mt, text="전체 되돌리기", command=lambda: self.undo(None)).pack(side="right")
        ttk.Button(mt, text="선택한 것 되돌리기", command=self.undo_selected).pack(side="right", padx=4)
        mcols = ("time", "type", "from", "to", "st")
        self.mtree = ttk.Treeview(t3, columns=mcols, show="headings")
        for c, t, w in zip(mcols, ["시각", "종류", "원래 위치", "옮긴 위치", "상태"], [140, 50, 480, 480, 70]):
            self.mtree.heading(c, text=t)
            self.mtree.column(c, width=w, anchor="w" if c in ("from", "to") else "center", stretch=c in ("from", "to"))
        msb = ttk.Scrollbar(t3, command=self.mtree.yview)
        self.mtree.configure(yscrollcommand=msb.set)
        self.mtree.pack(side="left", fill="both", expand=True)
        msb.pack(side="right", fill="y")

    def _mode_changed(self):
        on = self.v_mode.get() == "content"
        self.w_thumbs.config(state="disabled" if on else "normal")
        for w in self.opt_ws:
            try:
                w.config(state=("readonly" if isinstance(w, ttk.Combobox) else "normal") if on else "disabled")
            except Exception:
                pass

    # ───────────────────────── 검사할 곳 ─────────────────────────
    def _refresh_targets(self):
        self.tlist.delete(0, "end")
        for t in self.cfg["targets"]:
            self.tlist.insert("end", t["path"] if t["type"] == "local" else
                              f"FTP  {t['user']}@{t['host']}:{t.get('port', 21)}{t.get('root', '/')}")

    def _add_local(self, p):
        p = os.path.abspath(p)
        if not os.path.isdir(p):
            return
        if any(t["type"] == "local" and os.path.normcase(t["path"]) == os.path.normcase(p) for t in self.cfg["targets"]):
            return
        self.cfg["targets"].append({"type": "local", "path": p})
        self._refresh_targets()
        save_cfg(self.cfg)

    def add_folder(self):
        p = filedialog.askdirectory(title="검사할 폴더 선택")
        if p:
            self._add_local(p)

    def _on_drop(self, e):
        for p in self.root.tk.splitlist(e.data):
            self._add_local(p if os.path.isdir(p) else os.path.dirname(p))

    def add_ftp(self):
        FtpDialog(self.root, self._ftp_added)

    def _ftp_added(self, d):
        d["type"] = "ftp"
        self.cfg["targets"].append(d)
        self._refresh_targets()
        save_cfg(self.cfg)

    def remove_target(self):
        for i in sorted(self.tlist.curselection(), reverse=True):
            del self.cfg["targets"][i]
        self._refresh_targets()
        save_cfg(self.cfg)

    def get_src(self, sid):
        if sid in self.srcs:
            return self.srcs[sid]
        if self.res:
            for d, i in zip(self.res.get("sources", []), self.res.get("source_ids", [])):
                if i == sid:
                    s = sources.from_dict(d)
                    self.srcs[sid] = s
                    return s
        if sid.startswith("local:"):
            s = sources.LocalSource(sid[6:])
            self.srcs[sid] = s
            return s
        raise RuntimeError("검사 대상 정보를 찾을 수 없습니다: " + sid)

    # ───────────────────────── 검사 ─────────────────────────
    def _opts(self):
        return {"mode": self.v_mode.get(), "thumbs": self.v_thumbs.get(), "broken": self.v_broken.get(), "audio": self.v_audio.get(),
                "precise": self.v_precise.get(),
                "sens": self.v_sens.get(), "workers": int(self.cfg.get("workers") or 0)}

    def start_scan(self):
        if self.scan:
            return
        if not self.cfg["targets"]:
            messagebox.showinfo(APP_NAME, "검사할 폴더나 FTP를 먼저 추가해주세요.")
            return
        o = self._opts()
        self.cfg.update({k: o[k] for k in ("mode", "thumbs", "broken", "audio", "precise", "sens")})
        save_cfg(self.cfg)
        srcs = []
        for t in self.cfg["targets"]:
            s = sources.from_dict(t)
            if s.kind == "local" and not os.path.isdir(s.root):
                messagebox.showwarning(APP_NAME, f"폴더를 찾을 수 없습니다:\n{s.root}")
                return
            srcs.append(s)
        for s in srcs:
            self.srcs[s.id] = s
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.prog.config(value=0)
        self.scan = scanner.Scan(srcs, o, lambda st, txt, fr: self.q.append(("prog", st, txt, fr)), self.cache)
        self.scan_t0 = time.time()

        def work():
            try:
                res = self.scan.run()
                self.q.append(("done", res))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.q.append(("err", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def stop_scan(self):
        if self.scan:
            self.scan.stop()
            self.status("중지하는 중…")

    def status(self, t):
        self.status_lbl.config(text=t)

    def _poll(self):
        try:
            last = {}
            other = []
            while self.q:
                m = self.q.pop(0)
                if m[0] == "prog":
                    last = m
                else:
                    other.append(m)
            if last:
                _, st, txt, fr = last
                quick = bool(self.scan and self.scan.opts.get("mode") == "quick")
                w = ({"list": (0, 0.03), "hash": (0.03, 0.85), "thumb": (0.85, 1.0)} if quick else
                     {"list": (0, 0.02), "hash": (0.02, 0.08), "analyze": (0.08, 0.90), "match": (0.90, 1.0)}).get(st, (0, 1))
                if fr is not None and st in ("list", "hash", "analyze", "match", "thumb", "org"):
                    self.prog.config(value=1000 * (w[0] + (w[1] - w[0]) * fr))
                self.status(txt)
            for m in other:
                if m[0] == "done":
                    self._scan_done(m[1])
                elif m[0] == "err":
                    self._scan_end()
                    messagebox.showerror(APP_NAME, f"검사 중 문제가 생겼습니다.\n{m[1]}")
                elif m[0] == "call":
                    m[1]()
        except Exception:
            import traceback
            traceback.print_exc()
        self.root.after(100, self._poll)

    def _scan_end(self):
        self.scan = None
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")

    def _scan_done(self, res):
        self._scan_end()
        if res is None:
            self.prog.config(value=0)
            self.status("검사를 중지했습니다.")
            return
        self.prog.config(value=1000)
        path = results.save(res)
        self.set_result(res)
        self._refresh_saved(select=path)
        self.status(f"검사 끝 ({fpm.fmt_t(res['elapsed'])}) · {results.summary(res)} · 결과 저장: {os.path.basename(path)}")

    # ───────────────────────── 저장 목록 ─────────────────────────
    def _refresh_saved(self, select=None):
        self.saved_items = results.list_saved()
        self.saved.delete(0, "end")
        for i, s in enumerate(self.saved_items):
            nm = s["name"]
            disp = nm[:10] + " " + nm[11:13] + ":" + nm[13:15] + "  " + nm[18:] if len(nm) > 18 and nm[10] == "_" else nm
            self.saved.insert("end", disp)
            if select and s["path"] == select:
                self.saved.selection_set(i)
                self.saved.see(i)

    def _sel_saved(self):
        s = self.saved.curselection()
        return self.saved_items[s[0]] if s else None

    def open_saved(self):
        s = self._sel_saved()
        if not s:
            return
        try:
            res = results.load(s["path"])
        except Exception as e:
            messagebox.showerror(APP_NAME, f"결과를 열지 못했습니다.\n{e}")
            return
        self.set_result(res)
        self.status(f"저장된 결과를 열었습니다: {s['name']}")

    def open_saved_xlsx(self):
        s = self._sel_saved()
        if s:
            x = os.path.splitext(s["path"])[0] + ".xlsx"
            if self.res and self.res.get("_path") == s["path"]:
                results.save(self.res)
            fftools.open_file(x)

    # ───────────────────────── 결과 표시 ─────────────────────────
    def set_result(self, res):
        self.res = res
        self.srcs.update({})
        md = "빠른 검사" if (res.get("options") or {}).get("mode") == "quick" else "내용 검사"
        self.res_title.config(text=f"{res.get('created', '')} · {md} · {', '.join(res.get('targets', []))}")
        self.render_sets()
        self.render_broken()
        self.render_moves()
        threading.Thread(target=self._refresh_side, args=(res,), daemon=True).start()

    def _refresh_side(self, res):
        """자막·폴더 정보 다시 확인 (저장된 결과를 열었을 때도 최신으로)"""
        moved = {m.get("item") for m in res.get("moves", []) if not m.get("undone")}
        ids = [i for x in res["sets"] if x.get("status") != "done" for i in x["items"] if i not in moved]
        before = {i: json.dumps(res["items"][i].get("side"), sort_keys=True) for i in ids}
        try:
            mover.fill_side(res, self.get_src, ids)
        except Exception:
            return
        changed = any(json.dumps(res["items"][i].get("side"), sort_keys=True) != before[i] for i in ids)
        if changed:
            self.q.append(("call", lambda: res is self.res and self.rerender_keep()))

    def rerender_keep(self):
        """스크롤 위치 그대로 두고 다시 그림"""
        try:
            y = self.list.canvas.yview()[0]
        except Exception:
            y = 0
        n = self.rendered
        self.render_sets()
        while self.rendered < min(n, len(self.view_sets)):
            self._render_more()
        self.root.update_idletasks()
        self.list.canvas.yview_moveto(y)

    # ───────────────────────── 남김/옮김 선택 ─────────────────────────
    def choice(self, s, iid):
        """영상별 선택. 남김 = 세트에서 고른 1개(pick) + '추가 남김' 체크한 것, 나머지는 자동으로 옮김"""
        return results.item_choice(s, iid)

    def set_pick(self, s, iid):
        """'남김' 누름 → 이 영상만 남기고 나머지는 옮김 ('추가 남김' 체크한 것은 그대로 남김)"""
        s["checked"] = True
        s["pick"] = iid
        self.choice(s, iid)["extra"] = False
        self._choice_changed(s)

    def set_extra(self, s, iid, on):
        s["checked"] = True
        self.choice(s, iid)["extra"] = bool(on)
        self._choice_changed(s)

    def reset_choice(self, s):
        """선택 취소 → 미확인으로"""
        s["checked"] = False
        s["pick"] = None
        for c in s.get("choice", {}).values():
            c["extra"] = False
        self._choice_changed(s)

    def _choice_changed(self, s):
        for i in s["items"]:
            self.choice(s, i)
        self.update_sum()
        self.save_soon()
        self.root.after_idle(lambda: self.refresh_card(s))

    def save_soon(self):
        if getattr(self, "_save_job", None):
            self.root.after_cancel(self._save_job)

        def go():
            self._save_job = None
            if self.res:
                try:
                    results.save(self.res)
                except Exception:
                    pass

        self._save_job = self.root.after(1500, go)

    def exec_set(self, s):
        """세트 하나를 선택(남김/옮김·자막·폴더)대로 정리 → 오류 목록 (작업 스레드에서 호출)"""
        items = self.res["items"]
        moved = self.moved_items()
        ids = [i for i in s["items"] if i not in moved]
        if any("side" not in items[i] for i in ids):
            mover.fill_side(self.res, self.get_src, ids)
        keepers = [i for i in ids if self.choice(s, i)["act"] == "keep"]
        kid = s["keep"] if s["keep"] in keepers else (keepers[0] if keepers else None)
        keeper = dict(items[kid], id=kid) if kid else None
        dest = self.cfg.get("dest") or None
        errs = []
        for i in ids:
            c = self.choice(s, i)
            if c["act"] != "move":
                continue
            it = dict(items[i], id=i)
            si = it.get("side") or {"subs": [], "lone_folder": False}
            whole = si.get("lone_folder") and c["folder"] == "folder"
            if whole and keeper and it["dir"] == keeper["dir"]:
                whole = False
            sm = c["subs"] if (c["subs"] != "attach" or keeper) else "move"
            try:
                mover.move_item(self.res, self.get_src(it["src"]), it, mover.DUP_FOLDER, dest, sm, si.get("subs") or [],
                                whole, keeper=keeper, reason=f"세트 {s['no']}")
            except Exception as e:
                errs.append(f"{it['name']}: {e}")
        s["status"] = "done"
        return errs

    def organize_all(self):
        """목록에서 '옮김' 표시한 것 전부 한번에 정리 (두 번 확인)"""
        if not self.res or getattr(self, "busy", False):
            return
        items = self.res["items"]
        moved = self.moved_items()
        plan, skipped, nfiles, nbytes = [], 0, 0, 0
        for s in self.res["sets"]:
            if s.get("status") == "done" or not s.get("checked"):
                continue  # 확인(체크)한 세트만
            ids = [i for i in s["items"] if i not in moved]
            mv = [i for i in ids if self.choice(s, i)["act"] == "move"]
            kp = [i for i in ids if self.choice(s, i)["act"] == "keep"]
            if not mv:
                continue
            if not kp:
                skipped += 1
                continue
            plan.append(s)
            nfiles += len(mv)
            nbytes += sum(items[i]["size"] for i in mv)
        if not plan:
            messagebox.showinfo(APP_NAME, "'옮김'으로 표시된 영상이 없습니다. (확인·체크한 세트만 정리합니다)" +
                                (f"\n(남김이 하나도 없는 세트 {skipped}개는 건너뜁니다)" if skipped else ""))
            return
        msg = (f"'옮김'으로 표시한 영상 {nfiles}개 ({scanner.fmt_size(nbytes)}, {len(plan)}세트)를 "
               f"'{mover.DUP_FOLDER}' 폴더로 옮길까요?\n\n체크한(하늘색) 세트만 정리하고, 미확인 세트는 건드리지 않습니다.\n자막·폴더는 목록에서 고른 대로 처리합니다.")
        if skipped:
            msg += f"\n\n※ 남김이 하나도 없는 세트 {skipped}개는 안전을 위해 건너뜁니다."
        if not messagebox.askyesno(APP_NAME + " — 한번에 정리 (1/2)", msg):
            return
        if not messagebox.askyesno(APP_NAME + " — 마지막 확인 (2/2)",
                                   f"정말 {nfiles}개를 옮길까요?\n\n지우지 않고 옮기기만 하며, '이동 기록' 탭에서 되돌릴 수 있습니다.",
                                   icon="warning"):
            return
        self.busy = True
        self.btn_all.config(state="disabled")

        def work():
            errs = []
            for k, s in enumerate(plan):
                self.q.append(("prog", "org", f"정리 중 {k + 1}/{len(plan)}세트 · 세트 {s['no']}", (k + 1) / len(plan)))
                errs += self.exec_set(s)
            self.q.append(("call", lambda: self._after_all(errs, nfiles)))

        threading.Thread(target=work, daemon=True).start()

    def _after_all(self, errs, n):
        self.busy = False
        self.btn_all.config(state="normal")
        results.save(self.res)
        self.rerender_keep()
        self.render_broken()
        self.render_moves()
        if errs:
            messagebox.showwarning(APP_NAME, "일부는 옮기지 못했습니다.\n\n" + "\n".join(errs[:15]))
        self.status(f"한번에 정리 완료 · {n - len(errs)}개 옮김" + (f" · 실패 {len(errs)}개" if errs else ""))

    def _set_visible(self, s):
        f = self.v_filter.get()
        if f != "전체" and results.KIND_NAME.get(s["kind"]) != f:
            return False
        if self.v_hide.get() and s.get("status") == "done":
            return False
        c = self.v_chk.get()
        if c == "미확인만" and (s.get("checked") or s.get("status") == "done"):
            return False
        if c == "확인한 것만" and not s.get("checked"):
            return False
        return True

    def render_sets(self):
        self.cfg["hide_done"] = self.v_hide.get()
        self.list.clear()
        self.imgs = {}
        self.cards = {}
        self._keep_vars = []
        self.rendered = 0
        if not self.res:
            return
        self.view_sets = [s for s in self.res["sets"] if self._set_visible(s)]
        items = self.res["items"]
        total_save = 0
        for s in self.res["sets"]:
            if s.get("status") != "done":
                total_save += sum(items[i]["size"] for i in s["items"] if i != s["keep"])
        n_all = len(self.res["sets"])
        self.update_sum()
        self.nb.tab(0, text=f"중복 영상 ({n_all})")
        if not self.view_sets:
            tk.Label(self.list.inner, text="찾은 중복 영상이 없습니다." if not n_all else "표시할 세트가 없습니다.",
                     bg="#f4f5f7", fg="#777", font=(FONT, 12)).pack(pady=40)
            return
        self._render_more()

    def update_sum(self):
        if not self.res:
            return
        items = self.res["items"]
        moved = self.moved_items()
        n = b = 0
        for x in self.res["sets"]:
            if x.get("status") == "done":
                continue
            for i in x["items"]:
                if i not in moved and self.choice(x, i)["act"] == "move":
                    n += 1
                    b += items[i]["size"]
        ck = sum(1 for x in self.res["sets"] if x.get("checked") or x.get("status") == "done")
        self.sum_lbl.config(text=f"확인 {ck}/{len(self.res['sets'])}세트 · 옮김 표시 {n}개 ({scanner.fmt_size(b)})")

    def _render_more(self):
        if not self.res or self.rendered >= len(self.view_sets):
            return
        end = min(len(self.view_sets), self.rendered + 40)
        for s in self.view_sets[self.rendered:end]:
            self._card(s)
        self.rendered = end

    def _thumb(self, path, w=TW, h=TH, label=""):
        key = (path, w, h, label)
        if key not in self.imgs:
            if path and os.path.exists(path):
                im = fit_image(path, w, h)
            else:
                im = Image.new("RGB", (w, h), (52, 60, 70))
                if label:
                    from PIL import ImageDraw
                    d = ImageDraw.Draw(im)
                    try:
                        tw = d.textlength(label)
                    except Exception:
                        tw = len(label) * 6
                    d.text(((w - tw) / 2, h / 2 - 7), label, fill=(220, 225, 230))
            self.imgs[key] = ImageTk.PhotoImage(im)
        return self.imgs[key]

    @staticmethod
    def ext_label(it):
        return os.path.splitext(it["name"])[1].upper().lstrip(".") or "VIDEO"

    def best_rel(self, s, iid):
        keep = s["keep"]
        if iid == keep:
            return None
        for r in s["rels"]:
            if {r["a"], r["b"]} == {iid, keep}:
                return r
        rs = [r for r in s["rels"] if iid in (r["a"], r["b"])]
        return max(rs, key=lambda r: r["covA"] + r["covB"]) if rs else None

    def ordered(self, s):
        """남길 영상 먼저, 나머지는 긴 순서"""
        items = self.res["items"]
        rest = [i for i in s["items"] if i != s["keep"]]
        rest.sort(key=lambda i: -((items[i].get("info") or {}).get("duration") or 0))
        return [s["keep"]] + rest

    def rel_line(self, s, iid):
        items = self.res["items"]
        for g in s.get("exact", []):
            if iid in g and s["keep"] in g:
                return "남길 영상과 완전히 같은 파일" if iid != s["keep"] else ""
        r = self.best_rel(s, iid)
        if not r:
            if iid == s["keep"]:
                return ""
            for g in s.get("exact", []):
                if iid in g:
                    other = next(x for x in g if x != iid)
                    return f"'{items[other]['name']}'와 완전히 같은 파일"
            return ""
        return results.rel_text(r, iid == r["a"], items)

    def moved_items(self):
        return {m.get("item") for m in (self.res or {}).get("moves", [])
                if not m.get("undone") and m["type"] in ("file", "folder")}

    def _card(self, s):
        items = self.res["items"]
        moved = self.moved_items()
        done = s.get("status") == "done"
        checked = bool(s.get("checked"))
        bg = "#eef0f2" if done else ("#dceefb" if checked else "#ffffff")
        card = tk.Frame(self.list.inner, bg=bg, highlightthickness=2 if checked and not done else 1,
                        highlightbackground="#5dade2" if checked and not done else "#d5d8dc")
        card.pack(fill="x", padx=6, pady=5)
        self.cards[s["no"]] = card
        hd = tk.Frame(card, bg=bg)
        hd.pack(fill="x", padx=10, pady=(8, 4))
        tk.Label(hd, text=f"세트 {s['no']}", bg=bg, font=(FONT, 11, "bold")).pack(side="left")
        tk.Label(hd, text=f" {results.KIND_NAME[s['kind']]} ", bg=KIND_COLOR[s["kind"]], fg="white",
                 font=(FONT, 9, "bold")).pack(side="left", padx=8)
        basis = sorted({r.get("basis", "") for r in s["rels"]} - {""})
        extra = f"{len(s['items'])}개"
        if basis:
            extra += f" · 판정: {', '.join(basis)}"
        elif s["kind"] == "exact":
            extra += " · 판정: 파일 내용(해시) 동일"
        tk.Label(hd, text=extra, bg=bg, fg="#555").pack(side="left")
        ttk.Button(hd, text="자세히 보기" if not done else "다시 보기",
                   command=lambda s=s: DetailDialog(self, s)).pack(side="right")
        if done:
            tk.Label(hd, text="정리함", bg=bg, fg="#27ae60", font=(FONT, 10, "bold")).pack(side="right", padx=8)
        elif checked:
            ttk.Button(hd, text="선택 취소", command=lambda s=s: self.reset_choice(s)).pack(side="right", padx=4)
            tk.Label(hd, text="✓ 확인함", bg=bg, fg="#1f6fb2", font=(FONT, 10, "bold")).pack(side="right", padx=8)
        else:
            tk.Label(hd, text="미확인", bg=bg, fg="#999", font=(FONT, 10)).pack(side="right", padx=8)
        for iid in self.ordered(s):
            it = items[iid]
            gone = iid in moved
            c = self.choice(s, iid)
            rbg = bg if (done or gone or c["act"] != "move") else "#cfe3f3"
            row = tk.Frame(card, bg=rbg)
            row.pack(fill="x", padx=6, pady=2)
            im = tk.Label(row, image=self._thumb(it.get("thumb"), label=self.ext_label(it)), bg=rbg, cursor="hand2")
            im.pack(side="left", padx=4, pady=4)
            im.bind("<Button-1>", lambda e, it=it: self.play(it))
            # 오른쪽: 남김/옮김 + 자막·폴더
            if not done and not gone:
                self._row_controls(row, rbg, s, iid)
            tx = tk.Frame(row, bg=rbg)
            tx.pack(side="left", fill="x", expand=True, padx=10)
            nl = tk.Frame(tx, bg=rbg)
            nl.pack(anchor="w", fill="x")
            tk.Label(nl, text=it["name"], bg=rbg, font=(FONT, 10, "bold" if not gone else "overstrike"),
                     fg="#222" if not gone else "#999", anchor="w").pack(side="left")
            if iid == s["keep"]:
                tk.Label(nl, text=" 남김 추천 ", bg="#27ae60", fg="white", font=(FONT, 8, "bold")).pack(side="left", padx=6)
            if gone:
                tk.Label(nl, text=" 이동함 ", bg="#95a5a6", fg="white", font=(FONT, 8, "bold")).pack(side="left", padx=6)
            tk.Label(tx, text=label_info(it.get("info"), it["size"], it.get("mtime")), bg=rbg, fg="#333",
                     anchor="w").pack(anchor="w")
            tk.Label(tx, text=it["dir"], bg=rbg, fg="#777", anchor="w", font=(FONT, 9)).pack(anchor="w")
            si = it.get("side")
            if si and (si.get("subs") or si.get("lone_folder")) and not gone:
                bits = []
                if si.get("subs"):
                    bits.append(f"자막 {len(si['subs'])}개: {', '.join(si['subs'])}")
                if si.get("lone_folder"):
                    bits.append("폴더에 이 영상 하나뿐")
                tk.Label(tx, text=" · ".join(bits), bg=rbg, fg="#1f618d", anchor="w", font=(FONT, 9),
                         wraplength=700, justify="left").pack(anchor="w")
            rl = self.rel_line(s, iid)
            if rl:
                tk.Label(tx, text=rl, bg=rbg, fg=KIND_COLOR.get(s["kind"], "#555"), anchor="w",
                         font=(FONT, 9), wraplength=700, justify="left").pack(anchor="w")
        tk.Frame(card, bg=bg, height=6).pack()

    def _row_controls(self, row, rbg, s, iid):
        it = self.res["items"][iid]
        c = self.choice(s, iid)
        si = it.get("side") or {}
        box = tk.Frame(row, bg=rbg)
        box.pack(side="right", padx=8)
        r1 = tk.Frame(box, bg=rbg)
        r1.pack(anchor="e")
        is_pick = s.get("pick") == iid
        # 남김(세트에서 하나) — 누르면 나머지는 자동으로 옮김
        tk.Button(r1, text="● 남김" if is_pick else "○ 남김", width=7, font=(FONT, 10, "bold"),
                  bg="#27ae60" if is_pick else "#ffffff", fg="white" if is_pick else "#27ae60",
                  activebackground="#2ecc71", relief="raised" if not is_pick else "sunken", bd=1, cursor="hand2",
                  command=lambda: self.set_pick(s, iid)).pack(side="left", padx=1)
        # 추가 남김 — 둘 이상 남기고 싶을 때만
        ve = tk.BooleanVar(value=c["extra"] and not is_pick)
        tk.Checkbutton(r1, text="추가 남김", variable=ve, bg=rbg, activebackground=rbg, font=(FONT, 9),
                       state="disabled" if is_pick else "normal", cursor="hand2",
                       command=lambda: self.set_extra(s, iid, ve.get())).pack(side="left", padx=(6, 0))
        self._keep_vars.append(ve)
        act = c["act"]
        st = tk.Label(r1, text={"keep": "남김", "move": "→ 옮김"}.get(act, "미정"), width=6, font=(FONT, 10, "bold"),
                      bg=rbg, fg={"keep": "#27ae60", "move": "#c0392b"}.get(act, "#aaa"))
        st.pack(side="left", padx=(6, 0))
        if c["act"] == "move":
            if si.get("subs"):
                r2 = tk.Frame(box, bg=rbg)
                r2.pack(anchor="e", pady=(3, 0))
                tk.Label(r2, text="자막", bg=rbg, font=(FONT, 9)).pack(side="left")
                names = {"move": "같이 옮기기", "keep": "그대로 두기", "attach": "남길 영상 옆으로"}
                rev = {v: k for k, v in names.items()}
                vs = tk.StringVar(value=names.get(c["subs"], "같이 옮기기"))
                cb = ttk.Combobox(r2, textvariable=vs, values=list(names.values()), width=15, state="readonly")
                cb.pack(side="left", padx=4)

                def on_sub(*_):
                    c["subs"] = rev.get(vs.get(), "move")
                    self.save_soon()
                cb.bind("<<ComboboxSelected>>", on_sub)
                self._keep_vars.append(vs)
            if si.get("lone_folder"):
                vf = tk.BooleanVar(value=c["folder"] == "folder")

                def on_folder(*_):
                    c["folder"] = "folder" if vf.get() else "file"
                    self.save_soon()
                tk.Checkbutton(box, text="폴더째 옮기기", variable=vf, command=on_folder, bg=rbg,
                               activebackground=rbg, font=(FONT, 9)).pack(anchor="e")
                self._keep_vars.append(vf)

    def refresh_card(self, s):
        """세트 하나만 다시 그림(전체 스크롤 위치 유지)"""
        old = self.cards.get(s["no"])
        if not old or not old.winfo_exists():
            self.render_sets()
            return
        idx = self.list.inner.winfo_children().index(old)
        nxt = self.list.inner.winfo_children()[idx + 1] if idx + 1 < len(self.list.inner.winfo_children()) else None
        old.destroy()
        self._card(s)
        if nxt is not None:
            self.cards[s["no"]].pack_configure(before=nxt)

    def play(self, it):
        if it["src"].startswith("local:"):
            if os.path.exists(it["path"]):
                fftools.open_file(it["path"])
            else:
                messagebox.showinfo(APP_NAME, "파일이 그 자리에 없습니다. (이미 옮겼거나 지워짐)")
        else:
            messagebox.showinfo(APP_NAME, "FTP 영상은 여기서 바로 재생할 수 없습니다.\n" + it["path"])

    # ───────────────────────── 깨진 영상 ─────────────────────────
    def render_broken(self):
        self.btree.delete(*self.btree.get_children())
        self.bchk = {}
        if not self.res:
            return
        items = self.res["items"]
        kn = {"unsupported": "[지원 안 됨] ", "readfail": "[읽기 실패] ", "broken": ""}
        br = self.res.get("broken", [])
        for k, b in enumerate(br):
            it = items.get(b["id"], {})
            done = b.get("status") == "done"
            iid = f"b{k}"
            self.bchk[iid] = False
            tags = ("done",) if done else (("uns",) if b["kind"] != "broken" else ())
            self.btree.insert("", "end", iid=iid, tags=tags, values=(
                "" if done else "☐", it.get("name", ""), kn.get(b["kind"], "") + b["msg"],
                fpm.fmt_t(b["at"]) if b.get("at") else "", scanner.fmt_size(it.get("size")), it.get("dir", ""),
                "이동함" if done else ""))
        self.nb.tab(1, text=f"깨진 영상 ({len(br)})")

    def _broken_click(self, e):
        if self.btree.identify_region(e.x, e.y) != "cell":
            return
        if self.btree.identify_column(e.x) != "#1":
            return
        iid = self.btree.identify_row(e.y)
        if not iid or "done" in self.btree.item(iid, "tags"):
            return
        self.bchk[iid] = not self.bchk[iid]
        self.btree.set(iid, "chk", "☑" if self.bchk[iid] else "☐")

    def _broken_dbl(self, e):
        iid = self.btree.identify_row(e.y)
        if iid:
            b = self.res["broken"][int(iid[1:])]
            it = self.res["items"][b["id"]]
            if it["src"].startswith("local:"):
                fftools.show_in_folder(it["path"])

    def _broken_sel(self, on):
        for iid in self.btree.get_children():
            if "done" in self.btree.item(iid, "tags"):
                continue
            self.bchk[iid] = on
            self.btree.set(iid, "chk", "☑" if on else "☐")

    def move_broken(self):
        sel = [iid for iid, v in self.bchk.items() if v]
        if not sel:
            messagebox.showinfo(APP_NAME, "옮길 영상을 '선택' 칸을 눌러 체크해주세요.")
            return
        unsup = [i for i in sel if self.res["broken"][int(i[1:])]["kind"] != "broken"]
        msg = f"{len(sel)}개를 '{mover.BROKEN_FOLDER}' 폴더로 옮길까요?\n(지우지 않고 옮기기만 합니다)"
        if unsup:
            msg += f"\n\n※ 이 중 {len(unsup)}개는 손상이 아니라 '지원 안 됨/읽기 실패'입니다. 확인 후 진행하세요."
        if not messagebox.askyesno(APP_NAME, msg):
            return
        subs_on, folder_on = self.v_bsubs.get(), self.v_bfolder.get()
        jobs = [self.res["broken"][int(i[1:])] for i in sel]
        roots = self._roots()

        def work():
            errs = []
            for b in jobs:
                it = dict(self.res["items"][b["id"]], id=b["id"])
                try:
                    src = self.get_src(it["src"])
                    si = mover.side_info(src, it, roots)
                    mover.move_item(self.res, src, it, mover.BROKEN_FOLDER, self.cfg.get("dest") or None,
                                    "move" if subs_on else "keep", si["subs"] if subs_on else [],
                                    folder_on and si["lone_folder"], reason="깨진 영상")
                    b["status"] = "done"
                except Exception as e:
                    errs.append(f"{it['name']}: {e}")
            self.q.append(("call", lambda: self._after_move(errs)))

        threading.Thread(target=work, daemon=True).start()
        self.status("옮기는 중…")

    def _roots(self):
        out = []
        for d in (self.res or {}).get("sources", []):
            out.append(d.get("path") or d.get("root") or "")
        return out

    def _after_move(self, errs, s=None):
        results.save(self.res)
        self.render_broken()
        self.render_moves()
        if s is not None:
            self.refresh_card(s)
        self.update_sum()
        if errs:
            messagebox.showwarning(APP_NAME, "일부는 옮기지 못했습니다.\n\n" + "\n".join(errs[:15]))
        self.status("옮기기 완료 · 결과에 저장했습니다." if not errs else "일부 옮기기 실패")

    # ───────────────────────── 이동 기록 ─────────────────────────
    def render_moves(self):
        self.mtree.delete(*self.mtree.get_children())
        if not self.res:
            return
        tn = {"file": "영상", "folder": "폴더", "sub": "자막"}
        for k, m in enumerate(self.res.get("moves", [])):
            self.mtree.insert("", 0, iid=f"m{k}", values=(m.get("time", ""), tn.get(m["type"], m["type"]), m["from"],
                                                          m["to"], "되돌림" if m.get("undone") else ""))
        n = sum(1 for m in self.res.get("moves", []) if not m.get("undone"))
        self.nb.tab(2, text=f"이동 기록 ({n})")

    def undo_selected(self):
        sel = self.mtree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "되돌릴 줄을 선택해주세요.")
            return
        batches = {self.res["moves"][int(i[1:])]["batch"] for i in sel}
        self.undo(batches)

    def undo(self, batches):
        if not self.res or not self.res.get("moves"):
            return
        if not messagebox.askyesno(APP_NAME, "옮긴 파일을 원래 자리로 되돌릴까요?"):
            return

        def work():
            ok, fail = 0, []
            for b in (batches or [None]):
                o, f = mover.undo(self.res, self.get_src, b)
                ok += o
                fail += f
            back = {m.get("item") for m in self.res["moves"] if m.get("undone")}
            live = {m.get("item") for m in self.res["moves"] if not m.get("undone")}
            for s in self.res["sets"]:
                if s.get("status") == "done" and not (set(s["items"]) & live) and set(s["items"]) & back:
                    s["status"] = "pending"
            for b in self.res.get("broken", []):
                if b.get("status") == "done" and b["id"] in back and b["id"] not in live:
                    b["status"] = ""
            self.q.append(("call", lambda: self._after_undo(ok, fail)))

        threading.Thread(target=work, daemon=True).start()

    def _after_undo(self, ok, fail):
        results.save(self.res)
        self.render_sets()
        self.render_broken()
        self.render_moves()
        msg = f"{ok}개를 원래 자리로 되돌렸습니다."
        if fail:
            msg += "\n\n되돌리지 못한 것:\n" + "\n".join(fail[:15])
        messagebox.showinfo(APP_NAME, msg)

    # ───────────────────────── 기타 ─────────────────────────
    def open_settings(self):
        SettingsDialog(self)

    def check_update(self):
        UpdateDialog(self)

    def on_close(self):
        if self.scan:
            if not messagebox.askyesno(APP_NAME, "검사 중입니다. 중지하고 닫을까요?"):
                return
            self.scan.stop()
        try:
            self.cfg["geometry"] = self.root.geometry()
        except Exception:
            pass
        save_cfg(self.cfg)
        for s in self.srcs.values():
            s.close()
        self.cache.close()
        self.root.destroy()


# ───────────────────────── 상세 확인 창 ─────────────────────────
class DetailDialog(tk.Toplevel):
    def __init__(self, app, s):
        super().__init__(app.root)
        self.app, self.s = app, s
        self.res = app.res
        self.title(f"세트 {s['no']} 확인 · {results.KIND_NAME[s['kind']]}")
        self.geometry("1180x820")
        self.transient(app.root)
        self.imgs = []
        self.side = {}
        self.vars = {}
        top = ttk.Frame(self, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, text=f"세트 {s['no']} · {results.KIND_NAME[s['kind']]} · {len(s['items'])}개",
                  style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="남길 것과 옮길 것을 고른 뒤 아래 '정리 실행'을 누르세요. (옮기기만 하고 지우지 않습니다)",
                  foreground="#555").pack(side="left", padx=12)
        self.sc = Scrollable(self, bg="#ffffff")
        self.sc.pack(fill="both", expand=True, padx=12)
        bot = ttk.Frame(self, padding=12)
        bot.pack(fill="x")
        dest = app.cfg.get("dest") or ""
        self.dest_lbl = ttk.Label(bot, text="옮길 곳: " + (os.path.join(dest, mover.DUP_FOLDER) if dest else
                                                        f"각 드라이브(또는 FTP 서버) 맨 위의 '{mover.DUP_FOLDER}' 폴더 (원래 폴더 구조 그대로)"),
                                  foreground="#555")
        self.dest_lbl.pack(side="left")
        ttk.Button(bot, text="닫기", command=self.destroy).pack(side="right")
        self.btn_go = ttk.Button(bot, text="정리 실행", style="Big.TButton", command=self.execute)
        self.btn_go.pack(side="right", padx=8)
        self.build()
        self.cancel_frames = False
        threading.Thread(target=self._load_side, daemon=True).start()
        self.frames_thread = threading.Thread(target=self._load_frames, daemon=True)
        self.frames_thread.start()

    def build(self):
        s = self.s
        items = self.res["items"]
        moved = self.app.moved_items()
        self.blocks = {}
        for iid in self.app.ordered(s):
            it = items[iid]
            info = it.get("info") or {}
            gone = iid in moved
            f = tk.Frame(self.sc.inner, bg="#ffffff", highlightthickness=1, highlightbackground="#d5d8dc")
            f.pack(fill="x", pady=6)
            hd = tk.Frame(f, bg="#ffffff")
            hd.pack(fill="x", padx=10, pady=(8, 2))
            tk.Label(hd, text=it["name"], bg="#ffffff", font=(FONT, 11, "bold")).pack(side="left")
            if iid == s["keep"]:
                tk.Label(hd, text=" 남김 추천 ", bg="#27ae60", fg="white", font=(FONT, 8, "bold")).pack(side="left", padx=6)
            ttk.Button(hd, text="폴더 열기", command=lambda it=it: fftools.show_in_folder(it["path"])).pack(side="right")
            ttk.Button(hd, text="재생", command=lambda it=it: self.app.play(it)).pack(side="right", padx=4)
            strip = tk.Frame(f, bg="#ffffff")
            strip.pack(fill="x", padx=10, pady=4)
            lbls = []
            for k in range(5 if info else 0):
                lb = tk.Label(strip, image=self.app._thumb(it.get("thumb") if k == 0 else None, 200, 112), bg="#ffffff")
                lb.pack(side="left", padx=(0, 6))
                lbls.append(lb)
            det = tk.Frame(f, bg="#ffffff")
            det.pack(fill="x", padx=10)
            fps = info.get("fps") or 0
            rows = [("위치", it["dir"]),
                    ("용량", f"{scanner.fmt_size(it['size'])} ({it['size']:,} 바이트)")]
            if not info:
                rows += [("수정 날짜", time.strftime("%Y-%m-%d %H:%M", time.localtime(it.get("mtime") or 0))),
                         ("영상 정보", "빠른 검사라 영상을 열지 않았습니다 (파일 데이터가 완전히 같음)")]
            else:
                rows += [
                    ("길이", fpm.fmt_t(info.get("duration"))),
                    ("화질", f"{results.res_text(info)} · {(info.get('vcodec') or '').upper()} · "
                            f"{(info.get('bitrate') or 0) / 1e6:.2f}Mbps · {fps:.2f}fps"),
                    ("소리·자막", f"소리 {'있음 (' + info.get('acodec', '') + ')' if info.get('has_audio') else '없음'}"
                               f" · 영상 안 자막 트랙 {info.get('sub_tracks', 0)}개"),
                    ("수정 날짜", time.strftime("%Y-%m-%d %H:%M", time.localtime(it.get("mtime") or 0)))]
            rl = self.app.rel_line(s, iid)
            if rl:
                rows.append(("관계", rl))
            for k, (a, b) in enumerate(rows):
                tk.Label(det, text=a, bg="#ffffff", fg="#777", width=9, anchor="w").grid(row=k, column=0, sticky="w")
                tk.Label(det, text=b, bg="#ffffff", anchor="w", wraplength=950, justify="left").grid(row=k, column=1, sticky="w")
            ch = tk.Frame(f, bg="#f7f9fa")
            ch.pack(fill="x", padx=10, pady=(6, 10))
            c = self.app.choice(s, iid)
            v = tk.StringVar(value="gone" if gone else c["act"])
            self.vars[iid] = {"act": v, "subs": tk.StringVar(value=c["subs"]), "folder": tk.StringVar(value=c["folder"])}
            if gone:
                tk.Label(ch, text="이미 옮긴 영상입니다. (되돌리려면 '이동 기록' 탭)", bg="#f7f9fa", fg="#999").pack(anchor="w", padx=8, pady=6)
            else:
                r1 = tk.Frame(ch, bg="#f7f9fa")
                r1.pack(fill="x", padx=8, pady=(6, 2))
                ttk.Radiobutton(r1, text="남기기", value="keep", variable=v, command=self.update_opts).pack(side="left")
                ttk.Radiobutton(r1, text=f"'{mover.DUP_FOLDER}' 폴더로 옮기기", value="move", variable=v,
                                command=self.update_opts).pack(side="left", padx=16)
                opt = tk.Frame(ch, bg="#f7f9fa")
                opt.pack(fill="x", padx=8, pady=(0, 6))
                tk.Label(opt, text="자막·폴더 확인 중…", bg="#f7f9fa", fg="#999").pack(anchor="w")
                self.blocks[iid] = {"opt": opt}
            self.blocks.setdefault(iid, {})["lbls"] = lbls

    def _load_side(self):
        try:
            mover.fill_side(self.res, self.app.get_src, self.s["items"])
        except Exception:
            pass
        for iid in self.s["items"]:
            self.side[iid] = self.res["items"][iid].get("side") or {"subs": [], "videos": 0, "lone_folder": False,
                                                                   "others": 0}
        self.app.q.append(("call", self.update_opts))

    def _load_frames(self):
        tdir = cachemod.thumb_dir()
        for iid in self.s["items"]:
            it = self.res["items"][iid]
            dur = (it.get("info") or {}).get("duration") or 0
            if not dur:
                continue  # 빠른 검사 결과는 영상을 열지 않음
            try:
                src = self.app.get_src(it["src"])
                if src.kind == "local" and not os.path.exists(it["path"]):
                    continue
                url = src.input_url(it)
            except Exception:
                continue
            paths = []
            for k, frac in enumerate((0.3, 0.5, 0.7, 0.9)):
                if self.cancel_frames:
                    return
                h = hashlib.md5(f"{it['path']}|{it['size']}|{frac}".encode()).hexdigest()
                p = os.path.join(tdir, h + ".jpg")
                if os.path.exists(p) or fpm.thumbnail(url, dur, p, at=dur * frac, width=320):
                    paths.append((k + 1, p))
                if not self.winfo_exists():
                    return
            self.app.q.append(("call", lambda iid=iid, paths=paths: self._show_frames(iid, paths)))

    def _show_frames(self, iid, paths):
        if not self.winfo_exists():
            return
        lbls = self.blocks.get(iid, {}).get("lbls", [])
        for k, p in paths:
            if k < len(lbls):
                img = ImageTk.PhotoImage(fit_image(p, 200, 112))
                self.imgs.append(img)
                lbls[k].config(image=img)

    def update_opts(self):
        if not self.winfo_exists():
            return
        keepers = [i for i, v in self.vars.items() if v["act"].get() == "keep"]
        for iid, b in self.blocks.items():
            opt = b.get("opt")
            if opt is None:
                continue
            for w in opt.winfo_children():
                w.destroy()
            si = self.side.get(iid)
            v = self.vars[iid]
            if si is None:
                tk.Label(opt, text="자막·폴더 확인 중…", bg="#f7f9fa", fg="#999").pack(anchor="w")
                continue
            if v["act"].get() != "move":
                if si["subs"]:
                    tk.Label(opt, text=f"자막 파일 {len(si['subs'])}개: {', '.join(si['subs'])}", bg="#f7f9fa",
                             fg="#555").pack(anchor="w")
                continue
            if si["subs"]:
                r = tk.Frame(opt, bg="#f7f9fa")
                r.pack(anchor="w", pady=1)
                tk.Label(r, text=f"자막 파일 {len(si['subs'])}개 ({', '.join(si['subs'])}):", bg="#f7f9fa").pack(side="left")
                ttk.Radiobutton(r, text="같이 옮기기", value="move", variable=v["subs"]).pack(side="left", padx=6)
                ttk.Radiobutton(r, text="그대로 두기", value="keep", variable=v["subs"]).pack(side="left", padx=6)
                if keepers:
                    ttk.Radiobutton(r, text="남길 영상 옆으로 옮기기(이름 맞춤)", value="attach",
                                    variable=v["subs"]).pack(side="left", padx=6)
                elif v["subs"].get() == "attach":
                    v["subs"].set("move")
            if si["lone_folder"]:
                r = tk.Frame(opt, bg="#f7f9fa")
                r.pack(anchor="w", pady=1)
                tk.Label(r, text="이 폴더에는 이 영상 하나뿐입니다:", bg="#f7f9fa").pack(side="left")
                ttk.Radiobutton(r, text="폴더째 옮기기", value="folder", variable=v["folder"]).pack(side="left", padx=6)
                ttk.Radiobutton(r, text="영상만 옮기기", value="file", variable=v["folder"]).pack(side="left", padx=6)
            if not si["subs"] and not si["lone_folder"]:
                tk.Label(opt, text="함께 옮길 자막·폴더 없음", bg="#f7f9fa", fg="#999").pack(anchor="w")

    def execute(self):
        acts = {i: v["act"].get() for i, v in self.vars.items()}
        to_move = [i for i, a in acts.items() if a == "move"]
        keepers = [i for i, a in acts.items() if a == "keep"]
        # 창에서 고른 것을 목록 선택에도 반영
        if keepers or to_move:
            self.s["checked"] = True
        if keepers:
            if self.s.get("pick") not in keepers:
                self.s["pick"] = self.s["keep"] if self.s["keep"] in keepers else keepers[0]
        else:
            self.s["pick"] = None
        for i, v in self.vars.items():
            if acts[i] != "gone":
                c = self.app.choice(self.s, i)
                c.update(extra=(acts[i] == "keep" and i != self.s["pick"]), subs=v["subs"].get(),
                         folder=v["folder"].get())
                self.app.choice(self.s, i)
        if not to_move:
            if messagebox.askyesno(APP_NAME, "옮길 영상이 없습니다. 이 세트를 '확인함'으로 표시할까요?", parent=self):
                self.s["status"] = "done"
                self.app._after_move([], self.s)
                self.destroy()
            return
        if any(i not in self.side for i in to_move):
            messagebox.showinfo(APP_NAME, "자막·폴더 확인이 끝날 때까지 잠시만 기다려주세요.", parent=self)
            return
        if not keepers:
            if not messagebox.askyesno(APP_NAME, "남기는 영상이 하나도 없습니다. 그래도 모두 옮길까요?", parent=self, icon="warning"):
                return
        items = self.res["items"]
        names = "\n".join("· " + items[i]["name"] + ("  (폴더째)" if self.side[i].get("lone_folder") and
                                                       self.vars[i]["folder"].get() == "folder" else "") for i in to_move)
        if not messagebox.askyesno(APP_NAME, f"다음 {len(to_move)}개를 '{mover.DUP_FOLDER}' 폴더로 옮깁니다.\n\n{names}",
                                   parent=self):
            return
        self.btn_go.config(state="disabled")

        def work():
            # 미리보기 장면을 뽑느라 파일을 열고 있으면 옮기기가 막히므로 먼저 멈춤
            self.cancel_frames = True
            self.frames_thread.join(timeout=60)
            errs = self.app.exec_set(self.s)
            self.app.q.append(("call", lambda: self._done(errs)))

        threading.Thread(target=work, daemon=True).start()

    def _done(self, errs):
        self.app._after_move(errs, self.s)
        if self.winfo_exists():
            self.destroy()


# ───────────────────────── FTP 추가 창 ─────────────────────────
class FtpDialog(tk.Toplevel):
    def __init__(self, master, on_ok):
        super().__init__(master)
        self.on_ok = on_ok
        self.title("FTP 폴더 추가")
        self.resizable(False, False)
        self.transient(master)
        f = ttk.Frame(self, padding=14)
        f.pack()
        self.v = {k: tk.StringVar(value=d) for k, d in
                  (("host", ""), ("port", "21"), ("user", ""), ("password", ""), ("root", "/"), ("encoding", "utf-8"))}
        self.tls = tk.BooleanVar(value=False)
        rows = [("주소(IP)", "host"), ("포트", "port"), ("아이디", "user"), ("비밀번호", "password"), ("폴더", "root"),
                ("글자 인코딩", "encoding")]
        for k, (t, key) in enumerate(rows):
            ttk.Label(f, text=t).grid(row=k, column=0, sticky="w", pady=3)
            if key == "encoding":
                w = ttk.Combobox(f, textvariable=self.v[key], values=["utf-8", "cp949"], width=30)
            else:
                w = ttk.Entry(f, textvariable=self.v[key], width=33, show="*" if key == "password" else "")
            w.grid(row=k, column=1, sticky="w", padx=8)
        ttk.Checkbutton(f, text="보안 연결(FTPS)", variable=self.tls).grid(row=len(rows), column=1, sticky="w")
        ttk.Label(f, text="한글 파일명이 깨지면 인코딩을 cp949로 바꿔보세요.", foreground="#777").grid(
            row=len(rows) + 1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.msg = ttk.Label(f, text="", foreground="#c0392b")
        self.msg.grid(row=len(rows) + 2, column=0, columnspan=2, sticky="w")
        b = ttk.Frame(f)
        b.grid(row=len(rows) + 3, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(b, text="연결 확인 후 추가", command=self.ok).pack(side="left")
        ttk.Button(b, text="취소", command=self.destroy).pack(side="left", padx=6)

    def ok(self):
        d = {k: v.get().strip() for k, v in self.v.items()}
        d["password"] = self.v["password"].get()
        d["tls"] = self.tls.get()
        if not d["host"]:
            self.msg.config(text="주소를 입력해주세요.")
            return
        try:
            d["port"] = int(d["port"] or 21)
        except ValueError:
            self.msg.config(text="포트는 숫자로 입력해주세요.")
            return
        self.msg.config(text="연결 확인 중…", foreground="#555")
        self.update()
        try:
            s = sources.FtpSource(d["host"], d["port"], d["user"], d["password"], d["root"], d["encoding"], d["tls"])
            s.test()
            s.close()
        except Exception as e:
            self.msg.config(text=f"연결 실패: {e}"[:90], foreground="#c0392b")
            return
        self.on_ok(d)
        self.destroy()


# ───────────────────────── 설정 창 ─────────────────────────
class SettingsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("설정")
        self.resizable(False, False)
        self.transient(app.root)
        f = ttk.Frame(self, padding=16)
        f.pack()
        ttk.Label(f, text="옮길 위치", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.v_mode = tk.StringVar(value="custom" if app.cfg.get("dest") else "auto")
        self.v_dest = tk.StringVar(value=app.cfg.get("dest") or "")
        ttk.Radiobutton(f, text=f"자동: 각 드라이브(FTP는 서버) 맨 위에 '{mover.DUP_FOLDER}', '{mover.BROKEN_FOLDER}' 폴더 (추천 · 같은 드라이브라 순식간에 옮겨짐)",
                        value="auto", variable=self.v_mode).grid(row=1, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Radiobutton(f, text="직접 지정:", value="custom", variable=self.v_mode).grid(row=2, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.v_dest, width=50).grid(row=2, column=1, sticky="w")
        ttk.Button(f, text="찾아보기", command=self.pick).grid(row=2, column=2, padx=4)
        ttk.Label(f, text="(다른 드라이브를 지정하면 파일을 복사 후 옮기므로 오래 걸릴 수 있습니다)", foreground="#777").grid(
            row=3, column=0, columnspan=3, sticky="w")
        ttk.Separator(f).grid(row=4, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Label(f, text="동시에 읽을 영상 수", style="Title.TLabel").grid(row=5, column=0, sticky="w")
        self.v_w = tk.StringVar(value=str(app.cfg.get("workers") or "자동"))
        ttk.Combobox(f, textvariable=self.v_w, values=["자동", "1", "2", "3", "4", "6", "8"], width=8,
                     state="readonly").grid(row=5, column=1, sticky="w")
        ttk.Label(f, text="외장 하드(HDD)는 1~2가 빠릅니다. SSD는 자동 추천.", foreground="#777").grid(
            row=6, column=0, columnspan=3, sticky="w")
        ttk.Separator(f).grid(row=7, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Label(f, text="분석 기록", style="Title.TLabel").grid(row=8, column=0, sticky="w")
        ttk.Label(f, text="한 번 읽은 영상은 기록해 두어 다음 검사가 빠릅니다.", foreground="#777").grid(
            row=9, column=0, columnspan=2, sticky="w")
        ttk.Button(f, text="분석 기록 지우기", command=self.clear_cache).grid(row=9, column=2)
        b = ttk.Frame(f)
        b.grid(row=10, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(b, text="저장", command=self.ok).pack(side="left")
        ttk.Button(b, text="취소", command=self.destroy).pack(side="left", padx=6)

    def pick(self):
        p = filedialog.askdirectory(title="중복자료를 옮길 위치", parent=self)
        if p:
            self.v_dest.set(p)
            self.v_mode.set("custom")

    def clear_cache(self):
        if messagebox.askyesno(APP_NAME, "분석 기록을 지울까요? 다음 검사 때 모든 영상을 다시 읽습니다.", parent=self):
            self.app.cache.clear()
            messagebox.showinfo(APP_NAME, "지웠습니다.", parent=self)

    def ok(self):
        c = self.app.cfg
        c["dest"] = self.v_dest.get().strip() if self.v_mode.get() == "custom" else ""
        w = self.v_w.get()
        c["workers"] = int(w) if w.isdigit() else 0
        save_cfg(c)
        self.destroy()


# ───────────────────────── 업데이트 창 ─────────────────────────
class UpdateDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("업데이트 확인")
        self.resizable(False, False)
        self.transient(app.root)
        f = ttk.Frame(self, padding=16)
        f.pack()
        ttk.Label(f, text=f"지금 버전: {VERSION}").pack(anchor="w")
        self.msg = ttk.Label(f, text="새 버전을 확인하는 중…", wraplength=420)
        self.msg.pack(anchor="w", pady=8)
        self.prog = ttk.Progressbar(f, mode="indeterminate", length=420)
        self.prog.pack()
        self.prog.start(12)
        self.notes = tk.Text(f, height=6, width=56, wrap="word", font=(FONT, 9))
        self.btns = ttk.Frame(f)
        self.btns.pack(fill="x", pady=(12, 0))
        ttk.Button(self.btns, text="닫기", command=self.destroy).pack(side="right")
        self.q = []
        threading.Thread(target=self._check, daemon=True).start()
        self.after(100, self._poll)

    def _check(self):
        try:
            info = updater.latest_release()
        except Exception as e:
            if getattr(e, "code", None) == 404:
                self.q.append(("err", "업데이트 정보를 찾을 수 없습니다. (아직 배포된 버전이 없음)"))
            else:
                self.q.append(("err", f"새 버전을 확인하지 못했습니다. 인터넷 연결을 확인해주세요.\n({e})"))
            return
        if not updater.is_newer(info["tag"], VERSION):
            self.q.append(("latest", f"최신 버전을 쓰고 있습니다. ({info['tag'] or VERSION})"))
            return
        if not info["url"]:
            self.q.append(("err", f"새 버전 {info['tag']}의 설치 파일이 아직 준비되지 않았습니다. 잠시 뒤 다시 눌러주세요."))
            return
        self.q.append(("found", info))
        if not updater.is_frozen():
            self.q.append(("err", "개발용 실행이라 자동 적용은 건너뜁니다."))
            return
        try:
            p = updater.download(info["url"], lambda g, t: self.q.append(("prog", g, t or info["size"])))
            self.q.append(("apply", p))
        except Exception as e:
            self.q.append(("err", f"업데이트 중 문제가 생겼습니다.\n{e}"))

    def _poll(self):
        while self.q:
            m = self.q.pop(0)
            k = m[0]
            if k in ("err", "latest"):
                self.prog.stop()
                self.prog.config(mode="determinate", value=100 if k == "latest" else 0)
                self.msg.config(text=m[1])
            elif k == "found":
                info = m[1]
                self.msg.config(text=f"새 버전 {info['tag']}이 있습니다. 자동으로 내려받아 적용합니다.")
                if info["notes"].strip():
                    self.notes.insert("1.0", "바뀐 점\n" + info["notes"].strip())
                    self.notes.config(state="disabled")
                    self.notes.pack(fill="x", pady=(8, 0), before=self.btns)
                self.prog.stop()
                self.prog.config(mode="determinate", value=0, maximum=100)
            elif k == "prog":
                g, t = m[1], m[2]
                if t:
                    self.prog.config(value=g * 100 / t)
                    self.msg.config(text=f"내려받는 중... {g / 1048576:.1f} / {t / 1048576:.1f} MB")
            elif k == "apply":
                self.msg.config(text="설치 중입니다. 관리자 확인 창이 뜨면 '예'를 눌러주세요.\n설치가 끝나면 자동으로 다시 켜집니다.")
                self.update()
                try:
                    updater.run_setup(m[1])
                except Exception as e:
                    self.msg.config(text=f"설치 파일을 실행하지 못했습니다.\n{e}")
                    return
                if self.app.scan:
                    self.app.scan.stop()
                self.app.scan = None
                self.app.on_close()
                return
        if self.winfo_exists():
            self.after(100, self._poll)


# ───────────────────────── 화면 점검 ─────────────────────────
def _ui_test(app, root, res, d):
    def pump(sec):
        end = time.time() + sec
        while time.time() < end:
            root.update()
            time.sleep(0.02)

    app.set_result(res)
    pump(0.5)
    cards = len(app.list.inner.winfo_children())
    print("cards:", cards, "broken rows:", len(app.btree.get_children()), flush=True)
    s = next(x for x in res["sets"] if any(res["items"][i]["name"] == "원본 A 복사.mp4" for i in x["items"]))
    dlg = DetailDialog(app, s)
    t_end = time.time() + 30
    while time.time() < t_end and len(dlg.side) < len(s["items"]):
        pump(0.2)
    pump(1.0)
    cid = next(i for i in s["items"] if res["items"][i]["name"] == "원본 A 복사.mp4")
    for i, v in dlg.vars.items():
        v["act"].set("keep" if i == s["keep"] else ("move" if i == cid else "keep"))
    dlg.update_opts()
    side_ok = dlg.side[cid]["lone_folder"] and dlg.side[cid]["subs"] == ["원본 A 복사.srt"]
    messagebox.askyesno = lambda *a, **k: True
    messagebox.showinfo = lambda *a, **k: None
    messagebox.showwarning = lambda *a, **k: None
    dlg.execute()
    t_end = time.time() + 20
    while time.time() < t_end and s.get("status") != "done":
        pump(0.2)
    pump(0.5)
    dup_dir = os.path.join(d + "_dest", mover.DUP_FOLDER)
    moved_ok = not os.path.exists(os.path.join(d, "복사본")) and any(
        "복사본" in dp for dp, _, _ in os.walk(dup_dir))
    print("side_ok", side_ok, "moved_ok", moved_ok, "moves", len(res.get("moves", [])), flush=True)
    # 되돌리기
    app.undo(None)
    t_end = time.time() + 20
    while time.time() < t_end and not os.path.exists(os.path.join(d, "복사본", "원본 A 복사.srt")):
        pump(0.2)
    pump(0.5)
    undo_ok = os.path.exists(os.path.join(d, "복사본", "원본 A 복사.mp4"))
    # 목록에서 바로 남김/옮김 → 한번에 정리 (두 번 확인)
    asked = []
    messagebox.askyesno = lambda *a, **k: asked.append(a) or True
    for x in res["sets"]:
        x.pop("choice", None)
        x.pop("pick", None)
        x.pop("checked", None)
    default_ok = all(app.choice(x, i)["act"] == "none" for x in res["sets"] for i in x["items"])
    for x in res["sets"]:
        for i in x["items"]:
            app.set_extra(x, i, True)               # 다른 세트는 전부 남김
    s["status"] = "pending"
    others = [i for i in s["items"] if i != cid]
    app.set_pick(s, cid)                          # 남김 하나 누르면 나머지 자동 옮김
    excl_ok = all(app.choice(s, i)["act"] == ("keep" if app.choice(s, i)["extra"] else "move") for i in others)
    for i in others:
        app.set_extra(s, i, False)
    excl_ok = excl_ok and all(app.choice(s, i)["act"] == "move" for i in others) and app.choice(s, cid)["act"] == "keep"
    app.set_pick(s, others[0])                    # 다시: others[0] 남김 + 추가 남김(나머지, 복사본 제외)
    for i in others[1:]:
        app.set_extra(s, i, True)
    excl_ok = excl_ok and app.choice(s, cid)["act"] == "move"
    excl_ok = excl_ok and default_ok
    print("excl_ok", excl_ok, "default_ok", default_ok, flush=True)
    app.rerender_keep()
    pump(0.3)
    app.organize_all()
    t_end = time.time() + 20
    while time.time() < t_end and getattr(app, "busy", False):
        pump(0.2)
    pump(0.5)
    bulk_ok = excl_ok and (len(asked) == 2 and not os.path.exists(os.path.join(d, "복사본")) and s.get("status") == "done")
    thumbs = [res["items"][i].get("thumb") for x in res["sets"] for i in x["items"]]
    thumb_ok = all(t and os.path.exists(t) for t in thumbs)
    side_card = res["items"][cid].get("side", {}).get("subs") == ["원본 A 복사.srt"]
    print("bulk_ok", bulk_ok, "asked", len(asked), "thumb_ok", thumb_ok, "side_card", side_card, flush=True)
    app.undo(None)
    t_end = time.time() + 20
    while time.time() < t_end and not os.path.exists(os.path.join(d, "복사본")):
        pump(0.2)
    pump(0.5)
    undo_ok = undo_ok and bulk_ok and thumb_ok and side_card
    xlsx_ok = os.path.exists(os.path.splitext(res["_path"])[0] + ".xlsx")
    try:
        xlsx_ok = xlsx_ok and len(results.load(res["_path"]).get("moves", [])) == len(res["moves"]) > 0
    except Exception as e:
        print("reload fail", e, flush=True)
        xlsx_ok = False
    print("undo_ok", undo_ok, "xlsx_ok", xlsx_ok, "saved", len(app.saved_items), flush=True)
    return cards >= 1 and side_ok and moved_ok and undo_ok and xlsx_ok


def main():
    test = "--selftest" in sys.argv
    if test:
        import tempfile
        tmp = tempfile.mkdtemp(prefix="vdf_cfg_")
        os.environ["APPDATA"] = tmp
        os.environ["LOCALAPPDATA"] = tmp
        os.environ["VDF_RESULTS"] = tmp
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    app = App(root)
    if "--updated" in sys.argv:
        threading.Thread(target=updater.cleanup_later, daemon=True).start()
        app.status(f"{VERSION} 버전으로 업데이트했습니다.")
    if test:
        if "--selftest-out" in sys.argv:
            out = open(sys.argv[sys.argv.index("--selftest-out") + 1], "w", encoding="utf-8")
            sys.stdout = sys.stderr = out
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

        def run_test():
            try:
                import selftest
                print("ffmpeg:", fftools.FFMPEG, flush=True)
                ok, d, res = selftest.run(lambda *a: print(*a, flush=True))
                app.cfg["dest"] = d + "_dest"
                res["_path"] = results.save(res)
                app._refresh_saved()
                ui = _ui_test(app, root, res, d)
                print("dnd:", bool(TkinterDnD), flush=True)
                print("ui ok" if (ok and ui) else "ui FAIL", flush=True)
                if not (ok and ui):
                    sys.exitcode = 1
            except Exception:
                import traceback
                traceback.print_exc()
                sys.exitcode = 1
            finally:
                try:
                    out_ = sys.stdout
                    out_.flush()
                except Exception:
                    pass
                root.destroy()

        root.after(800, run_test)
    root.mainloop()
    if getattr(sys, "exitcode", 0):
        sys.exit(1)


if __name__ == "__main__":
    main()
