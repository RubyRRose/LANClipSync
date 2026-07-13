"""
网络管理 — UDP 发现 / TCP 传输 / ACK 确认 / 断线重连 / Peer 管理

职责：
  - UDP 广播发现 + 心跳保活
  - TCP 服务端（接收数据 + 回 ACK）
  - TCP 客户端（发送数据 + 等待 ACK + 失败重试）
  - Peer 状态管理（在线 / 离线 / 超时清理）
  - 端口冲突自动 +1 重试
  - 传输层加密（加密握手 + payload 加密）
"""

import ctypes
import hashlib
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import traceback
import zlib
from . import config, protocol, crypto, clipboard, settings


# ── 工具函数 ──

def get_local_ip():
    """获取本机局域网 IP（纯内网，不依赖外网）"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("192.168.0.1", 1))  # 不真正发包
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as e:
        print(f"[LANClipSync] get_local_ip 失败: {e}", file=sys.stderr)
    return "127.0.0.1"


def get_local_ips():
    """获取本机所有非回环 IP"""
    ips: set[str] = set()
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception as e:
        print(f"[LANClipSync] get_local_ips 失败: {e}", file=sys.stderr)
    if not ips:
        ips.add(get_local_ip())
    return ips


def get_lan_interfaces():
    """枚举所有局域网接口的 (IP, 广播地址) — 使用 GetAdaptersAddresses 正确计算子网广播地址"""
    interfaces = []

    # ── Windows API 类型与结构 ──
    iphlpapi = ctypes.windll.iphlpapi
    _GetAdaptersAddresses = iphlpapi.GetAdaptersAddresses
    _GetAdaptersAddresses.argtypes = [
        ctypes.c_ulong,        # Family
        ctypes.c_ulong,        # Flags
        ctypes.c_void_p,       # Reserved
        ctypes.c_void_p,       # AdapterAddresses (output)
        ctypes.POINTER(ctypes.c_ulong),  # SizePointer
    ]
    _GetAdaptersAddresses.restype = ctypes.c_ulong

    class _SOCKET_ADDRESS(ctypes.Structure):
        _fields_ = [
            ("lpSockaddr", ctypes.c_void_p),
            ("iSockaddrLength", ctypes.c_int),
        ]

    class _UNICAST_ADDRESS(ctypes.Structure):
        _fields_ = [
            ("__alignment", ctypes.c_ulonglong),   # +0, union: Alignment | Length+Flags
            ("Next", ctypes.c_void_p),              # +8
            ("Address", _SOCKET_ADDRESS),           # +16 (16B: lpSockaddr 8 + iSockaddrLength 4 + padding 4)
            ("PrefixOrigin", ctypes.c_int),         # +32
            ("SuffixOrigin", ctypes.c_int),         # +36
            ("DadState", ctypes.c_int),             # +40
            ("ValidLifetime", ctypes.c_ulong),      # +44
            ("PreferredLifetime", ctypes.c_ulong),  # +48
            ("LeaseLifetime", ctypes.c_ulong),      # +52
            ("OnLinkPrefixLength", ctypes.c_ubyte), # +56
        ]

    class _ADAPTER_ADDRESSES(ctypes.Structure):
        _fields_ = [
            ("__alignment", ctypes.c_ulonglong),        # +0
            ("Next", ctypes.c_void_p),                   # +8
            ("AdapterName", ctypes.c_char_p),            # +16
            ("FirstUnicastAddress", ctypes.c_void_p),    # +24
        ]

    AF_INET = 2
    ERROR_SUCCESS = 0

    buf_size = ctypes.c_ulong(64 * 1024)
    buf = ctypes.create_string_buffer(buf_size.value)

    try:
        rc = _GetAdaptersAddresses(
            AF_INET, 0, None, buf, ctypes.byref(buf_size))
    except Exception as e:
        print(f"[LANClipSync] GetAdaptersAddresses 调用失败: {e}", file=sys.stderr)
        return _lan_interfaces_fallback()

    if rc != ERROR_SUCCESS:
        print(f"[LANClipSync] GetAdaptersAddresses 返回 {rc}，回退到旧方法",
              file=sys.stderr)
        return _lan_interfaces_fallback()

    p = ctypes.cast(buf, ctypes.POINTER(_ADAPTER_ADDRESSES))
    while p:
        aa = p.contents
        if aa.FirstUnicastAddress:
            ua = ctypes.cast(
                aa.FirstUnicastAddress, ctypes.POINTER(_UNICAST_ADDRESS))
            while ua:
                u = ua.contents
                sockaddr = u.Address.lpSockaddr
                socklen = u.Address.iSockaddrLength
                if sockaddr and socklen >= 16:
                    family = ctypes.c_ushort.from_address(sockaddr).value
                    if family == AF_INET:
                        ip_b = (ctypes.c_ubyte * 4).from_address(
                            sockaddr + 4)
                        ip = ".".join(str(b) for b in ip_b)
                        if not ip.startswith("127."):
                            prefix = u.OnLinkPrefixLength
                            if prefix == 0 or prefix > 31:
                                prefix = 24  # 安全兜底
                            # 广播地址 = IP | (~子网掩码)
                            ip_int = (
                                (ip_b[0] << 24) | (ip_b[1] << 16) |
                                (ip_b[2] << 8) | ip_b[3]
                            )
                            mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
                            bcast = ip_int | (~mask & 0xFFFFFFFF)
                            baddr = ".".join(
                                str((bcast >> (8 * (3 - i))) & 0xFF)
                                for i in range(4)
                            )
                            interfaces.append((ip, baddr))
                nxt = u.Next
                ua = ctypes.cast(nxt, ctypes.POINTER(_UNICAST_ADDRESS)) if nxt else None
        nxt = aa.Next
        p = ctypes.cast(nxt, ctypes.POINTER(_ADAPTER_ADDRESSES)) if nxt else None

    if not interfaces:
        return _lan_interfaces_fallback()
    return interfaces


def _lan_interfaces_fallback():
    """get_lan_interfaces 的降级方案：gethostbyname_ex + /24 假设"""
    interfaces = []
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127."):
                parts = ip.split(".")
                baddr = f"{parts[0]}.{parts[1]}.{parts[2]}.255"
                interfaces.append((ip, baddr))
    except Exception as e:
        print(f"[LANClipSync] 备用接口枚举失败: {e}", file=sys.stderr)
    if not interfaces:
        ip = get_local_ip()
        if ip != "127.0.0.1":
            parts = ip.split(".")
            baddr = f"{parts[0]}.{parts[1]}.{parts[2]}.255"
            interfaces.append((ip, baddr))
    return interfaces


def _validate_relpath(relpath):
    """检查 relpath 是否合法（拒绝路径穿越和绝对路径）"""
    # 拒绝含 .. 的路径
    if ".." in relpath.split(os.sep):
        return False
    if ".." in relpath.split("/"):
        return False
    # 拒绝绝对路径
    if os.path.isabs(relpath):
        return False
    # 拒绝以 / 或 \\ 开头
    if relpath.startswith("/") or relpath.startswith("\\"):
        return False
    return True


def _auto_rename(path):
    """文件已存在时自动重命名：file.txt → file (1).txt → file (2).txt"""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    for i in range(1, 999):
        new_path = f"{base} ({i}){ext}"
        if not os.path.exists(new_path):
            return new_path
    return path  # 兜底


def _file_md5(path: str) -> str:
    """计算文件的 MD5 哈希"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ── PeerInfo ──

class PeerInfo:
    """对端信息"""
    def __init__(self, ip: str, tcp_port: int, fingerprint: str, name: str = ""):
        self.ip = ip
        self.tcp_port = tcp_port
        self.fingerprint = fingerprint
        self.last_seen = time.time()
        self.connected = False  # 用户是否选中此 peer 作为发送目标
        self.name = name or ip  # 设备别名

    @property
    def is_alive(self, timeout=config.PEER_TIMEOUT):
        return (time.time() - self.last_seen) < timeout

    def touch(self):
        self.last_seen = time.time()


# ── 文件传输进度回调 ──

# 进度回调类型
ProgressCallback = type(lambda: None)  # (message: str) -> None


# ── NetworkManager ──

class NetworkManager:
    """网络管理器"""

    def __init__(self, app_key: str):
        self.key = app_key
        self.fingerprint = crypto.make_fingerprint(app_key) if app_key else ""

        self.local_ip = get_local_ip()
        self.local_ips = get_local_ips()
        self.running = False

        # 设备名称
        s = settings.load()
        self.device_name = (s.get("device_name") or
                            socket.gethostname())

        # 端口（实际绑定的）
        self.udp_port = config.UDP_PORT
        self.tcp_port = config.TCP_PORT

        # socket 引用（用于 stop 时关闭）
        self._tcp_server_sock = None
        self._udp_sock = None

        # Peer 管理
        self._peers: dict[str, PeerInfo] = {}
        self._peers_lock = threading.RLock()
        self._connected_peer_ip: str | None = None

        # 自动连接（同步模式下自动连到第一个发现的对端）
        self.auto_connect = False

        # 自动重连：记住最后主动连接的 IP
        self._last_connected_ip: str | None = None

        # 回调（由 GUI 层设置）
        self.on_peer_list_updated = None   # 当 peer 列表变化时
        self.on_connected = None           # 当连接到某个 peer 时
        self.on_disconnected = None        # 当断开时
        self.on_text_received = None       # (text)
        self.on_image_received = None      # (data)
        self.on_files_received = None      # (saved_paths)
        self.on_fetch_request = None       # 收到手动拉取请求
        self.on_file_check_failed = None   # (mismatched_relpaths)
        self.on_log = None                 # (message)
        self.on_progress = None            # (message)

        # 缓存网卡接口列表（解决频繁调用 get_lan_interfaces 的内存增长）
        self._cached_interfaces: list[tuple[str, str]] = []
        self._interfaces_cached = False

        # 后台线程引用
        self._threads = []

    # ── connected_peer_ip 线程安全属性 ──

    @property
    def connected_peer_ip(self) -> str | None:
        with self._peers_lock:
            return self._connected_peer_ip

    @connected_peer_ip.setter
    def connected_peer_ip(self, value: str | None) -> None:
        with self._peers_lock:
            self._connected_peer_ip = value

    # ── 日志辅助 ──

    def _log(self, msg: str):
        if self.on_log:
            self.on_log(msg)

    def _progress(self, msg: str):
        if self.on_progress:
            self.on_progress(msg)

    # ── 网卡接口缓存 ──

    def refresh_interfaces(self):
        """重新枚举网卡接口（手动刷新按钮调用）"""
        self._cached_interfaces = get_lan_interfaces()
        self._interfaces_cached = True

    def get_cached_interfaces(self) -> list[tuple[str, str]]:
        """获取缓存的网卡接口列表，未刷新时自动执行首次查询"""
        if not self._interfaces_cached:
            self.refresh_interfaces()
        return self._cached_interfaces

    # ── 生命周期 ──

    def start(self):
        """启动网络服务（TCP 监听 + UDP 收发）"""
        if self.running:
            return
        self.running = True

        # 清理旧文件缓存（启动时清理，避免积累）
        self._clean_file_cache()

        # 启动 TCP 监听
        t_tcp = threading.Thread(target=self._tcp_server_loop,
                                  daemon=True, name="tcp-server")
        t_tcp.start()
        self._threads.append(t_tcp)

        # 启动 UDP 接收
        t_udp_recv = threading.Thread(target=self._udp_recv_loop,
                                       daemon=True, name="udp-recv")
        t_udp_recv.start()
        self._threads.append(t_udp_recv)

        # 启动 UDP 发送（广播）
        t_udp_send = threading.Thread(target=self._udp_send_loop,
                                       daemon=True, name="udp-send")
        t_udp_send.start()
        self._threads.append(t_udp_send)

        # 启动 peer 清理线程
        t_cleanup = threading.Thread(target=self._peer_cleanup_loop,
                                      daemon=True, name="peer-cleanup")
        t_cleanup.start()
        self._threads.append(t_cleanup)

        self._log(f"🌐 网络已启动 (TCP:{self.tcp_port} UDP:{self.udp_port})")

    def stop(self):
        """停止所有网络服务"""
        self.running = False
        # 关闭 socket 以中断阻塞的线程
        try:
            if self._tcp_server_sock:
                self._tcp_server_sock.close()
        except Exception:
            pass
        try:
            if self._udp_sock:
                self._udp_sock.close()
        except Exception:
            pass
        self._threads.clear()
        # 清理连接状态，下次 start() 从干净状态开始
        self._connected_peer_ip = None
        self._last_connected_ip = None
        with self._peers_lock:
            self._peers.clear()

    # ── 密钥 ──

    def set_key(self, new_key: str):
        self.key = new_key
        self.fingerprint = crypto.make_fingerprint(new_key) if new_key else ""

    # ── Peer 管理 ──

    def get_alive_peers(self):
        """返回当前在线的 peer 列表"""
        now = time.time()
        alive = []
        with self._peers_lock:
            for p in self._peers.values():
                if (now - p.last_seen) < config.PEER_TIMEOUT:
                    alive.append(p)
        return alive

    def get_peer(self, ip: str):
        with self._peers_lock:
            return self._peers.get(ip)

    def connect_to_peer(self, ip: str) -> bool:
        """用户选择连接到某个 peer"""
        peer = self.get_peer(ip)
        if not peer:
            self._log(f"❌ 对端 {ip} 不在线")
            return False
        # 取消之前的连接
        with self._peers_lock:
            for p in self._peers.values():
                p.connected = False
            peer.connected = True
            self._connected_peer_ip = ip
        self._last_connected_ip = ip
        self._log(f"🔗 已连接到对端: {ip}")
        if self.on_connected:
            self.on_connected(ip)
        if self.on_peer_list_updated:
            self.on_peer_list_updated()
        return True

    def connect_to_ip(self, ip: str, port: int = None) -> bool:
        """手动输入 IP 直连（无需广播发现）"""
        # 创建临时 peer（如果不存在）
        with self._peers_lock:
            if ip not in self._peers:
                self._peers[ip] = PeerInfo(ip, port or config.TCP_PORT, "")
            for p in self._peers.values():
                p.connected = False
            self._peers[ip].connected = True
            self._connected_peer_ip = ip
        self._last_connected_ip = ip
        self._log(f"🔗 已直连: {ip}:{port or config.TCP_PORT}")
        if self.on_connected:
            self.on_connected(ip)
        if self.on_peer_list_updated:
            self.on_peer_list_updated()
        return True

    def disconnect(self):
        """断开当前连接"""
        with self._peers_lock:
            for p in self._peers.values():
                p.connected = False
            self._connected_peer_ip = None
        self._log("⏹ 已断开连接")
        if self.on_disconnected:
            self.on_disconnected()
        if self.on_peer_list_updated:
            self.on_peer_list_updated()

    def _update_peer(self, ip: str, tcp_port: int, fingerprint: str, name: str = ""):
        """更新 peer 状态（收到广播时调用）"""
        # 跳过本机
        if ip in self.local_ips:
            return
        # 指纹验证
        if self.fingerprint:
            if fingerprint != self.fingerprint:
                return
        with self._peers_lock:
            if ip in self._peers:
                p = self._peers[ip]
                p.touch()
                if name:
                    p.name = name
                # 自动重连：上次连过的对端上线了，且当前未连接
                if (self._last_connected_ip == ip
                        and not self._connected_peer_ip
                        and self.running):
                    p.connected = True
                    self._connected_peer_ip = ip
                    self._log(f"🔗 已自动重连: {p.name} ({ip})")
                    if self.on_connected:
                        self.on_connected(ip)
                    if self.on_peer_list_updated:
                        self.on_peer_list_updated()
                return
            # 新 peer
            peer = PeerInfo(ip, tcp_port, fingerprint, name)
            self._peers[ip] = peer
        display = f"{name} ({ip})" if name else ip
        self._log(f"🔍 发现对端: {display}")
        if self.auto_connect and not self._connected_peer_ip:
            self.connect_to_peer(ip)
        if self.on_peer_list_updated:
            self.on_peer_list_updated()

    def _peer_cleanup_loop(self):
        """定时清理超时的 peer"""
        while self.running:
            time.sleep(config.PEER_CLEANUP_INTERVAL)
            now = time.time()
            changed = False
            with self._peers_lock:
                expired = [
                    ip for ip, p in self._peers.items()
                    if (now - p.last_seen) >= config.PEER_TIMEOUT
                ]
                for ip in expired:
                    peer = self._peers.pop(ip)
                    if peer.connected:
                        self._connected_peer_ip = None
                        self._log(f"📡 对端 {ip} 已离线")
                        if self.on_disconnected:
                            self.on_disconnected()
                changed = bool(expired)
            if changed and self.on_peer_list_updated:
                self.on_peer_list_updated()

    # ── UDP 广播 ──

    def _udp_send_loop(self):
        """定期发送 UDP 广播到所有网卡（使用缓存接口列表）"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(1)
            while self.running:
                for ip, baddr in self.get_cached_interfaces():
                    msg = json.dumps({
                        "magic": config.PROTOCOL_MAGIC.decode(),
                        "ip": ip,
                        "tcp_port": self.tcp_port,
                        "fingerprint": self.fingerprint,
                        "name": self.device_name,
                    }).encode()
                    try:
                        sock.sendto(msg, (baddr, self.udp_port))
                    except Exception as e:
                        print(f"[LANClipSync] UDP 发送失败 ({baddr}): {e}",
                              file=sys.stderr)
                time.sleep(config.BROADCAST_INTERVAL)
            sock.close()
        except Exception as e:
            if self.running:
                self._log(f"UDP 发送异常: {e}")

    def _udp_recv_loop(self):
        """监听 UDP 广播"""
        self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._udp_sock.setsockopt(socket.SOL_SOCKET,
                                       socket.SO_BROADCAST, 1)
        except Exception:
            pass
        # 端口冲突重试
        for port in range(self.udp_port,
                          self.udp_port + config.TCP_PORT_RETRY_MAX):
            try:
                self._udp_sock.bind(("", port))
                self.udp_port = port
                break
            except OSError:
                continue
        self._udp_sock.settimeout(1)

        while self.running:
            try:
                data, addr = self._udp_sock.recvfrom(1024)
                info = json.loads(data.decode())
                if info.get("magic") != config.PROTOCOL_MAGIC.decode():
                    continue
                peer_ip = info["ip"]
                peer_port = info.get("tcp_port", config.TCP_PORT)
                peer_fp = info.get("fingerprint", "")
                peer_name = info.get("name", "")
                self._update_peer(peer_ip, peer_port, peer_fp, peer_name)
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    self._log(f"UDP 接收异常: {e}")
                continue
        try:
            self._udp_sock.close()
        except Exception:
            pass

    # ── TCP 服务端 ──

    def _tcp_server_loop(self):
        """TCP 服务端，接受对端连接"""
        self._tcp_server_sock = socket.socket(socket.AF_INET,
                                                socket.SOCK_STREAM)
        self._tcp_server_sock.setsockopt(socket.SOL_SOCKET,
                                           socket.SO_REUSEADDR, 1)
        # 端口冲突重试
        for port in range(config.TCP_PORT,
                          config.TCP_PORT + config.TCP_PORT_RETRY_MAX):
            try:
                self._tcp_server_sock.bind(("", port))
                self.tcp_port = port
                break
            except OSError:
                continue
        self._tcp_server_sock.listen(5)
        self._tcp_server_sock.settimeout(1)

        while self.running:
            try:
                conn, addr = self._tcp_server_sock.accept()
                t = threading.Thread(target=self._handle_incoming,
                                     args=(conn,),
                                     daemon=True,
                                     name="tcp-handle")
                t.start()
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    self._log(f"TCP 接受连接异常: {e}")
                continue
        try:
            self._tcp_server_sock.close()
        except Exception:
            pass

    def _handle_incoming(self, conn: socket.socket):
        """处理收到的 TCP 连接"""
        conn.settimeout(config.SOCKET_TIMEOUT)
        try:
            msg = protocol.recv_msg(conn)
            if not msg:
                return
            msg_type, payload = msg

            if msg_type == protocol.MSG_TEXT:
                # 解密
                if self.key:
                    try:
                        payload = crypto.decrypt(payload, self.key)
                    except Exception:
                        self._log("⚠️ 文本解密失败，跳过")
                        return
                try:
                    payload = zlib.decompress(payload)
                except zlib.error:
                    pass
                text = payload.decode("utf-8")
                if self.on_text_received:
                    self.on_text_received(text)
                self._send_ack(conn)

            elif msg_type == protocol.MSG_IMAGE:
                if self.key:
                    try:
                        payload = crypto.decrypt(payload, self.key)
                    except Exception:
                        self._log("⚠️ 图片解密失败，跳过")
                        return
                try:
                    dib_data = zlib.decompress(payload)
                except zlib.error:
                    dib_data = payload
                if self.on_image_received:
                    self.on_image_received(dib_data)
                self._send_ack(conn)

            elif msg_type == protocol.MSG_FILES:
                self._handle_incoming_files(conn, payload)

            elif msg_type == protocol.MSG_FETCH_REQUEST:
                conn.close()
                if self.on_fetch_request:
                    threading.Thread(target=self.on_fetch_request,
                                     daemon=True,
                                     name="fetch-respond").start()

            elif msg_type == protocol.MSG_ACK:
                # ACK 由客户端处理，服务端收到可忽略
                pass

            elif msg_type == protocol.MSG_FILE_CHECK:
                # 由 _handle_incoming_files 内联处理，此处不会到达
                self._send_ack(conn)

        except Exception as e:
            self._log(f"接收错误: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ── 文件缓存管理 ──

    @staticmethod
    def _get_cache_root():
        custom = settings.get("cache_dir", "")
        if custom:
            return custom
        return os.path.join(tempfile.gettempdir(), config.RECEIVE_CACHE_DIR)

    @staticmethod
    def _clean_file_cache():
        """启动时清理超过 CACHE_FILE_MAX_AGE 秒的旧缓存文件"""
        cache_root = NetworkManager._get_cache_root()
        if not os.path.exists(cache_root):
            return
        now = time.time()
        for entry in os.listdir(cache_root):
            entry_path = os.path.join(cache_root, entry)
            try:
                age = now - os.path.getmtime(entry_path)
                if age > config.CACHE_FILE_MAX_AGE:
                    if os.path.isfile(entry_path):
                        os.remove(entry_path)
                    elif os.path.isdir(entry_path):
                        shutil.rmtree(entry_path, ignore_errors=True)
            except Exception:
                pass

    def _handle_incoming_files(self, conn: socket.socket, payload: bytes):
        """接收文件传输（已在 _handle_incoming 中分派）"""
        # 由于文件数据是流式的，不能先接收完整 payload
        # 这里重新从 conn 读取
        # 实际上文件的元数据在 payload 中
        try:
            meta = json.loads(payload.decode("utf-8"))
        except Exception:
            self._log("⚠️ 文件元数据解析失败")
            return

        # ── 流式解密（如元数据中携带了 enc_salt） ──
        file_dec = None
        enc_salt_hex = meta.get("enc_salt", "")
        if enc_salt_hex and self.key:
            try:
                file_dec = crypto.FileDecryptor(self.key, bytes.fromhex(enc_salt_hex))
            except Exception:
                self._log("⚠️ 文件解密初始化失败，跳过")
                return

        saved_paths = []
        cache_root = self._get_cache_root()
        os.makedirs(cache_root, exist_ok=True)

        # ── 如果是文件夹传输（relpath 含目录前缀），先清空旧缓存 ──
        # 避免 _auto_rename 把文件命名为 "xxx (1).png" 导致粘贴时重复
        all_relpaths = []
        for finfo in meta.get("files", []):
            rp = finfo.get("relpath", finfo["name"])
            all_relpaths.append(rp)
        if len(all_relpaths) > 1:
            common_rel = os.path.commonpath(all_relpaths)
            if common_rel:
                common_cache = os.path.join(cache_root, common_rel)
                if common_cache != cache_root and os.path.exists(common_cache):
                    shutil.rmtree(common_cache, ignore_errors=True)

        try:
            saved_relpath_map = {}  # relpath → actual save path
            received_md5s = {}     # relpath → 本地计算的 MD5
            for finfo in meta.get("files", []):
                fname = finfo["name"]
                fsize = finfo["size"]
                relpath = finfo.get("relpath", fname)

                # 路径校验
                if not _validate_relpath(relpath):
                    self._log(f"⚠️ 文件名不合法已跳过: {relpath}")
                    # 跳过此文件，但要继续读取数据流
                    remaining = fsize
                    while remaining > 0:
                        chunk = protocol.recv_exact(
                            conn, min(config.CHUNK_SIZE, remaining))
                        if not chunk:
                            return
                        remaining -= len(chunk)
                    continue

                save_path = os.path.join(cache_root, relpath)
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                save_path = _auto_rename(save_path)
                saved_relpath_map[relpath] = save_path

                remaining = fsize
                md5_hash = hashlib.md5()
                with open(save_path, "wb") as f:
                    while remaining > 0:
                        chunk_size = min(config.CHUNK_SIZE, remaining)
                        chunk = protocol.recv_exact(conn, chunk_size)
                        if not chunk:
                            return
                        if file_dec:
                            chunk = file_dec.decrypt_chunk(chunk)
                        f.write(chunk)
                        md5_hash.update(chunk)
                        remaining -= len(chunk)
                saved_paths.append(save_path)
                received_md5s[relpath] = md5_hash.hexdigest()

            if saved_paths:
                # ── 文件接收完毕，先 ACK ──
                self._send_ack(conn)

                # 如果所有文件在同一子目录下（文件夹传输），只传目录路径
                # 这样对端粘贴时才能保留文件夹结构
                if len(saved_paths) > 1:
                    common = os.path.commonpath(saved_paths)
                    if common != cache_root and os.path.isdir(common):
                        saved_paths = [common]
                if self.on_files_received:
                    self.on_files_received(saved_paths)

                # ── 等待发送端发来 MSG_FILE_CHECK 进行完整性校验 ──
                check_msg = protocol.recv_msg(conn)
                if check_msg and check_msg[0] == protocol.MSG_FILE_CHECK:
                    try:
                        check_payload = json.loads(check_msg[1].decode("utf-8"))

                        # ── HMAC 完整性校验（如启用加密） ──
                        enc_hmac_hex = check_payload.get("enc_hmac", "")
                        if file_dec and enc_hmac_hex:
                            hmac_ok = file_dec.verify(bytes.fromhex(enc_hmac_hex))
                            if not hmac_ok:
                                self._log("⚠️ 文件 HMAC 校验失败，传输可能被篡改")
                                mismatches = ["__HMAC_MISMATCH__"]
                        else:
                            hmac_ok = True

                        # ── 逐文件 MD5 校验 ──
                        if hmac_ok:
                            mismatches = []
                            for finfo in check_payload.get("files", []):
                                rp = finfo.get("relpath", "")
                                expected = finfo.get("md5", "")
                                actual = received_md5s.get(rp)
                                if expected and actual and actual != expected:
                                    mismatches.append(rp)
                                elif expected and not actual:
                                    mismatches.append(rp)

                        if mismatches and self.on_file_check_failed:
                            self.on_file_check_failed(mismatches)
                    except Exception:
                        pass

                self._send_ack(conn)  # ACK for MSG_FILE_CHECK
            else:
                pass
        except Exception as e:
            self._log(f"接收文件异常: {e}")

    def _send_ack(self, conn: socket.socket):
        """发送 ACK 确认"""
        try:
            protocol.send_msg(conn, protocol.MSG_ACK)
        except Exception as e:
            self._log(f"[ACK] 发送确认失败: {e}")

    # ── TCP 发送（带 ACK + 重试） ──

    def _get_connected_peer(self):
        """获取当前连接的对端地址"""
        ip = self._connected_peer_ip
        if not ip:
            self._log("未连接到任何对端")
            return None
        peer = self.get_peer(ip)
        port = peer.tcp_port if peer else config.TCP_PORT
        return ip, port

    def _send_with_retry(self, msg_type: int, payload: bytes,
                          description: str = "") -> bool:
        """
        发送数据到对端，等待 ACK，失败重试。

        返回是否成功收到 ACK。
        """
        addr = self._get_connected_peer()
        if not addr:
            return False
        peer_ip, peer_port = addr

        for attempt in range(1, config.SEND_RETRY_MAX + 1):
            if not self.running:
                return False
            # 重试前检查 peer 是否还在线
            if not self._connected_peer_ip:
                self._log("对端已断开，停止发送")
                return False

            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)  # 连接阶段 5s 超时（快速反馈错误 IP）
                sock.connect((peer_ip, peer_port))
                sock.settimeout(config.SOCKET_TIMEOUT)  # 数据传输恢复 30s

                # 发送
                protocol.send_msg(sock, msg_type, payload)

                # 等待 ACK
                ack = protocol.recv_msg(sock)
                sock.close()

                if ack and ack[0] == protocol.MSG_ACK:
                    if description:
                        self._log(f"✅ {description}")
                    return True
                else:
                    self._log(f"⚠️ {description} 未收到确认")
                    return False

            except (socket.timeout, ConnectionError, OSError) as e:
                self._log(f"⏳ {description} 发送失败 (第{attempt}次): {e}")
                if attempt < config.SEND_RETRY_MAX:
                    time.sleep(2 ** attempt)  # 2s, 4s
                continue
            finally:
                try:
                    sock.close()
                except Exception:
                    pass

        self._log(f"❌ {description} 发送失败，已重试 {config.SEND_RETRY_MAX} 次")
        return False

    # ── 公开发送接口 ──

    def send_text(self, text: str) -> bool:
        """发送文本到对端（自动加密 + zlib 压缩）"""
        ok, warn = clipboard.check_text_size(text)
        if not ok:
            self._log(warn)
            return False
        if warn:
            self._log(warn)

        data = text.encode("utf-8")
        # zlib 压缩（4KB 以下不压，短文本无收益）
        if len(data) > 4096:
            compressed = zlib.compress(data)
        else:
            compressed = data
        ratio = (1 - len(compressed) / max(len(data), 1)) * 100

        # 加密
        if self.key:
            compressed = crypto.encrypt(compressed, self.key)

        preview = text[:50].replace("\n", "\\n")
        self._progress(f"📤 发送文本: \"{preview}{'...' if len(text)>50 else ''}\"")
        ok = self._send_with_retry(protocol.MSG_TEXT, compressed,
                                    description=f"文本 (-{ratio:.0f}%)")
        if ok:
            self._progress("")
        return ok

    def send_image(self, dib_data: bytes) -> bool:
        """发送图片到对端（zlib 压缩 + 加密）"""
        compressed = zlib.compress(dib_data)
        ratio = (1 - len(compressed) / max(len(dib_data), 1)) * 100

        if self.key:
            compressed = crypto.encrypt(compressed, self.key)

        self._progress(f"📤 发送图片 ({len(dib_data)}→{len(compressed)} 字节)")
        ok = self._send_with_retry(protocol.MSG_IMAGE, compressed,
                                    description=f"图片 (-{ratio:.0f}%)")
        if ok:
            self._progress("")
        return ok

    def send_files(self, file_paths):
        """发送文件/文件夹到对端（流式，递归展开目录）"""
        addr = self._get_connected_peer()
        if not addr:
            return False
        peer_ip, peer_port = addr

        # ── 展开所有路径：文件直接加入，递归展开目录 ──
        expanded = []  # [(abs_path, relpath)]
        for fp in file_paths:
            fp = os.path.normpath(fp)
            if os.path.isdir(fp):
                parent = os.path.dirname(fp)  # relpath 相对于父目录
                for root, dirs, files in os.walk(fp):
                    for fname in files:
                        full = os.path.join(root, fname)
                        rel = os.path.relpath(full, parent)
                        expanded.append((full, rel))
            elif os.path.isfile(fp):
                expanded.append((fp, os.path.basename(fp)))
            else:
                self._log(f"⚠️ 跳过不存在的路径: {fp}")

        if not expanded:
            self._log("❌ 没有可发送的文件（可能是空文件夹）")
            return False

        self._progress(f"📤 准备发送 {len(expanded)} 个文件")
        total_size_all = sum(os.path.getsize(full) for full, _ in expanded)

        for attempt in range(1, config.SEND_RETRY_MAX + 1):
            if not self.running or not self._connected_peer_ip:
                self._log("🛑 已中断进行中的文件传输")
                return False

            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(config.SOCKET_TIMEOUT)
                sock.connect((peer_ip, peer_port))

                # ── 流式加密（如配置了密钥） ──
                file_enc = None
                if self.key:
                    file_enc = crypto.FileEncryptor(self.key)

                # 构建文件元数据（不带 MD5，改为流式边发边算）
                files_meta = []
                for full, rel in expanded:
                    fsize = os.path.getsize(full)
                    files_meta.append({
                        "name": os.path.basename(full),
                        "size": fsize,
                        "relpath": rel,
                        "md5": "",  # 占位，文件发完后通过 MSG_FILE_CHECK 发送
                    })

                meta_dict = {"files": files_meta}
                if file_enc:
                    meta_dict["enc_salt"] = file_enc.salt.hex()
                meta_bytes = json.dumps(meta_dict).encode("utf-8")

                # 先发文件头
                protocol.send_msg(sock, protocol.MSG_FILES, meta_bytes)

                # 再发文件内容（chunk 循环中检查 running 以支持中断，同时增量计算 MD5）
                total_sent = 0
                _speed_start = time.time()
                _speed_sent = 0
                file_md5s = {}  # relpath → md5（边发边算）
                for full, rel in expanded:
                    if not self.running or not self._connected_peer_ip:
                        self._log("🛑 已中断进行中的文件传输")
                        return False
                    fsize = os.path.getsize(full)
                    remaining = fsize
                    md5_hash = hashlib.md5()
                    with open(full, "rb") as f:
                        while remaining > 0:
                            if not self.running or not self._connected_peer_ip:
                                self._log("🛑 已中断进行中的文件传输")
                                return False
                            chunk_size = min(config.CHUNK_SIZE, remaining)
                            chunk = f.read(chunk_size)
                            if not chunk:
                                break
                            md5_hash.update(chunk)               # MD5 of original content
                            if file_enc:
                                chunk = file_enc.encrypt_chunk(chunk)
                            sock.sendall(chunk)
                            remaining -= len(chunk)
                            total_sent += len(chunk)
                            _speed_sent += len(chunk)
                            pct = total_sent * 100 // total_size_all if total_size_all else 100

                            # 每秒更新一次速度
                            elapsed = time.time() - _speed_start
                            if elapsed >= 1.0 or remaining <= 0:
                                speed = _speed_sent / elapsed / (1024 * 1024) if elapsed > 0 else 0
                                _speed_start = time.time()
                                _speed_sent = 0
                            else:
                                speed = None

                            if speed is not None:
                                self._progress(f"📤 发送文件... {pct}% ({speed:.1f} MB/s)")
                            else:
                                self._progress(f"📤 发送文件... {pct}%")
                    file_md5s[rel] = md5_hash.hexdigest()

                # 等待文件 ACK
                file_ack = protocol.recv_msg(sock)

                if file_ack and file_ack[0] == protocol.MSG_ACK:
                    # 文件已接收完毕，发送完整性校验
                    check_dict = {
                        "files": [{"relpath": rel, "md5": md5}
                                  for rel, md5 in file_md5s.items()]
                    }
                    if file_enc:
                        check_dict["enc_hmac"] = file_enc.finalize().hex()
                    check_data = json.dumps(check_dict).encode("utf-8")
                    protocol.send_msg(sock, protocol.MSG_FILE_CHECK, check_data)

                    # 等待校验 ACK
                    check_ack = protocol.recv_msg(sock)
                    if check_ack and check_ack[0] == protocol.MSG_ACK:
                        self._log(f"✅ 已发送 {len(expanded)} 个文件 (MD5 校验完成)")
                    else:
                        self._log(f"⚠️ 文件已发送，MD5 校验未确认")
                    self._progress("")
                    return True
                else:
                    self._log("⚠️ 文件发送未收到确认")
                    return False

            except (socket.timeout, ConnectionError, OSError) as e:
                self._log(f"⏳ 文件发送失败 (第{attempt}次): {e}")
                if attempt < config.SEND_RETRY_MAX:
                    time.sleep(2 ** attempt)
                continue
            finally:
                try:
                    if sock:
                        sock.close()
                except Exception:
                    pass

        self._log(f"❌ 文件发送失败，已重试 {config.SEND_RETRY_MAX} 次")
        return False

    def request_fetch(self) -> bool:
        """请求对端发送当前剪贴板内容"""
        addr = self._get_connected_peer()
        if not addr:
            return False
        peer_ip, peer_port = addr

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)  # 连接阶段 5s 超时（快速反馈错误 IP）
            sock.connect((peer_ip, peer_port))
            sock.settimeout(config.SOCKET_TIMEOUT)  # 数据传输恢复 30s
            protocol.send_msg(sock, protocol.MSG_FETCH_REQUEST)
            sock.close()
            self._log("📤 已发送剪贴板获取请求")
            return True
        except Exception as e:
            self._log(f"❌ 获取请求失败: {e}")
            return False

    def respond_fetch(self):
        """响应获取请求：读取本机剪贴板并发送给对端"""
        # 文件优先
        files = clipboard.get_files()
        if files:
            self._log(f"📤 响应获取: 发送 {len(files)} 个文件")
            self.send_files(files)
            return
        img = clipboard.get_image()
        if img:
            self._log(f"📤 响应获取: 发送图片 ({len(img)} 字节)")
            self.send_image(img)
            return
        text = clipboard.get_text()
        if text:
            self._log(f"📤 响应获取: 发送文本")
            self.send_text(text)
            return
        self._log("⚠️ 本机剪贴板为空，无法响应获取请求")
