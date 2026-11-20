"""FFmpeg 위치 찾기와 실행 도우미"""
import os
import shutil
import subprocess
import sys

NOWIN = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
EXE = ".exe" if sys.platform == "win32" else ""


def app_dir():
    if "__compiled__" in globals() or getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.argv[0]))
    return os.path.dirname(os.path.abspath(__file__))


def _find(name):
    base = app_dir()
    for d in (os.path.join(base, "ffmpeg", "bin"), os.path.join(base, "ffmpeg"), base):
        p = os.path.join(d, name + EXE)
        if os.path.isfile(p):
            return p
    return shutil.which(name)


FFMPEG = _find("ffmpeg")
FFPROBE = _find("ffprobe")


def available():
    return bool(FFMPEG and FFPROBE)


def run(args, timeout=None):
    """짧은 명령 실행 → (returncode, stdout bytes, stderr text)"""
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                       creationflags=NOWIN, timeout=timeout)
    return p.returncode, p.stdout, p.stderr.decode("utf-8", "replace")


def popen(args):
    return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            creationflags=NOWIN, bufsize=0)


def open_file(path):
    """기본 프로그램으로 열기"""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def show_in_folder(path):
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            open_file(os.path.dirname(path))
    except Exception:
        pass
