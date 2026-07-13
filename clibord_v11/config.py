"""
配置管理 — 所有可调参数集中管理，不散落在各模块中
"""

# ── 端口 ──
UDP_PORT = 37020                # UDP 广播发现端口
TCP_PORT = 37021                # TCP 数据传输起始端口
TCP_PORT_RETRY_MAX = 10         # 端口被占用时最多 +1 重试次数

# ── 网络 ──
BROADCAST_INTERVAL = 0.5        # UDP 广播间隔（秒）
PEER_TIMEOUT = 15.0             # 超过此秒数未收到心跳则认为对端离线
PEER_CLEANUP_INTERVAL = 5.0     # 清理离线 peer 的间隔（秒）
SOCKET_TIMEOUT = 30.0           # TCP recv/send 超时（秒）
ACK_TIMEOUT = 5.0               # 等待 ACK 响应的超时（秒）
SEND_RETRY_MAX = 3              # 发送失败最大重试次数

# ── 剪贴板 ──
TEXT_SIZE_WARN = 10 * 1024 * 1024      # 文本 >= 此值则日志警告（10MB）
TEXT_SIZE_LIMIT = 50 * 1024 * 1024     # 文本 > 此值则拒绝传输（50MB）
SUPPRESS_SECONDS = 2.0                 # 收到远端数据后抑制本机检测的秒数
CLIPBOARD_POLL_FALLBACK = 1.0          # 监听器不可用时的回退轮询间隔（秒）

# ── 文件传输 ──
CHUNK_SIZE = 262144                     # 文件分块大小 256KB
# 接收文件缓存目录（取代 tempfile.mkdtemp，避免临时目录堆积）
# 启动时会清理其中超过 CACHE_FILE_MAX_AGE 秒的文件
RECEIVE_CACHE_DIR = "LANClipSync_cache"
CACHE_FILE_MAX_AGE = 600                # 缓存文件保留 10 分钟供粘贴用

# ── 协议标识 ──
PROTOCOL_MAGIC = b"LC6\n"               # 消息头 magic（4 字节）
VERSION = "2.0"
MAX_PAYLOAD_SIZE = 100 * 1024 * 1024     # 单条消息 payload 上限（100MB）

# ── 加密 ──
KEY_MIN_LENGTH = 8
KEY_MAX_LENGTH = 32

# ── 界面 ──
HISTORY_TEXT_MAX = 50              # 文本历史最多保留条数（存内存）
HISTORY_IMAGE_MAX = 10             # 图片历史最多保留条数（存磁盘路径）
HISTORY_FILES_MAX = 20             # 文件历史最多保留条数（存路径列表）
KEEP_LOG_DAYS = 30                 # 日志文件保留天数

# ── 设备 ──
DEVICE_NAME = ""                   # 设备别名（空 = 使用主机名）
