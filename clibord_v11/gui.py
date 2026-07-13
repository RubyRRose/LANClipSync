"""
GUI 层 — tkinter 界面 + 业务协调

职责：
  - 构建和管理 tkinter 界面
  - 协调 ClipboardManager 和 NetworkManager
  - 管理模式切换（同步模式 / 手动模式）
  - 处理界面状态更新
"""

import os
import re
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import date
from . import config, clipboard, net, settings


class GUI:
    """主界面类"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("LANClipSync - 局域网剪贴板同步")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 核心组件
        self.clip_mgr = clipboard.ClipboardManager()
        self.net_mgr = net.NetworkManager(app_key="")
        self.sync_mode = True
        self.running = False
        self._using_listener = False
        self.history = []          # 剪贴板历史 [{type, data, time, preview}]

        # 类型过滤（从设置加载）
        self.filter_text = settings.get("filter_text", True)
        self.filter_image = settings.get("filter_image", True)
        self.filter_files = settings.get("filter_files", True)

        # 日志文件路径
        try:
            self._exe_dir = os.path.dirname(sys.executable)
        except Exception:
            self._exe_dir = os.path.dirname(os.path.abspath(__file__))
        self._log_path = os.path.join(self._exe_dir, "LANClipSync.log")
        self._log_date = date.today()
        self._log_lock = threading.Lock()
        # 启动时: 如日志文件是上一日的 → 归档
        self._archive_if_mtime_old()
        # 启动时清理超过保留天数的旧日志
        self._clean_old_logs()

        # 启动时清理不属于任何历史条目的孤立缓存图片
        self._clean_orphan_hist_images()

        # 手动输入 IP 连接
        self._manual_ip_var = tk.StringVar()

        # 隐私模式（发送前确认）
        self.privacy_mode = settings.get("privacy_mode", False)

        # 最后接收的文件目录（用于打开文件夹）
        self._last_received_dir = None

        # 注册文件校验失败回调
        self.net_mgr.on_file_check_failed = self._on_file_check_failed

        # 注册网络回调
        self.net_mgr.on_peer_list_updated = self._refresh_peer_list
        self.net_mgr.on_connected = self._on_peer_connected
        self.net_mgr.on_disconnected = self._on_peer_disconnected
        self.net_mgr.on_log = self._log
        self.net_mgr.on_progress = self._progress_log

        # 注册剪贴板检测回调
        self.clip_mgr._on_text = self._on_text_detected
        self.clip_mgr._on_image = self._on_image_detected
        self.clip_mgr._on_files = self._on_files_detected

        # 注册网络接收回调
        self.net_mgr.on_text_received = self._on_text_received
        self.net_mgr.on_image_received = self._on_image_received
        self.net_mgr.on_files_received = self._on_files_received
        self.net_mgr.on_fetch_request = self._on_fetch_request

        # 构建界面
        self._build_ui()

        # 恢复上次的窗口位置（如已保存）
        wx = settings.get("window_x")
        wy = settings.get("window_y")
        ww = settings.get("window_width")
        wh = settings.get("window_height")
        if ww and wh:
            geom = f"{ww}x{wh}"
            if wx is not None and wy is not None:
                geom += f"+{wx}+{wy}"
            self.root.geometry(geom)
        else:
            self.root.geometry("700x860")

    # ── 界面构建 ──

    def _build_ui(self):
        root = self.root
        root.configure(bg="#1e1e2e")
        root.minsize(600, 600)

        # 样式
        style = ttk.Style()
        style.theme_use("clam")
        bg = "#1e1e2e"
        fg = "#cdd6f4"
        entry_bg = "#313244"
        btn_bg = "#45475a"
        select_bg = "#585b70"
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("TButton", background=btn_bg, foreground=fg,
                         borderwidth=1, focusthickness=3)
        style.map("TButton", background=[("active", select_bg)])
        style.configure("TRadiobutton", background=bg, foreground=fg)
        style.configure("TLabelframe", background=bg, foreground=fg)
        style.configure("TLabelframe.Label", background=bg, foreground=fg)

        # ── 密钥输入 ──
        key_frame = ttk.LabelFrame(root, text=" 共享密钥（可选） ", padding=8)
        key_frame.pack(fill="x", padx=15, pady=(10, 5))

        self.key_var = tk.StringVar()
        self._key_entry = ttk.Entry(key_frame, textvariable=self.key_var,
                                     show="*", width=40)
        self._key_entry.pack(side="left", padx=(0, 8), fill="x", expand=True)
        self._key_btn = ttk.Button(key_frame, text="👁",
                                    command=self._toggle_key_vis, width=4)
        self._key_btn.pack(side="left")

        # ── 操作区 ──
        ctrl_frame = ttk.Frame(root)
        ctrl_frame.pack(fill="x", padx=15, pady=5)

        # 启动/停止按钮
        self.toggle_btn = tk.Button(
            ctrl_frame, text="▶  点击启用同步",
            command=self._toggle,
            bg="#89b4fa", fg="#1e1e2e",
            font=("Segoe UI", 11, "bold"),
            relief="flat", padx=20, pady=8, cursor="hand2"
        )
        self.toggle_btn.pack(side="left", padx=(0, 10))

        self.status_label = tk.Label(
            ctrl_frame, text="⏸ 已停止",
            bg=bg, fg="#6c7086", font=("Segoe UI", 10)
        )
        self.status_label.pack(side="left")

        # 隐私模式按钮
        self._privacy_btn = tk.Button(
            ctrl_frame,
            text="🔒 隐私关" if not self.privacy_mode else "🔒 隐私开",
            command=self._toggle_privacy,
            bg="#45475a" if not self.privacy_mode else "#f9e2af",
            fg="#cdd6f4" if not self.privacy_mode else "#1e1e2e",
            font=("Segoe UI", 9),
            relief="flat", padx=8, pady=4, cursor="hand2"
        )
        self._privacy_btn.pack(side="left", padx=(10, 0))

        # 刷新网卡按钮
        self._refresh_net_btn = tk.Button(
            ctrl_frame,
            text="🔄 刷新网卡",
            command=self._refresh_network_interfaces,
            bg="#45475a", fg="#cdd6f4",
            font=("Segoe UI", 9),
            relief="flat", padx=8, pady=4, cursor="hand2"
        )
        self._refresh_net_btn.pack(side="left", padx=(5, 0))

        # ── 模式切换 ──
        mode_frame = ttk.LabelFrame(root, text=" 模式 ", padding=8)
        self.mode_var = tk.StringVar(value="sync")
        self.sync_rb = ttk.Radiobutton(
            mode_frame, text="同步模式（自动）",
            variable=self.mode_var, value="sync",
            command=self._on_mode_change
        )
        self.sync_rb.pack(side="left", padx=(0, 20))
        self.manual_rb = ttk.Radiobutton(
            mode_frame, text="手动模式（按需）",
            variable=self.mode_var, value="manual",
            command=self._on_mode_change
        )
        self.manual_rb.pack(side="left")

        # ── 类型过滤 ──
        filter_frame = ttk.LabelFrame(root, text=" 同步过滤 ", padding=6)
        self.filter_text_var = tk.BooleanVar(value=settings.get("filter_text", True))
        self.filter_img_var = tk.BooleanVar(value=settings.get("filter_image", True))
        self.filter_file_var = tk.BooleanVar(value=settings.get("filter_files", True))
        cb_kw = dict(bg="#313244", fg="#cdd6f4", selectcolor="#1e1e2e",
                     activebackground="#45475a", activeforeground="#cdd6f4",
                     relief="flat", highlightthickness=0,
                     font=("Segoe UI", 9))
        def _make_filter_cb(name, var, attr):
            def cb():
                val = var.get()
                setattr(self, attr, val)
                settings.put(f"filter_{attr.split('_')[1]}", val)
                self._log(f"🔘 {name}同步{'已开启' if val else '已关闭'} — {'允许传输' if val else '禁止传输'}")
            return cb

        tk.Checkbutton(filter_frame, text=" 文本", variable=self.filter_text_var,
                       command=_make_filter_cb("文本", self.filter_text_var, "filter_text"),
                       width=8, anchor="w",
                       **cb_kw).pack(side="left", padx=(0, 4))
        tk.Checkbutton(filter_frame, text=" 图片", variable=self.filter_img_var,
                       command=_make_filter_cb("图片", self.filter_img_var, "filter_image"),
                       width=8, anchor="w",
                       **cb_kw).pack(side="left", padx=(0, 4))
        tk.Checkbutton(filter_frame, text=" 文件", variable=self.filter_file_var,
                       command=_make_filter_cb("文件", self.filter_file_var, "filter_files"),
                       width=8, anchor="w",
                       **cb_kw).pack(side="left")

        # ── Peer 列表 ──
        peer_frame = ttk.LabelFrame(root, text=" 在线设备 ", padding=8)
        self.peer_listbox = tk.Listbox(
            peer_frame, height=3,
            bg="#313244", fg="#cdd6f4",
            selectbackground="#585b70",
            selectforeground="#cdd6f4",
            relief="flat", highlightthickness=0,
            font=("Segoe UI", 10)
        )
        self.peer_listbox.pack(fill="x")
        self.peer_listbox.bind("<<ListboxSelect>>", self._on_peer_select)
        self._connect_btn = ttk.Button(
            peer_frame, text="连接选中设备",
            command=self._connect_selected, state="disabled"
        )
        self._connect_btn.pack(pady=(5, 0))

        # ── 手动输入对端 IP ──
        manual_ip_frame = ttk.Frame(peer_frame)
        self._manual_ip_entry = ttk.Entry(
            manual_ip_frame, textvariable=self._manual_ip_var,
            width=18, font=("Segoe UI", 9)
        )
        self._manual_ip_entry.pack(side="left", padx=(0, 5))
        self._manual_ip_entry.bind("<Return>", lambda e: self._on_manual_connect())
        self._manual_connect_btn = ttk.Button(
            manual_ip_frame, text="直连",
            command=self._on_manual_connect, width=6
        )
        self._manual_connect_btn.pack(side="left")
        manual_ip_frame.pack(pady=(5, 0), fill="x")

        # ── 手动操作按钮 ──
        self.action_frame = ttk.Frame(root)
        self._send_btn = ttk.Button(
            self.action_frame, text="📤 发送剪贴板",
            command=self._manual_send, width=18
        )
        self._send_btn.pack(side="left", padx=(0, 10))
        self._fetch_btn = ttk.Button(
            self.action_frame, text="📥 获取对端剪贴板",
            command=self._manual_fetch, width=18
        )
        self._fetch_btn.pack(side="left")

        # ── 剪贴板历史 ──
        history_frame = ttk.LabelFrame(root, text=" 剪贴板历史 ", padding=8)
        self.history_listbox = tk.Listbox(
            history_frame, height=4,
            bg="#313244", fg="#cdd6f4",
            selectbackground="#585b70",
            selectforeground="#cdd6f4",
            relief="flat", highlightthickness=0,
            font=("Segoe UI", 10)
        )
        self.history_listbox.pack(fill="x")
        self.history_listbox.bind("<Double-1>", self._on_history_select)

        # ── 日志区 ──
        log_frame = ttk.LabelFrame(root, text=" 同步日志 ", padding=4)

        # 传输进度条（默认隐藏）
        self._progress_frame = tk.Frame(log_frame, bg="#1e1e2e")
        self._progress_bar = ttk.Progressbar(
            self._progress_frame, mode="determinate",
            length=0, value=0
        )
        self._progress_label = tk.Label(
            self._progress_frame, text="",
            bg="#1e1e2e", fg="#89b4fa",
            font=("Consolas", 9), anchor="w"
        )
        self._progress_bar.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._progress_label.pack(side="right")

        self.log_text = tk.Text(
            log_frame, height=17,
            bg="#1e1e2e", fg="#cdd6f4",
            insertbackground="#cdd6f4",
            relief="flat", highlightthickness=0,
            font=("Consolas", 9), wrap="word",
            state="disabled"
        )
        scrollbar = tk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        # 进度条初始隐藏（放 log_text 之后打包）
        self._progress_frame.pack(fill="x", before=self.log_text)
        self._progress_frame.pack_forget()

        # 打开文件夹链接（收到文件后显示）
        self._open_folder_link = tk.Label(
            log_frame, text="📂 打开文件夹",
            bg="#1e1e2e", fg="#89b4fa", cursor="hand2",
            font=("Segoe UI", 9)
        )
        self._open_folder_link.bind("<Button-1>", self._on_open_folder)
        # 初始隐藏

        # 日志标签样式
        self.log_text.tag_configure("info", foreground="#cdd6f4")
        self.log_text.tag_configure("success", foreground="#a6e3a1")
        self.log_text.tag_configure("error", foreground="#f38ba8")
        self.log_text.tag_configure("warn", foreground="#f9e2af")
        self.log_text.tag_configure("time", foreground="#6c7086")

        # ── 打包各区域 ──
        mode_frame.pack(fill="x", padx=15, pady=(0, 5))
        filter_frame.pack(fill="x", padx=15, pady=(0, 5))
        peer_frame.pack(fill="x", padx=15, pady=(0, 5))
        history_frame.pack(fill="x", padx=15, pady=(0, 5))
        log_frame.pack(fill="both", expand=True, padx=15, pady=(0, 10))

    # ── UI 更新方法 ──

    def _log(self, msg: str, tag: str = "info"):
        """向日志区添加一条消息（线程安全），同时写入日志文件"""
        if not msg:
            return
        timestamp = time.strftime("%H:%M:%S")
        full_ts = time.strftime("%Y-%m-%d %H:%M:%S")

        # ── 写入日志文件（按天轮转） ──
        today = date.today()
        if today != self._log_date:
            with self._log_lock:
                # 二次检查（拿到锁后可能已被其他线程更新）
                if today != self._log_date:
                    # 归档旧日志：LANClipSync.log → LANClipSync_2026-07-08.log
                    if os.path.exists(self._log_path) and os.path.getsize(self._log_path) > 0:
                        archive_name = f"LANClipSync_{self._log_date.isoformat()}.log"
                        archive_path = os.path.join(self._exe_dir, archive_name)
                        try:
                            os.replace(self._log_path, archive_path)
                        except Exception:
                            pass
                    self._log_date = today
                    # 清理超过保留天数的旧日志
                    self._clean_old_logs()
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(f"[{full_ts}] {msg}\n")
        except Exception:
            pass

        MAX_LOG_LINES = 2000

        def _trim_log():
            """保留最近 MAX_LOG_LINES 行"""
            try:
                lines = int(self.log_text.index("end-1c").split(".")[0])
                if lines > MAX_LOG_LINES:
                    excess = lines - MAX_LOG_LINES
                    self.log_text.delete("1.0", f"{excess + 1}.0")
            except tk.TclError:
                pass

        # ── 写入 GUI 日志区 ──
        def _insert():
            try:
                self.log_text.configure(state="normal")
                self.log_text.insert("end", f"[{timestamp}] ", "time")
                self.log_text.insert("end", msg + "\n", tag)
                _trim_log()
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
            except tk.TclError:
                pass  # 窗口已销毁

        if threading.current_thread() is threading.main_thread():
            _insert()
        else:
            self.root.after(0, _insert)

    def _archive_if_mtime_old(self):
        """启动时检查日志文件是否跨日，是则归档"""
        try:
            if not os.path.exists(self._log_path) or os.path.getsize(self._log_path) == 0:
                return
            mtime = os.path.getmtime(self._log_path)
            mtime_date = date.fromtimestamp(mtime)
            if mtime_date >= self._log_date:
                return  # 同一天，不用归档
            archive_name = f"LANClipSync_{mtime_date.isoformat()}.log"
            archive_path = os.path.join(self._exe_dir, archive_name)
            os.replace(self._log_path, archive_path)
        except Exception:
            pass

    def _clean_old_logs(self):
        """清理超过保留天数的旧日志文件"""
        try:
            now = time.time()
            cutoff = now - config.KEEP_LOG_DAYS * 86400
            for name in os.listdir(self._exe_dir):
                if not (name.startswith("LANClipSync_") and name.endswith(".log")):
                    continue
                path = os.path.join(self._exe_dir, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except Exception:
                    pass
        except Exception:
            pass

    def _progress_log(self, msg: str):
        """进度消息 — 更新 ProgressBar + 单行 Label，不写入日志区"""
        def _update():
            try:
                if msg:
                    # 解析百分比（格式如 "📤 发送文件... 45% (2.1 MB/s)"）
                    m = re.search(r"(\d+)%", msg)
                    if m:
                        pct = int(m.group(1))
                        self._progress_bar["value"] = pct
                    else:
                        # 无百分比时设为不确定状态（持续动画）
                        self._progress_bar["value"] = 0
                    self._progress_label["text"] = msg
                    # 如果 frame 未显示则显示
                    if not self._progress_frame.winfo_ismapped():
                        self._progress_frame.pack(fill="x", before=self.log_text)
                else:
                    # 空消息 → 隐藏进度条
                    self._progress_frame.pack_forget()
                    self._progress_bar["value"] = 0
                    self._progress_label["text"] = ""
            except tk.TclError:
                pass  # 窗口已销毁

        if threading.current_thread() is threading.main_thread():
            _update()
        else:
            self.root.after(0, _update)

    def _update_ui_state(self):
        """更新所有 UI 控件的状态"""
        is_running = self.running
        has_peer = bool(self.net_mgr.connected_peer_ip)
        is_manual = self.mode_var.get() == "manual"

        # 模式切换只有在停止时才可用
        state_mode = "normal" if not is_running else "disabled"
        self.sync_rb.configure(state=state_mode)
        self.manual_rb.configure(state=state_mode)

        # 手动操作按钮：只在手动模式 + 已启动时显示
        if is_running and is_manual:
            self.action_frame.pack(fill="x", padx=15, pady=(0, 5))
            manual_state = "normal" if has_peer else "disabled"
            self._send_btn.configure(state=manual_state)
            self._fetch_btn.configure(state=manual_state)
        else:
            self.action_frame.pack_forget()

        # 连接按钮
        self._connect_btn.configure(state="normal" if is_running else "disabled")

    def _refresh_peer_list(self):
        """更新 peer 列表显示"""
        def _update():
            self.peer_listbox.delete(0, "end")
            for peer in self.net_mgr.get_alive_peers():
                icon = "🔗" if peer.connected else "📡"
                display = peer.name if peer.name else peer.ip
                self.peer_listbox.insert("end",
                    f"{icon} {display}  ({peer.ip}:{peer.tcp_port})")
            self._update_ui_state()

        self.root.after(0, _update)

    def _on_peer_connected(self, ip: str):
        """对端连接（可能从非 GUI 线程调用，通过 after 派发）"""
        self.root.after(0, self._update_ui_state)

    def _on_peer_disconnected(self):
        """对端断开（可能从非 GUI 线程调用，通过 after 派发）"""
        self.root.after(0, self._update_ui_state)

    # ── 用户操作回调 ──

    def _toggle_key_vis(self):
        """切换密钥显示/隐藏"""
        if self._key_entry.cget("show") == "*":
            self._key_entry.configure(show="")
            self._key_btn.configure(text="🙈")
        else:
            self._key_entry.configure(show="*")
            self._key_btn.configure(text="👁")

    def _toggle(self):
        """启动/停止同步"""
        if not self.running:
            self._start()
        else:
            self._stop()

    def _toggle_privacy(self):
        """切换隐私模式"""
        self.privacy_mode = not self.privacy_mode
        settings.put("privacy_mode", self.privacy_mode)
        if self.privacy_mode:
            self._privacy_btn.configure(
                text="🔒 隐私开", bg="#f9e2af", fg="#1e1e2e")
            self._log("🔒 隐私模式已开启 — 发送前需确认", "warn")
        else:
            self._privacy_btn.configure(
                text="🔒 隐私关", bg="#45475a", fg="#cdd6f4")
            self._log("🔒 隐私模式已关闭 — 自动发送")

    def _refresh_network_interfaces(self):
        """手动刷新网卡接口列表"""
        if hasattr(self, 'net_mgr') and self.net_mgr:
            self.net_mgr.refresh_interfaces()
            count = len(self.net_mgr.get_cached_interfaces())
            self._log(f"🔄 网卡接口列表已刷新（发现 {count} 个局域网接口）", "info")
        else:
            self._log("⚠️ 网络未启动，无需刷新", "warn")

    def _on_file_check_failed(self, mismatched):
        """文件完整性校验失败"""
        msg = "以下文件完整性校验失败：\n\n" + "\n".join(mismatched)
        msg += "\n\n建议重新传输。"
        self.root.after(0, lambda: messagebox.showwarning(
            "文件校验失败", msg))
        self._log(f"⚠️ 文件完整性校验失败: {', '.join(mismatched)}", "error")

    def _start(self):
        """启动同步"""
        try:
            # 获取密钥
            key = self.key_var.get().strip()
            if key and len(key) < config.KEY_MIN_LENGTH:
                self._log(f"❌ 密钥至少 {config.KEY_MIN_LENGTH} 个字符", "error")
                return
            if len(key) > config.KEY_MAX_LENGTH:
                self._log(f"❌ 密钥最多 {config.KEY_MAX_LENGTH} 个字符", "error")
                return

            self.net_mgr.set_key(key)
            self.net_mgr.auto_connect = self.sync_mode
            self.clip_mgr = clipboard.ClipboardManager()
            self._reconnect_callbacks()

            # 启动网络
            self.net_mgr.start()
            # 首次枚举网卡接口
            self.net_mgr.refresh_interfaces()
            self.running = True

            # 注册剪贴板监听
            hwnd = self.root.winfo_id()
            self._using_listener = self.clip_mgr.start_listener(
                hwnd,
                on_text_changed=self._on_text_detected,
                on_image_changed=self._on_image_detected,
                on_files_changed=self._on_files_detected,
            )

            if not self._using_listener:
                self._log("⚠️ 剪贴板监听器注册失败，使用轮询模式", "warn")
                self._start_poll_fallback()

            # 更新 UI
            self.toggle_btn.configure(text="⏹  点击停止同步",
                                       bg="#f38ba8", activebackground="#eba0ac")
            self.status_label.configure(text="● 同步中", fg="#a6e3a1")
            self._log("✅ 同步已启用 — 等待对端设备...")
            self._log(f"   本机IP: {self.net_mgr.local_ip}")

            self._update_ui_state()

        except Exception as e:
            import traceback
            self.running = False
            self._log(f"❌ 启动失败: {e}", "error")
            # 回滚：停止可能已部分启动的网络
            try:
                self.net_mgr.stop()
                self.clip_mgr.stop_listener()
            except Exception:
                pass
            # 写崩溃日志（PyInstaller 下 __file__ 指向临时目录，用 exe 目录）
            try:
                exe_dir = os.path.dirname(sys.executable)
            except Exception:
                exe_dir = os.path.dirname(os.path.abspath(__file__))
            tb_path = os.path.join(exe_dir, "LANClipSync_crash.log")
            try:
                with open(tb_path, "a", encoding="utf-8") as f:
                    f.write(f"\n=== CRASH at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                    traceback.print_exc(file=f)
                    f.write("=" * 40 + "\n")
            except Exception:
                pass
            return

    def _stop(self):
        """停止同步"""
        self.running = False
        self.clip_mgr.stop_listener()
        self._stop_poll_fallback()
        self.net_mgr.stop()

        self.toggle_btn.configure(text="▶  点击启用同步",
                                   bg="#89b4fa", activebackground="#74c7ec")
        self.status_label.configure(text="⏸ 已停止", fg="#6c7086")
        self._log("⏹ 同步已停止")

        self._update_ui_state()

    def _reconnect_callbacks(self):
        """重新连接回调（_start 中重建 clip_mgr 后调用）"""
        self.clip_mgr._on_text = self._on_text_detected
        self.clip_mgr._on_image = self._on_image_detected
        self.clip_mgr._on_files = self._on_files_detected

    def _on_mode_change(self):
        """模式切换"""
        self.sync_mode = self.mode_var.get() == "sync"
        mode_name = "同步模式（自动）" if self.sync_mode else "手动模式（按需传输）"
        self._log(f"🔄 切换至 {mode_name}")
        self._update_ui_state()

    def _on_manual_connect(self):
        """手动输入 IP 直连"""
        ip = self._manual_ip_var.get().strip()
        if not ip:
            return
        # 用户可能输入 ip:port 格式
        port = config.TCP_PORT
        if ":" in ip:
            parts = ip.split(":")
            ip = parts[0].strip()
            try:
                port = int(parts[1].strip())
            except (ValueError, IndexError):
                pass
        if not self.running:
            self._log("⚠️ 请先启动同步", "warn")
            return
        self._log(f"🔗 正在直连 {ip}:{port}...")
        self.net_mgr.connect_to_ip(ip, port)

    def _on_peer_select(self, event):
        """peer 列表选择事件"""
        sel = self.peer_listbox.curselection()
        if sel:
            self._connect_btn.configure(state="normal")

    def _connect_selected(self):
        """连接选中的 peer"""
        sel = self.peer_listbox.curselection()
        if not sel:
            return
        text = self.peer_listbox.get(sel[0])
        # 格式: "📡 IP:PORT" 或 "🔗 IP:PORT"
        ip = text.split(" ")[1].split(":")[0]
        if self.net_mgr.connected_peer_ip == ip:
            self.net_mgr.disconnect()
        else:
            self.net_mgr.connect_to_peer(ip)

    def _manual_send(self):
        """手动发送当前剪贴板内容"""
        threading.Thread(target=self._do_manual_send,
                         daemon=True).start()

    def _do_manual_send(self):
        files = clipboard.get_files()
        if files:
            self._log(f"📤 手动发送 {len(files)} 个文件")
            self.net_mgr.send_files(files)
            return
        img = clipboard.get_image()
        if img:
            self._log(f"📤 手动发送图片 ({len(img)} 字节)")
            self.net_mgr.send_image(img)
            return
        text = clipboard.get_text()
        if text:
            ok, warn = clipboard.check_text_size(text)
            if not ok:
                self._log(warn)
                return
            if warn:
                self._log(warn)
            self._log(f"📤 手动发送文本")
            self.net_mgr.send_text(text)
            return
        self._log("⚠️ 本机剪贴板为空")

    def _manual_fetch(self):
        """手动获取对端剪贴板"""
        self.net_mgr.request_fetch()

    # ── 剪贴板检测回调 ──

    def _confirm_send_async(self, content_type: str, preview: str,
                             on_yes, on_no):
        """隐私模式下弹非模态确认窗口（不阻塞任何线程），30s 超时自动取消"""
        if not self.privacy_mode:
            on_yes()
            return

        def _show():
            win = tk.Toplevel(self.root)
            win.title("隐私确认 - LANClipSync")
            win.configure(bg="#1e1e2e")
            win.resizable(False, False)
            # 居中于主窗口
            win.transient(self.root)
            win.grab_set()

            frame = tk.Frame(win, bg="#1e1e2e", padx=20, pady=20)
            frame.pack(fill="both", expand=True)

            tk.Label(frame, text=f"检测到 {content_type} 变更",
                     bg="#1e1e2e", fg="#cdd6f4",
                     font=("Segoe UI", 12, "bold")).pack(anchor="w")
            tk.Label(frame, text=f"是否发送到对端？",
                     bg="#1e1e2e", fg="#a6e3a1",
                     font=("Segoe UI", 10)).pack(anchor="w", pady=(4, 0))
            tk.Label(frame, text=preview,
                     bg="#313244", fg="#f9e2af",
                     font=("Consolas", 9), wraplength=360, justify="left",
                     padx=8, pady=6).pack(fill="x", pady=(8, 4))

            countdown_label = tk.Label(frame, text="",
                                       bg="#1e1e2e", fg="#6c7086",
                                       font=("Segoe UI", 9))
            countdown_label.pack(anchor="e")

            btn_frame = tk.Frame(frame, bg="#1e1e2e")
            btn_frame.pack(fill="x", pady=(8, 0))

            closed = [False]

            def _cleanup():
                if closed[0]:
                    return
                closed[0] = True
                try:
                    win.grab_release()
                except Exception:
                    pass
                win.destroy()

            def _yes():
                _cleanup()
                threading.Thread(target=on_yes, daemon=True).start()

            def _no():
                _cleanup()
                on_no()

            def _timeout():
                _cleanup()
                self._log("🔒 隐私模式: 已超时自动取消")
                on_no()

            tk.Button(btn_frame, text="✅ 发送", command=_yes,
                      bg="#a6e3a1", fg="#1e1e2e",
                      font=("Segoe UI", 10, "bold"),
                      relief="flat", padx=16, pady=4, cursor="hand2"
                      ).pack(side="right", padx=(8, 0))
            tk.Button(btn_frame, text="❌ 取消", command=_no,
                      bg="#45475a", fg="#cdd6f4",
                      font=("Segoe UI", 10),
                      relief="flat", padx=16, pady=4, cursor="hand2"
                      ).pack(side="right")

            remaining = [30]
            def _tick():
                remaining[0] -= 1
                if closed[0]:
                    return
                if remaining[0] <= 0:
                    _timeout()
                    return
                countdown_label.config(
                    text=f"自动取消剩余 {remaining[0]} 秒")
                win.after(1000, _tick)

            countdown_label.config(text="自动取消剩余 30 秒")
            win.after(1000, _tick)

        self.root.after(0, _show)

    def _on_text_detected(self, text: str):
        """本地检测到文本变化"""
        if not self.filter_text:
            return
        ok, warn = clipboard.check_text_size(text)
        if not ok:
            self._log(warn)
            return
        if warn:
            self._log(warn, "warn")

        preview = text[:50].replace("\n", "\\n")
        self._log(f"📝 检测到文本复制: \"{preview}{'...' if len(text)>50 else ''}\"")
        if self.sync_mode and self.net_mgr.connected_peer_ip:
            self._confirm_send_async("文本", preview,
                on_yes=lambda: self.net_mgr.send_text(text),
                on_no=lambda: self._log("🔒 隐私模式: 已取消发送文本"))

    def _on_image_detected(self, data: bytes):
        """本地检测到图片变化"""
        if not self.filter_image:
            return
        self._log(f"🖼️ 检测到图片复制: {len(data)} 字节")
        if self.sync_mode and self.net_mgr.connected_peer_ip:
            self._confirm_send_async("图片", f"{len(data)} 字节",
                on_yes=lambda: self.net_mgr.send_image(data),
                on_no=lambda: self._log("🔒 隐私模式: 已取消发送图片"))

    def _on_files_detected(self, paths):
        """本地检测到文件变化"""
        if not self.filter_files:
            return
        self._log(f"📁 检测到文件复制: {len(paths)} 个项目")
        if self.sync_mode and self.net_mgr.connected_peer_ip:
            names = ", ".join(os.path.basename(p) for p in paths[:3])
            preview = f"{len(paths)} 个文件: {names}"
            self._confirm_send_async("文件", preview,
                on_yes=lambda: self.net_mgr.send_files(paths),
                on_no=lambda: self._log("🔒 隐私模式: 已取消发送文件"))

    # ── 网络接收回调 ──

    def _on_text_received(self, text: str):
        """收到远端文本"""
        self.clip_mgr.set_suppress(config.SUPPRESS_SECONDS)
        self.clip_mgr.update_hash("text", text)
        clipboard.set_text(text)
        preview = text[:50].replace("\n", "\\n")
        self._add_history("text", text, f"\"{preview}{'...' if len(text)>50 else ''}\"")
        self._log(f"📥 收到文本: \"{preview}{'...' if len(text)>50 else ''}\""
                   f" → 已写入剪贴板", "success")

    def _on_image_received(self, dib_data: bytes):
        """收到远端图片"""
        self.clip_mgr.set_suppress(config.SUPPRESS_SECONDS)
        self.clip_mgr.update_hash("img", dib_data)
        clipboard.set_image(dib_data)
        self._add_history("image", dib_data, f"{len(dib_data)} 字节")
        self._log(f"📥 收到图片: {len(dib_data)} 字节 → 已写入剪贴板",
                   "success")

    def _on_files_received(self, saved_paths):
        """收到远端文件"""
        self.clip_mgr.set_suppress(config.SUPPRESS_SECONDS)
        self.clip_mgr.update_hash("files", saved_paths)
        clipboard.set_files(saved_paths)
        names = [os.path.basename(p) for p in saved_paths]
        preview_names = ", ".join(names[:3])
        if len(names) > 3:
            preview_names += " …"
        self._add_history("files", saved_paths, f"{len(saved_paths)} 个文件 ({preview_names})")
        self._log(f"📥 收到 {len(saved_paths)} 个文件 → 已写入剪贴板",
                   "success")
        for n in names[:5]:
            self._log(f"    • {n}")
        if len(names) > 5:
            self._log(f"    ... 还有 {len(names)-5} 个")
        # 记录目录并显示打开文件夹链接
        if saved_paths:
            first = saved_paths[0]
            if os.path.isfile(first):
                self._last_received_dir = os.path.dirname(first)
            else:
                self._last_received_dir = first
            self._log(f"📂 保存路径: {self._last_received_dir}")
            self.root.after(0, self._show_open_folder_link)

    def _on_fetch_request(self):
        """收到对端的手动拉取请求"""
        self.net_mgr.respond_fetch()

    def _on_open_folder(self, event=None):
        """打开最后接收文件的目录"""
        if self._last_received_dir and os.path.isdir(self._last_received_dir):
            try:
                os.startfile(self._last_received_dir)
            except Exception as e:
                self._log(f"❌ 打开文件夹失败: {e}", "error")

    def _show_open_folder_link(self):
        """显示打开文件夹链接"""
        self._open_folder_link.pack(anchor="e", padx=(0, 5), before=self.log_text)
        # 5 秒后自动隐藏
        self.root.after(5000, lambda: self._open_folder_link.pack_forget())

    # ── 图片历史缓存 ──

    def _get_cache_root(self) -> str:
        """获取缓存根目录（与 net.py 逻辑一致）"""
        custom = settings.get("cache_dir", "")
        if custom:
            return custom
        return os.path.join(tempfile.gettempdir(), config.RECEIVE_CACHE_DIR)

    def _write_image_cache(self, data: bytes) -> str:
        """将图片 DIB 写入缓存目录 LANClipSync_cache/，返回文件路径"""
        cache_root = self._get_cache_root()
        os.makedirs(cache_root, exist_ok=True)
        fname = f"hist_img_{int(time.time() * 1000)}_{len(data)}.dib"
        fpath = os.path.join(cache_root, fname)
        with open(fpath, "wb") as f:
            f.write(data)
        return fpath

    def _clean_orphan_hist_images(self):
        """启动时清理不属于任何历史条目的孤立 hist_img_* 文件"""
        valid = set()
        for item in self.history:
            if item.get("image_path"):
                valid.add(item["image_path"])
        cache_root = self._get_cache_root()
        if not os.path.isdir(cache_root):
            return
        for name in os.listdir(cache_root):
            if not name.startswith("hist_img_"):
                continue
            fpath = os.path.join(cache_root, name)
            if fpath not in valid:
                try:
                    os.remove(fpath)
                except Exception:
                    pass

    # ── 剪贴板历史 ──

    def _add_history(self, type_, data, preview=""):
        """添加一条历史记录（非线程安全，仅从主线程或 receive 回调调用）"""
        if type_ == "image":
            # 图片：写盘后存路径，不保留 bytes 在内存
            img_path = self._write_image_cache(data)
            entry = {
                "type": "image",
                "image_path": img_path,
                "size": len(data),
                "time": time.strftime("%H:%M"),
                "preview": preview,
            }
        else:
            # 文本/文件：保持原有行为，数据在内存中
            entry = {
                "type": type_,
                "data": data,
                "time": time.strftime("%H:%M"),
                "preview": preview,
            }

        self.history.insert(0, entry)

        # 按类型独立限额，从尾部淘汰超出的条目
        limits = {"text": config.HISTORY_TEXT_MAX,
                  "image": config.HISTORY_IMAGE_MAX,
                  "files": config.HISTORY_FILES_MAX}
        limit = limits.get(type_, 50)
        count = 0
        for i in range(len(self.history) - 1, -1, -1):
            if self.history[i]["type"] == type_:
                count += 1
                if count > limit:
                    # 淘汰时删除对应的缓存文件（仅图片）
                    if self.history[i].get("image_path"):
                        try:
                            os.remove(self.history[i]["image_path"])
                        except Exception as e:
                            print(f"[LANClipSync] 删除历史缓存图片失败: {e}",
                                  file=sys.stderr)
                    self.history.pop(i)

        self.root.after(0, self._refresh_history_display)

    def _refresh_history_display(self):
        """刷新历史 Listbox"""
        self.history_listbox.delete(0, "end")
        for item in self.history:
            t = item["time"]
            if item["type"] == "text":
                pv = item["preview"][:40].replace("\n", " ")
                self.history_listbox.insert("end", f"{t}  📝 {pv}")
            elif item["type"] == "image":
                self.history_listbox.insert("end", f"{t}  🖼️ {item['preview']}")
            elif item["type"] == "files":
                self.history_listbox.insert("end", f"{t}  📁 {item['preview']}")

    def _on_history_select(self, event):
        """双击历史条目 → 恢复到剪贴板"""
        sel = self.history_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx >= len(self.history):
            return
        item = self.history[idx]
        try:
            if item["type"] == "text":
                clipboard.set_text(item["data"])
            elif item["type"] == "image":
                # 从磁盘读取缓存文件
                try:
                    with open(item["image_path"], "rb") as f:
                        dib_data = f.read()
                except (FileNotFoundError, IOError) as e:
                    self._log(f"❌ 历史图片文件已丢失: {e}", "error")
                    return
                clipboard.set_image(dib_data)
            elif item["type"] == "files":
                clipboard.set_files(item["data"])
            self._log(f"📋 已恢复历史记录: {item['preview']}", "success")
        except Exception as e:
            self._log(f"❌ 恢复历史记录失败: {e}", "error")

    # ── 轮询回退 ──

    def _start_poll_fallback(self):
        """启动轮询回退（监听器不可用时）"""
        self._poll_running = True
        t = threading.Thread(target=self._poll_loop,
                              daemon=True, name="clip-poll")
        t.start()

    def _stop_poll_fallback(self):
        self._poll_running = False

    def _poll_loop(self):
        """回退轮询循环"""
        while self.running and self._poll_running:
            self.clip_mgr.poll_once()
            time.sleep(config.CLIPBOARD_POLL_FALLBACK)

    # ── 关闭 ──

    def _on_close(self):
        """关闭窗口 — 保存设置"""
        self._stop()
        # 保存窗口位置
        try:
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            settings.put_many({
                "window_x": x, "window_y": y,
                "window_width": w, "window_height": h,
                "privacy_mode": self.privacy_mode,
            })
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        """启动主循环"""
        self.root.mainloop()
