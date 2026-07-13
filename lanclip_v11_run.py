#!/usr/bin/env python3
"""
LANClipSync v11 入口 — 供 PyInstaller 打包用
"""
import sys
import os
import traceback
import threading

# ── 全局异常钩子 ──
_exe_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))


def _write_crash_log(title: str, exc_type, exc_value, exc_tb):
    try:
        log_path = os.path.join(_exe_dir, "LANClipSync_crash.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n=== {title} at {__import__('time').strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
            f.write("=" * 40 + "\n")
    except Exception:
        pass


def _global_hook(exc_type, exc_value, exc_tb):
    _write_crash_log("GLOBAL CRASH", exc_type, exc_value, exc_tb)
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def _thread_hook(args):
    _write_crash_log("THREAD CRASH", args.exc_type, args.exc_value, args.exc_traceback)


sys.excepthook = _global_hook
_orig_thread_hook = threading.excepthook
threading.excepthook = _thread_hook

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tkinter as tk
from clibord_v11.gui import GUI


def _clean_mei_temp():
    """清理残留的 PyInstaller _MEI* 临时目录（跳过当前进程正在使用的）"""
    import shutil
    import tempfile

    # 当前进程自己的 _MEI 路径，跳过它
    self_mei = getattr(sys, '_MEIPASS', None)

    tmp = tempfile.gettempdir()
    for name in os.listdir(tmp):
        if not name.startswith("_MEI"):
            continue
        path = os.path.join(tmp, name)
        if not os.path.isdir(path):
            continue
        if self_mei and os.path.normpath(path) == os.path.normpath(self_mei):
            continue  # 当前进程自己的，不动
        try:
            shutil.rmtree(path, ignore_errors=False)
        except (PermissionError, OSError):
            pass  # 其他正在运行中的实例，跳过


def main():
    _clean_mei_temp()
    root = tk.Tk()
    app = GUI(root)
    app.run()


if __name__ == "__main__":
    main()
