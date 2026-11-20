"""분석 결과 저장소(캐시) — 같은 파일(경로·크기·수정시각 동일)은 다음 검사 때 다시 읽지 않음"""
import io
import json
import os
import sqlite3
import threading

import numpy as np


def data_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
    d = os.path.join(base, "VideoDupFinder")
    os.makedirs(d, exist_ok=True)
    return d


def thumb_dir():
    d = os.path.join(data_dir(), "thumbs")
    os.makedirs(d, exist_ok=True)
    return d


class Cache:
    def __init__(self, path=None):
        self.path = path or os.path.join(data_dir(), "cache.db")
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS files(
            key TEXT PRIMARY KEY, info TEXT, broken TEXT, vfp BLOB, afp BLOB, qhash TEXT, fhash TEXT,
            thumb TEXT, extra TEXT)""")
        self.db.commit()

    @staticmethod
    def key(item, ver, mode):
        return f"{ver}|{mode}|{item['src']}|{item['path']}|{item['size']}|{item['mtime']}"

    def get(self, k):
        with self.lock:
            r = self.db.execute("SELECT info,broken,vfp,afp,qhash,fhash,thumb,extra FROM files WHERE key=?",
                                (k,)).fetchone()
        if not r:
            return None
        return {"info": json.loads(r[0]) if r[0] else None, "broken": json.loads(r[1]) if r[1] else None,
                "video": unpack_v(r[2]) if r[2] else None, "audio": unpack_a(r[3]) if r[3] else None,
                "qhash": r[4], "fhash": r[5], "thumb": r[6], "extra": json.loads(r[7]) if r[7] else {}}

    def put(self, k, info=None, broken=None, video=None, audio=None, qhash=None, fhash=None, thumb=None, extra=None):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)",
                            (k, json.dumps(info) if info else None, json.dumps(broken) if broken else None,
                             pack_v(video) if video is not None else None,
                             pack_a(audio) if audio is not None else None, qhash, fhash, thumb,
                             json.dumps(extra) if extra else None))
            self.db.commit()

    def set_field(self, k, field, value):
        with self.lock:
            self.db.execute(f"UPDATE files SET {field}=? WHERE key=?", (value, k))
            self.db.commit()

    def clear(self):
        with self.lock:
            self.db.execute("DELETE FROM files")
            self.db.commit()
            self.db.execute("VACUUM")

    def close(self):
        try:
            self.db.close()
        except Exception:
            pass


def pack_v(v):
    b = io.BytesIO()
    np.savez_compressed(b, t=v["t"], h=v["h"], c=v["c"], f=v["f"], g=v["g"], ok=v["ok"], box=np.asarray(v["box"]))
    return b.getvalue()


def unpack_v(blob):
    z = np.load(io.BytesIO(blob))
    return {"t": z["t"], "h": z["h"], "c": z["c"], "f": z["f"], "g": z["g"], "ok": z["ok"], "box": list(z["box"])}


def pack_a(a):
    b = io.BytesIO()
    np.savez_compressed(b, fp=a[0], ok=a[1])
    return b.getvalue()


def unpack_a(blob):
    z = np.load(io.BytesIO(blob))
    return z["fp"], z["ok"]
