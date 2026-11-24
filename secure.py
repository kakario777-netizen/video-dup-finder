"""사용자가 입력한 접속 정보(FTP 비밀번호 등) 보호
윈도우 기본 암호화(DPAPI)로 잠가서 저장 → 이 PC의 이 윈도우 계정에서만 풀 수 있음.
설정 파일이나 결과 파일이 다른 곳으로 복사돼도 비밀번호는 읽을 수 없음."""
import base64
import sys

_PREFIX = "dpapi:"


def _dpapi(data, protect):
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    crypt = ctypes.windll.crypt32
    fn = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    # 마지막 인자 0x1 = 화면에 묻지 않음(UI 없음)
    if not fn(ctypes.byref(inp), None, None, None, None, 0x1, ctypes.byref(out)):
        raise OSError("DPAPI 실패")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(text):
    if not text:
        return ""
    raw = text.encode("utf-8")
    if sys.platform == "win32":
        try:
            return _PREFIX + base64.b64encode(_dpapi(raw, True)).decode()
        except Exception:
            pass
    return "b64:" + base64.b64encode(raw).decode()  # 윈도우가 아닌 곳(점검용)


def unprotect(value):
    if not value:
        return ""
    try:
        if value.startswith(_PREFIX):
            return _dpapi(base64.b64decode(value[len(_PREFIX):]), False).decode("utf-8")
        if value.startswith("b64:"):  # 예전 버전에서 저장한 값
            return base64.b64decode(value[4:]).decode("utf-8")
    except Exception:
        return ""  # 다른 PC에서 만든 값 등 → 비밀번호 다시 입력 필요
    return value
