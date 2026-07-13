"""
剪贴板操作封装 + AddClipboardFormatListener 事件驱动监听

职责：
  - 剪贴板读写（文本/图片/文件）
  - AddClipboardFormatListener 注册 / 取消
  - 剪贴板内容 hash 跟踪（防重复发送）
  - 文本长度校验
"""

import ctypes
import ctypes.wintypes as wintypes
import hashlib
import os
import threading
import time
from . import config

# ── Windows 剪贴板格式 ──
CF_TEXT = 1
CF_UNICODETEXT = 13
CF_DIB = 8
CF_HDROP = 15
CF_BITMAP = 2

# ── Win32 内存常量 ──
GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040
GHND = GMEM_MOVEABLE | GMEM_ZEROINIT

# ── 窗口消息 ──
WM_CLIPBOARDUPDATE = 0x031D
GWLP_WNDPROC = -4

# ── Win32 DLL ──
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ── 类型标注 (argtypes / restype) ──
# 注意：64 位 Windows 下所有指针尺寸参数必须用 c_void_p / c_size_t / c_longlong，
# 不能用 c_int / c_long（32-bit），否则指针截断 → segfault

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.AddClipboardFormatListener.argtypes = [wintypes.HWND]
user32.AddClipboardFormatListener.restype = wintypes.BOOL
user32.RemoveClipboardFormatListener.argtypes = [wintypes.HWND]
user32.RemoveClipboardFormatListener.restype = wintypes.BOOL

# Window 过程操作 — 缺失 argtypes 会导致 64 位指针截断 segfault！
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_void_p
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
user32.SetWindowLongPtrW.restype = ctypes.c_void_p
user32.CallWindowProcW.argtypes = [
    ctypes.c_void_p,      # WNDPROC（函数指针）
    wintypes.HWND,        # hWnd
    wintypes.UINT,         # Msg
    wintypes.WPARAM,       # wParam (UINT_PTR, 64-bit on x64)
    wintypes.LPARAM,       # lParam (LONG_PTR, 64-bit on x64)
]
user32.CallWindowProcW.restype = ctypes.c_longlong  # LRESULT (LONG_PTR, 64-bit on x64)

kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class DROPFILES(ctypes.Structure):
    _fields_ = [
        ("pFiles", wintypes.DWORD),
        ("pt", POINT),
        ("fNC", wintypes.BOOL),
        ("fWide", wintypes.BOOL),
    ]


# Window procedure 类型 — LRESULT 在 x64 下是 64 位的！
# 如果用 c_long (32-bit)，user32 调用回调后栈/寄存器损坏 → segfault
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong,  # LRESULT = LONG_PTR (64-bit)
                              wintypes.HWND,
                              wintypes.UINT,
                              wintypes.WPARAM,
                              wintypes.LPARAM)


# ── 内部工具函数 ──

def _open_clipboard(retries: int = 5, delay: float = 0.05) -> bool:
    """打开剪贴板（支持重试）"""
    for _ in range(retries):
        if user32.OpenClipboard(0):
            return True
        time.sleep(delay)
    return False


def _close_clipboard():
    """关闭剪贴板"""
    try:
        user32.CloseClipboard()
    except Exception:
        pass


def _clipboard_hash(clip_type: str, data):
    """计算剪贴板内容的 hash"""
    if data is None:
        return None
    if isinstance(data, str):
        raw = (clip_type + data).encode("utf-8")
    elif isinstance(data, bytes):
        if len(data) <= 8192:
            raw = clip_type.encode("utf-8") + data
        else:
            # 三段采样
            mid = len(data) // 2
            sample = data[:4096] + data[mid - 2048:mid + 2048] + data[-4096:]
            raw = clip_type.encode("utf-8") + sample
    elif isinstance(data, list):
        raw = (clip_type + "|".join(sorted(data))).encode("utf-8")
    else:
        return None
    return hashlib.md5(raw).hexdigest()


def check_text_size(text):
    """检查文本长度是否在允许范围内。
    返回 (允许传输, 警告/拒绝消息)
    """
    size = len(text.encode("utf-8"))
    if size > config.TEXT_SIZE_LIMIT:
        return False, f"❌ 文本超过 {config.TEXT_SIZE_LIMIT // 1048576}MB 上限，已跳过"
    if size > config.TEXT_SIZE_WARN:
        mb = size / 1048576
        return True, f"⚠️ 文本较大 ({mb:.1f} MB)"
    return True, ""


# ── 公开读写函数 ──

def get_text():
    """读取剪贴板 Unicode 文本"""
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return None
    if not _open_clipboard():
        return None
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        h_mem = user32.GetClipboardData(CF_UNICODETEXT)
        if not h_mem:
            return None
        ptr = kernel32.GlobalLock(h_mem)
        if not ptr:
            return None
        try:
            text = ctypes.wstring_at(ptr)
            return text if text else None
        finally:
            kernel32.GlobalUnlock(h_mem)
    finally:
        _close_clipboard()


def set_text(text: str) -> bool:
    """写入 Unicode 文本到剪贴板"""
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        data = (text + "\0").encode("utf-16-le")
        h_mem = kernel32.GlobalAlloc(GHND, len(data))
        if not h_mem:
            return False
        ptr = kernel32.GlobalLock(h_mem)
        if not ptr:
            kernel32.GlobalFree(h_mem)
            return False
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(h_mem)
        user32.SetClipboardData(CF_UNICODETEXT, h_mem)
        return True
    finally:
        _close_clipboard()


def get_image():
    """读取剪贴板 DIB 图像"""
    if not user32.IsClipboardFormatAvailable(CF_DIB) and not user32.IsClipboardFormatAvailable(CF_BITMAP):
        return None
    if not _open_clipboard():
        return None
    try:
        if not user32.IsClipboardFormatAvailable(CF_DIB):
            return None
        h_mem = user32.GetClipboardData(CF_DIB)
        if not h_mem:
            return None
        ptr = kernel32.GlobalLock(h_mem)
        if not ptr:
            return None
        try:
            size = kernel32.GlobalSize(h_mem)
            if size <= 0:
                return None
            return ctypes.string_at(ptr, size)
        finally:
            kernel32.GlobalUnlock(h_mem)
    finally:
        _close_clipboard()


def set_image(dib_data: bytes) -> bool:
    """写入 DIB 图像到剪贴板"""
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        h_mem = kernel32.GlobalAlloc(GHND, len(dib_data))
        if not h_mem:
            return False
        ptr = kernel32.GlobalLock(h_mem)
        if not ptr:
            kernel32.GlobalFree(h_mem)
            return False
        ctypes.memmove(ptr, dib_data, len(dib_data))
        kernel32.GlobalUnlock(h_mem)
        user32.SetClipboardData(CF_DIB, h_mem)
        return True
    finally:
        _close_clipboard()


def get_files():
    """读取剪贴板文件路径列表 (CF_HDROP)"""
    if not user32.IsClipboardFormatAvailable(CF_HDROP):
        return None
    if not _open_clipboard():
        return None
    try:
        h_mem = user32.GetClipboardData(CF_HDROP)
        if not h_mem:
            return None
        ptr = kernel32.GlobalLock(h_mem)
        if not ptr:
            return None
        try:
            df = ctypes.cast(ptr, ctypes.POINTER(DROPFILES)).contents
            offset = df.pFiles
            is_wide = df.fWide
            file_list = []
            if is_wide:
                pos = ptr + offset
                while True:
                    s = ctypes.wstring_at(pos)
                    if not s:
                        break
                    file_list.append(s)
                    pos += (len(s) + 1) * 2
            else:
                pos = ptr + offset
                while True:
                    s = ctypes.string_at(pos).decode("gbk", errors="replace")
                    if not s:
                        break
                    file_list.append(s)
                    pos += len(s) + 1
            return file_list if file_list else None
        finally:
            kernel32.GlobalUnlock(h_mem)
    finally:
        _close_clipboard()


def set_files(file_paths):
    """写入文件路径列表 (CF_HDROP) 到剪贴板"""
    if not file_paths:
        return False
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        paths_wide = [os.path.abspath(p) + "\0" for p in file_paths]
        paths_data = "".join(paths_wide).encode("utf-16-le") + b"\0\0"
        dropfiles_size = ctypes.sizeof(DROPFILES)
        total_size = dropfiles_size + len(paths_data)
        h_mem = kernel32.GlobalAlloc(GHND, total_size)
        if not h_mem:
            return False
        ptr = kernel32.GlobalLock(h_mem)
        if not ptr:
            kernel32.GlobalFree(h_mem)
            return False
        try:
            ctypes.memset(ptr, 0, total_size)
            df = ctypes.cast(ptr, ctypes.POINTER(DROPFILES)).contents
            df.pFiles = dropfiles_size
            df.fWide = True
            ctypes.memmove(ptr + dropfiles_size, paths_data, len(paths_data))
        finally:
            kernel32.GlobalUnlock(h_mem)
        user32.SetClipboardData(CF_HDROP, h_mem)
        return True
    finally:
        _close_clipboard()


# ── ClipboardManager ──

class ClipboardManager:
    """
    剪贴板管理器

    职责：
    - 低层读写：get_text / set_text / ...
    - 事件驱动监听：AddClipboardFormatListener（带回退轮询）
    - hash 跟踪（按类型分别记录）
    - 接收数据后抑制检测
    """

    def __init__(self):
        self._hwnd = None
        self._orig_wndproc = None
        self._wndproc_cb = None  # 保持强引用防止 GC
        self._on_change = None
        self._listener_ok = False

        self._suppress_until = 0.0
        self._last_hash = {}
        self._hash_lock = threading.Lock()

    # ── 公开属性 ──

    @property
    def listener_active(self) -> bool:
        return self._listener_ok

    # ── hash 跟踪 ──

    def update_hash(self, clip_type: str, data) -> bool:
        """
        检查并更新 hash。
        返回 True 表示内容有变化（需要处理），False 表示与上次相同。
        """
        h = _clipboard_hash(clip_type, data)
        if h is None:
            return False
        with self._hash_lock:
            key = f"clip_{clip_type}"
            if self._last_hash.get(key) == h:
                return False
            self._last_hash[key] = h
            return True

    def set_suppress(self, seconds: float) -> None:
        """收到远端数据后，抑制本机检测"""
        self._suppress_until = time.time() + seconds

    @property
    def is_suppressed(self) -> bool:
        return time.time() < self._suppress_until

    # ── 监听器 ──

    def start_listener(self, hwnd: int,
                       on_text_changed,
                       on_image_changed,
                       on_files_changed) -> bool:
        """
        注册 AddClipboardFormatListener。

        参数：
          hwnd            — tkinter 窗口的 HWND (winfo_id())
          on_text_changed — 检测到文本变化时的回调 (text: str)
          on_image_changed— 检测到图片变化时的回调 (data: bytes)
          on_files_changed— 检测到文件变化时的回调 (paths: list)

        返回是否注册成功。不成功时可退回到轮询模式。
        """
        self._on_text = on_text_changed
        self._on_image = on_image_changed
        self._on_files = on_files_changed

        self._hwnd = hwnd

        # 关键：先获取原始窗口过程，再注册监听器、再替换
        # 顺序错了会导致替换后 msgs 到达时 self._orig_wndproc 还是 None
        # 从而 WNDPROC 返回 0 破坏 tkinter 消息循环
        WndProc = WNDPROC(self._wndproc)
        self._wndproc_cb = WndProc  # 保持强引用防止 GC
        self._orig_wndproc = user32.GetWindowLongPtrW(hwnd, GWLP_WNDPROC)

        success = user32.AddClipboardFormatListener(hwnd)
        if not success:
            self._orig_wndproc = None
            self._wndproc_cb = None
            return False

        user32.SetWindowLongPtrW(
            hwnd, GWLP_WNDPROC, self._wndproc_cb
        )
        self._listener_ok = True
        return True

    def stop_listener(self) -> None:
        """取消注册并恢复原始窗口过程"""
        self._listener_ok = False
        if self._hwnd and self._orig_wndproc:
            try:
                user32.RemoveClipboardFormatListener(self._hwnd)
                user32.SetWindowLongPtrW(
                    self._hwnd, GWLP_WNDPROC, self._orig_wndproc
                )
            except Exception:
                pass
        self._hwnd = None
        self._orig_wndproc = None
        self._wndproc_cb = None

    # ── WNDPROC ──

    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        """替换的窗口过程"""
        try:
            if msg == WM_CLIPBOARDUPDATE:
                self._on_clipboard_notify()
        except Exception:
            import traceback
            traceback.print_exc()
        # 调用原始窗口过程
        try:
            if self._orig_wndproc:
                return user32.CallWindowProcW(self._orig_wndproc,
                                              hwnd, msg, wparam, lparam)
        except Exception:
            import traceback
            traceback.print_exc()
        return 0

    def _on_clipboard_notify(self) -> None:
        """收到 WM_CLIPBOARDUPDATE（主线程消息循环中调用）"""
        if self.is_suppressed:
            return

        # 在后台线程中一次性读完所有格式，然后各自起线程发送
        def _detect():
            if self.is_suppressed:
                return

            files = get_files()
            img = get_image()
            text = get_text()

            if files and self.update_hash("files", files):
                threading.Thread(target=self._on_files, args=(files,),
                                 daemon=True).start()
            if img and self.update_hash("img", img):
                threading.Thread(target=self._on_image, args=(img,),
                                 daemon=True).start()
            if text and self.update_hash("text", text):
                threading.Thread(target=self._on_text, args=(text,),
                                 daemon=True).start()

        threading.Thread(target=_detect, daemon=True).start()

    # ── 回退轮询（监听器不可用时的备选方案）──

    def poll_once(self) -> None:
        """单次轮询检测（用于回退模式）"""
        if self.is_suppressed:
            return

        files = get_files()
        if files and self.update_hash("files", files):
            if self._on_files:
                self._on_files(files)
            return  # 本轮已处理，下一轮再检查其他类型

        img = get_image()
        if img and self.update_hash("img", img):
            if self._on_image:
                self._on_image(img)
            return

        text = get_text()
        if text and self.update_hash("text", text):
            if self._on_text:
                self._on_text(text)
