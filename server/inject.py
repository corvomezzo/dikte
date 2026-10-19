"""Metni o an odakta olan pencereye yazar (Windows SendInput).

"type" modu her UTF-16 birimini KEYEVENTF_UNICODE ile gönderir; klavye düzeninden
bağımsızdır, Türkçe karakterler (çğıöşü) bozulmaz. "paste" modu panoyu kullanır;
uzun metinlerde ve tuş vuruşunu yavaş işleyen uygulamalarda daha hızlıdır.

Not: Windows UIPI nedeniyle yönetici olarak çalışan bir pencereye, yönetici olmayan
bir süreçten yazılamaz.
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D
VK_BACK = 0x08
VK_CONTROL = 0x11
VK_V = 0x56
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

SPECIAL_KEYS = {"enter": VK_RETURN, "backspace": VK_BACK, "space": 0x20, "tab": 0x09}

ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _vk(vk: int, up: bool = False) -> INPUT:
    return INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, 0)))


def _unicode(unit: int, up: bool = False) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(0, unit, flags, 0, 0)))


def build_inputs(text: str) -> list[INPUT]:
    """Metni tuş olaylarına çevirir. \\n -> Enter; BMP dışı karakterler surrogate çifti olur."""
    events: list[INPUT] = []
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            events += [_vk(VK_RETURN), _vk(VK_RETURN, up=True)]
            continue
        data = ch.encode("utf-16-le")
        for i in range(0, len(data), 2):
            unit = int.from_bytes(data[i : i + 2], "little")
            events += [_unicode(unit), _unicode(unit, up=True)]
    return events


if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    _user32.SendInput.restype = wintypes.UINT
    _user32.OpenClipboard.argtypes = (wintypes.HWND,)
    _user32.GetClipboardData.restype = wintypes.HANDLE
    _user32.GetClipboardData.argtypes = (wintypes.UINT,)
    _user32.SetClipboardData.restype = wintypes.HANDLE
    _user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
    _kernel32.GlobalLock.restype = ctypes.c_void_p
    _kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
    _kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)


def _send(events: list[INPUT]) -> None:
    if not events:
        return
    arr = (INPUT * len(events))(*events)
    sent = _user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
    if sent != len(events):
        raise OSError(f"SendInput {sent}/{len(events)} olay gönderdi (hata {ctypes.get_last_error()})")


def _open_clipboard() -> None:
    for _ in range(10):
        if _user32.OpenClipboard(None):
            return
        time.sleep(0.02)
    raise OSError("Pano açılamadı")


def _get_clipboard_text() -> str | None:
    _open_clipboard()
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = _kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(ptr)
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def _set_clipboard_text(text: str) -> None:
    buf = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(buf)
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    ptr = _kernel32.GlobalLock(handle)
    ctypes.memmove(ptr, buf, size)
    _kernel32.GlobalUnlock(handle)
    _open_clipboard()
    try:
        _user32.EmptyClipboard()
        _user32.SetClipboardData(CF_UNICODETEXT, handle)
    finally:
        _user32.CloseClipboard()


def type_text(text: str) -> None:
    _send(build_inputs(text))


def paste_text(text: str) -> None:
    previous = _get_clipboard_text()
    _set_clipboard_text(text)
    _send([_vk(VK_CONTROL), _vk(VK_V), _vk(VK_V, up=True), _vk(VK_CONTROL, up=True)])
    time.sleep(0.5)  # hedef uygulama yapıştırmayı işleyip panoyu okusun (yavaş uygulamalarda kısa süre yetmiyor)
    if previous is not None:
        _set_clipboard_text(previous)


def press(key: str) -> None:
    vk = SPECIAL_KEYS[key]
    _send([_vk(vk), _vk(vk, up=True)])


def backspace(n: int) -> None:
    _send([e for _ in range(n) for e in (_vk(VK_BACK), _vk(VK_BACK, up=True))])


def foreground_window() -> int:
    return _user32.GetForegroundWindow() or 0


class TypedHistory:
    """Bu sunucunun yazdığı parçaları (pencere, karakter sayısı) olarak tutar; "geri al"
    bunları Backspace ile siler. Odaktaki pencere değiştiyse silmeyi reddeder, çünkü
    Backspace o zaman başka bir yerdeki metni silerdi.

    Karakter sayısı kod noktası sayısıdır: çoğu uygulamada bir Backspace bir emojiyi
    (surrogate çifti) ve bir satır sonunu tek seferde siler.
    """

    def __init__(self) -> None:
        self.chunks: list[tuple[int, int, int]] = []  # (hwnd, karakter sayısı, parça id)
        self._next_id = 1

    def push(self, hwnd: int, text: str) -> int:
        n = len(text.replace("\r\n", "\n"))
        if not n:
            return 0
        cid, self._next_id = self._next_id, self._next_id + 1
        self.chunks.append((hwnd, n, cid))
        return cid

    def on_backspace(self) -> None:
        if self.chunks:
            hwnd, n, cid = self.chunks[-1]
            self.chunks[-1:] = [(hwnd, n - 1, cid)] if n > 1 else []

    def take_if_last(self, hwnd: int, cid: int) -> int | None:
        """Parça hâlâ bu penceredeki en son parçaysa, uzunluğunu döner ve geçmişten düşer."""
        if self.chunks and self.chunks[-1][0] == hwnd and self.chunks[-1][2] == cid:
            return self.chunks.pop()[1]
        return None

    def take(self, hwnd: int, everything: bool) -> int:
        """Silinecek karakter sayısını döner ve o parçaları geçmişten düşer.
        Yalnızca verilen pencereye ait, en sondan başlayan kesintisiz parçaları alır."""
        total = 0
        while self.chunks and self.chunks[-1][0] == hwnd:
            total += self.chunks.pop()[1]
            if not everything:
                break
        return total


def inject(text: str, mode: str = "type") -> None:
    if mode == "paste":
        paste_text(text)
    else:
        type_text(text)
