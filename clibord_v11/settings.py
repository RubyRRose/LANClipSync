"""
配置持久化 — 将用户设置保存到 JSON 文件，下次启动自动加载
"""

import json
import os
import sys
import threading

SETTINGS_FILE = "LANClipSync_settings.json"

DEFAULT_SETTINGS = {
    "device_name": "",
    "cache_dir": "",
    "filter_text": True,
    "filter_image": True,
    "filter_files": True,
    "window_x": None,
    "window_y": None,
    "window_width": 580,
    "window_height": 720,
    "privacy_mode": False,
}

_settings = None
_lock = threading.Lock()


def _get_path():
    try:
        exe_dir = os.path.dirname(sys.executable)
    except Exception:
        exe_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(exe_dir, SETTINGS_FILE)


def load():
    global _settings
    with _lock:
        path = _get_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            _settings = {**DEFAULT_SETTINGS, **data}
        except Exception as e:
            print(f"[settings] 加载设置失败: {e}", file=sys.stderr)
            _settings = dict(DEFAULT_SETTINGS)
        return dict(_settings)


def save():
    global _settings
    with _lock:
        if _settings is None:
            _settings = dict(DEFAULT_SETTINGS)
        path = _get_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(_settings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[settings] 保存设置失败: {e}", file=sys.stderr)


def get(key, default=None):
    global _settings
    if _settings is None:
        load()
    return _settings.get(key, default)


def put(key, value):
    global _settings
    if _settings is None:
        load()
    _settings[key] = value
    save()


def put_many(**kwargs):
    """批量更新多个配置项并一次保存"""
    global _settings
    if _settings is None:
        load()
    _settings.update(kwargs)
    save()
