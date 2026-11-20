"""검사 대상 저장소 — 일반 폴더 / FTP 폴더를 같은 방식으로 다루기
FTP 영상은 내부 중계(127.0.0.1)로 FFmpeg에 넘겨서 필요한 부분만 읽음(되감기·건너뛰기 가능)"""
import ftplib
import http.server
import os
import posixpath
import secrets
import socketserver
import threading
import time
import urllib.parse

VIDEO_EXT = {
    ".mp4", ".m4v", ".mkv", ".avi", ".wmv", ".asf", ".mov", ".qt", ".flv", ".f4v", ".webm", ".ts", ".m2ts",
    ".mts", ".tp", ".trp", ".mpg", ".mpeg", ".mpe", ".m1v", ".m2v", ".vob", ".3gp", ".3g2", ".rm", ".rmvb",
    ".ogm", ".ogv", ".divx", ".xvid", ".dv", ".mxf", ".y4m", ".h264", ".h265", ".hevc", ".264", ".265",
    ".dat", ".k3g", ".skm", ".evo", ".nsv", ".amv", ".mod", ".tod", ".dvr-ms", ".wtv", ".bik", ".hdmov",
}
SUB_EXT = {".srt", ".smi", ".sami", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup", ".txt", ".lrc"}
SKIP_DIRS = {"중복자료", "깨진영상", "$RECYCLE.BIN", "System Volume Information", "#recycle", "@eaDir", ".Trash"}


def is_video(name):
    return os.path.splitext(name)[1].lower() in VIDEO_EXT


def is_sub_for(video_name, other):
    """other 가 video_name 의 자막 파일인지 (이름.srt, 이름.ko.srt, 이름.kor.smi 등)"""
    stem = os.path.splitext(video_name)[0].lower()
    o = other.lower()
    ext = os.path.splitext(o)[1]
    if ext not in SUB_EXT:
        return False
    if ext == ".txt" or ext == ".lrc":
        return o[: -len(ext)] == stem
    return o.startswith(stem + ".") or o[: -len(ext)] == stem


# ───────────────────────── 일반 폴더 ─────────────────────────
class LocalSource:
    kind = "local"

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.id = "local:" + self.root

    def label(self):
        return self.root

    def walk(self, stop=None):
        for dp, dns, fns in os.walk(self.root):
            if stop and stop():
                return
            dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in fns:
                if not is_video(fn):
                    continue
                p = os.path.join(dp, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                yield {"src": self.id, "path": p, "name": fn, "dir": dp, "size": st.st_size, "mtime": int(st.st_mtime)}

    def input_url(self, item):
        return item["path"]

    def read_range(self, item, offset, length):
        with open(item["path"], "rb") as f:
            f.seek(offset)
            return f.read(length)

    def open_stream(self, item):
        return open(item["path"], "rb")

    def list_dir(self, d):
        try:
            return [(n, os.path.isdir(os.path.join(d, n))) for n in os.listdir(d)]
        except OSError:
            return []

    def join(self, *a):
        return os.path.join(*a)

    def dirname(self, p):
        return os.path.dirname(p)

    def basename(self, p):
        return os.path.basename(p)

    def exists(self, p):
        return os.path.exists(p)

    def default_dest(self, folder_name):
        drive = os.path.splitdrive(self.root)[0]
        if drive:
            return drive + os.sep + folder_name
        return os.path.join(os.path.dirname(self.root.rstrip(os.sep)) or os.sep, folder_name)

    def rel_for_dest(self, p):
        """이동 대상 안에 원래 구조를 남길 상대 경로"""
        drive, rest = os.path.splitdrive(os.path.abspath(p))
        rest = rest.lstrip("\\/")
        if drive.startswith("\\\\"):  # 네트워크 경로
            rest = os.path.join(drive.strip("\\").replace("\\", "_"), rest)
        return rest

    def move(self, src, dst):
        import shutil
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        dst = unique_path(dst, os.path.exists)
        for k in range(6):  # 다른 프로그램이 잠깐 잡고 있으면 조금 기다렸다 다시
            try:
                if same_drive(src, dst):
                    os.rename(src, dst)
                else:
                    shutil.move(src, dst)
                return dst
            except PermissionError:
                if k == 5:
                    raise PermissionError("다른 프로그램이 파일을 사용 중입니다 (재생 중이면 닫고 다시 시도)")
                time.sleep(0.7)

    def rmdir_if_empty(self, d):
        try:
            os.rmdir(d)
        except OSError:
            pass

    def close(self):
        pass


def same_drive(a, b):
    try:
        return os.stat(a).st_dev == os.stat(os.path.dirname(b)).st_dev
    except OSError:
        return False


def unique_path(p, exists):
    if not exists(p):
        return p
    base, ext = os.path.splitext(p)
    i = 2
    while exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


# ───────────────────────── FTP ─────────────────────────
class FtpSource:
    kind = "ftp"

    def __init__(self, host, port=21, user="", password="", root="/", encoding="utf-8", tls=False, passive=True):
        self.host, self.port = host.strip(), int(port or 21)
        self.user, self.password = user or "anonymous", password or ""
        self.root = "/" + (root or "/").strip().strip("/")
        if self.root != "/":
            self.root = self.root.rstrip("/")
        self.encoding, self.tls, self.passive = encoding or "utf-8", bool(tls), passive
        self.id = f"ftp://{self.user}@{self.host}:{self.port}{self.root}"
        self._lock = threading.Lock()
        self._main = None

    def label(self):
        return f"FTP {self.host}:{self.port}{self.root}"

    def to_dict(self):
        return {"host": self.host, "port": self.port, "user": self.user, "password": self.password,
                "root": self.root, "encoding": self.encoding, "tls": self.tls}

    def connect(self):
        cls = ftplib.FTP_TLS if self.tls else ftplib.FTP
        f = cls(encoding=self.encoding, timeout=30)
        f.connect(self.host, self.port)
        f.login(self.user, self.password)
        if self.tls:
            f.prot_p()
        f.set_pasv(self.passive)
        if self.encoding.lower().replace("-", "") == "utf8":
            try:
                f.sendcmd("OPTS UTF8 ON")
            except Exception:
                pass
        return f

    def _conn(self):
        if self._main is None:
            self._main = self.connect()
        else:
            try:
                self._main.voidcmd("NOOP")
            except Exception:
                self._main = self.connect()
        return self._main

    def test(self):
        with self._lock:
            f = self._conn()
            f.cwd(self.root)
            return True

    def _listdir(self, f, d):
        """[(이름, 폴더여부, 크기, 수정시각)]"""
        out = []
        try:
            for name, facts in f.mlsd(d, facts=["type", "size", "modify"]):
                t = facts.get("type", "")
                if name in (".", "..") or t in ("cdir", "pdir"):
                    continue
                mt = 0
                m = facts.get("modify")
                if m:
                    try:
                        mt = int(time.mktime(time.strptime(m[:14], "%Y%m%d%H%M%S")))
                    except Exception:
                        pass
                out.append((name, t == "dir", int(facts.get("size") or 0), mt))
            return out
        except ftplib.error_perm:
            pass
        lines = []
        f.retrlines("LIST " + d, lines.append)
        for ln in lines:
            parts = ln.split(None, 8)
            if len(parts) < 9:
                continue
            name = parts[8]
            if name in (".", ".."):
                continue
            isdir = ln.startswith("d")
            if ln.startswith("l"):
                name = name.split(" -> ")[0]
            try:
                size = int(parts[4])
            except ValueError:
                size = 0
            out.append((name, isdir, size, 0))
        return out

    def walk(self, stop=None):
        with self._lock:
            f = self._conn()
            stack = [self.root]
            while stack:
                if stop and stop():
                    return
                d = stack.pop()
                try:
                    ents = self._listdir(f, d)
                except Exception:
                    continue
                for name, isdir, size, mt in ents:
                    p = posixpath.join(d, name)
                    if isdir:
                        if name not in SKIP_DIRS and not name.startswith("."):
                            stack.append(p)
                    elif is_video(name):
                        if not mt:
                            try:
                                r = f.sendcmd("MDTM " + p)
                                mt = int(time.mktime(time.strptime(r.split()[-1][:14], "%Y%m%d%H%M%S")))
                            except Exception:
                                mt = 0
                        yield {"src": self.id, "path": p, "name": name, "dir": d, "size": size, "mtime": mt}

    def input_url(self, item):
        return proxy().url_for(self, item["path"], item["size"])

    def read_range(self, item, offset, length):
        f = self.connect()
        try:
            f.voidcmd("TYPE I")
            s = f.transfercmd("RETR " + item["path"], rest=offset or None)
            buf = bytearray()
            while len(buf) < length:
                c = s.recv(min(65536, length - len(buf)))
                if not c:
                    break
                buf += c
            s.close()
            return bytes(buf)
        finally:
            try:
                f.close()
            except Exception:
                pass

    def open_stream(self, item):
        return _FtpReader(self, item["path"])

    def list_dir(self, d):
        with self._lock:
            try:
                return [(n, isd) for n, isd, _, _ in self._listdir(self._conn(), d)]
            except Exception:
                return []

    def join(self, *a):
        return posixpath.join(*a)

    def dirname(self, p):
        return posixpath.dirname(p)

    def basename(self, p):
        return posixpath.basename(p)

    def exists(self, p):
        d, n = posixpath.split(p)
        return any(x[0] == n for x in self.list_dir(d))

    def default_dest(self, folder_name):
        return "/" + folder_name

    def rel_for_dest(self, p):
        return p.lstrip("/")

    def _mkdirs(self, f, d):
        parts = [x for x in d.split("/") if x]
        cur = ""
        for x in parts:
            cur += "/" + x
            try:
                f.mkd(cur)
            except ftplib.error_perm:
                pass

    def move(self, src, dst):
        with self._lock:
            f = self._conn()
            self._mkdirs(f, posixpath.dirname(dst))
            names = {n for n, _, _, _ in self._listdir(f, posixpath.dirname(dst))}
            dst = unique_path(dst, lambda q: posixpath.basename(q) in names)
            f.rename(src, dst)
            return dst

    def rmdir_if_empty(self, d):
        with self._lock:
            try:
                self._conn().rmd(d)
            except Exception:
                pass

    def close(self):
        try:
            if self._main:
                self._main.quit()
        except Exception:
            pass
        self._main = None


class _FtpReader:
    """처음부터 순서대로 읽는 FTP 스트림 (해시 계산용)"""

    def __init__(self, src, path):
        self.f = src.connect()
        self.f.voidcmd("TYPE I")
        self.s = self.f.transfercmd("RETR " + path)

    def read(self, n):
        return self.s.recv(n) if n else b""

    def close(self):
        for x in (self.s, self.f):
            try:
                x.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# ───────────────── FTP → FFmpeg 중계 (구간 읽기 지원) ─────────────────
class _Proxy:
    def __init__(self):
        self.map = {}
        self.lock = threading.Lock()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_HEAD(self):
                self._serve(head=True)

            def do_GET(self):
                self._serve(head=False)

            def _serve(self, head):
                tok = self.path.strip("/").split("/")[0]
                ent = outer.map.get(tok)
                if not ent:
                    self.send_error(404)
                    return
                src, path, size = ent
                start, end = 0, size - 1
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    a, _, b = rng[6:].split(",")[0].partition("-")
                    try:
                        start = int(a) if a else max(0, size - int(b))
                        end = int(b) if (a and b) else size - 1
                    except ValueError:
                        start, end = 0, size - 1
                if start >= size > 0:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                n = end - start + 1
                self.send_response(206 if rng else 200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(max(0, n)))
                if rng:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if head or n <= 0:
                    return
                f = s = None
                try:
                    f = src.connect()
                    f.voidcmd("TYPE I")
                    s = f.transfercmd("RETR " + path, rest=start or None)
                    left = n
                    while left > 0:
                        c = s.recv(min(262144, left))
                        if not c:
                            break
                        self.wfile.write(c)
                        left -= len(c)
                except Exception:
                    pass
                finally:
                    for x in (s, f):
                        try:
                            x and x.close()
                        except Exception:
                            pass

        class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self.srv = S(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def url_for(self, src, path, size):
        tok = secrets.token_hex(8)
        with self.lock:
            self.map[tok] = (src, path, size)
        ext = os.path.splitext(path)[1]
        return f"http://127.0.0.1:{self.port}/{tok}/v{urllib.parse.quote(ext)}"


_proxy = None


def proxy():
    global _proxy
    if _proxy is None:
        _proxy = _Proxy()
    return _proxy


def from_dict(d):
    if d.get("type") == "ftp":
        return FtpSource(d["host"], d.get("port", 21), d.get("user", ""), d.get("password", ""), d.get("root", "/"),
                         d.get("encoding", "utf-8"), d.get("tls", False))
    return LocalSource(d["path"])


def to_dict(src):
    if src.kind == "ftp":
        x = src.to_dict()
        x["type"] = "ftp"
        return x
    return {"type": "local", "path": src.root}
