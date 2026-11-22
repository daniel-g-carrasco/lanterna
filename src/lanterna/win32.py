"""What the app needs from Windows itself, through ctypes: the title bars
in the content's colour, the executable's icon on every window, the files
that would run when opened. Imported only on Windows.

Every function sets the argtypes and restype of what it calls: without
them ctypes passes and returns C ints, and a HANDLE (64 bit) comes back
truncated to 32, a mistake that shows only now and then.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


# -- the title bar -----------------------------------------------------------------

def dark_title_bars(dark: bool) -> None:
    """The system title bars of this process's windows, dark or light, as
    the content is (Windows 10 1809 and later; ignored elsewhere)."""
    user32, dwmapi = ctypes.windll.user32, ctypes.windll.dwmapi
    pid, value = os.getpid(), ctypes.c_int(1 if dark else 0)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            for attribute in (20, 19):      # DWMWA_USE_IMMERSIVE_DARK_MODE, then its 1809 number
                if dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
        return True

    user32.EnumWindows(visit, 0)


SM_CXICON, SM_CXSMICON = 11, 49
ICON_SMALL, ICON_BIG, WM_SETICON = 0, 1, 0x0080
_icons: dict[tuple[str, int], int] = {}     # kept for the life of the process: a window does not copy its icon


def _exe_icon(exe: str, size: int) -> int:
    """The exe's own icon at this size (its .ico holds 16, 20, 24 ... 256,
    the small ones drawn for small sizes); 0 when there is none."""
    if (exe, size) not in _icons:
        handles, ids = (wintypes.HICON * 1)(), (wintypes.UINT * 1)()
        got = ctypes.windll.user32.PrivateExtractIconsW(exe, 0, size, size, handles, ids, 1, 0)
        _icons[(exe, size)] = (handles[0] or 0) if got == 1 else 0
    return _icons[(exe, size)]


def window_icons(exe: str) -> None:
    """This process's windows with the exe's icon, at the sizes Windows draws
    for each window's DPI (the title bar's small one, Alt+Tab's big one).
    GTK gives Windows a 48-pixel icon, which the title bar shrinks badly to
    16."""
    user32 = ctypes.windll.user32
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, wintypes.UINT]
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = wintypes.LPARAM
    pid = os.getpid()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            dpi = user32.GetDpiForWindow(hwnd) or 96
            for kind, metric in ((ICON_SMALL, SM_CXSMICON), (ICON_BIG, SM_CXICON)):
                icon = _exe_icon(exe, user32.GetSystemMetricsForDpi(metric, dpi))
                if icon:
                    user32.SendMessageW(hwnd, WM_SETICON, kind, icon)
        return True

    user32.EnumWindows(visit, 0)


# -- a file that runs when it is opened ---------------------------------------------

# What Windows runs, rather than shows, when such a file is opened: programs,
# scripts, installers, shortcuts, settings files. AssocIsDangerous gives the
# system's own list, which policies can change; these always count.
RUNS_WHEN_OPENED = frozenset((
    ".exe", ".com", ".scr", ".pif", ".cpl", ".msc", ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".vbs", ".vbe",
    ".js", ".jse", ".wsf", ".wsh", ".hta", ".msi", ".msp", ".mst", ".appx", ".msix", ".lnk", ".url", ".scf",
    ".reg", ".inf", ".jar", ".application", ".appref-ms", ".gadget", ".chm", ".library-ms",
    ".settingcontent-ms", ".diagcab", ".xbap", ".website",
))


def runs_when_opened(path: str) -> bool:
    """Opening this file would run it (ShellExecute): a program, a script, a
    shortcut. "Fattura.pdf.exe" counts by its last extension, as Windows
    reads it. An app that opens files it did not make (downloads, files
    shared by others) shows such a file in its folder instead."""
    ext = os.path.splitext(path)[1].lower()
    if not ext:
        return False
    if ext in RUNS_WHEN_OPENED:
        return True
    try:
        shlwapi = ctypes.WinDLL("shlwapi")
        shlwapi.AssocIsDangerous.argtypes = [wintypes.LPCWSTR]
        shlwapi.AssocIsDangerous.restype = wintypes.BOOL
        return bool(shlwapi.AssocIsDangerous(ext))
    except (AttributeError, OSError):
        return False
