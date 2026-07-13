"""
入口 — python -m clibord_v11
"""

import sys
import os
import traceback

# 确保能找到包（当从 clibord/ 目录运行时）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _global_excepthook(exc_type, exc_value, exc_tb):
    """捕获任何顶层未处理的异常，写入崩溃日志"""
    try:
        exe_dir = os.path.dirname(sys.executable)
    except Exception:
        exe_dir = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(exe_dir, "LANClipSync_crash.log")
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n=== GLOBAL CRASH at {__import__('time').strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
            f.write("=" * 40 + "\n")
    except Exception:
        pass
    # 原始处理
    sys.__excepthook__(exc_type, exc_value, exc_tb)


sys.excepthook = _global_excepthook

# 线程异常也需要捕获
import threading

_original_threading_excepthook = threading.excepthook


def _threading_excepthook(args):
    """线程内未捕获异常也写入崩溃日志"""
    try:
        exe_dir = os.path.dirname(sys.executable)
    except Exception:
        exe_dir = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(exe_dir, "LANClipSync_crash.log")
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n=== THREAD CRASH at {__import__('time').strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback, file=f)
            f.write("=" * 40 + "\n")
    except Exception:
        pass
    _original_threading_excepthook(args)


threading.excepthook = _threading_excepthook

import tkinter as tk
from .gui import GUI


def main():
    root = tk.Tk()
    app = GUI(root)
    root.geometry("580x640")
    app.run()


if __name__ == "__main__":
    main()
