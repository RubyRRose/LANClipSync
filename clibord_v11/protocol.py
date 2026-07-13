"""
协议常量 + 网络工具函数

消息头格式（16 字节）：
  4B magic (LC6\n) + 4B 消息类型 + 8B payload 长度
"""

import struct
import socket
from . import config


# ── 消息类型 ──
MSG_TEXT = 1          # 文本
MSG_IMAGE = 2         # 图片（DIB + zlib 压缩）
MSG_FILES = 3         # 文件
MSG_FETCH_REQUEST = 4 # 手动拉取请求
MSG_ACK = 5           # 接收确认
MSG_FILE_CHECK = 6    # 文件完整性校验

# ── 消息头 ──
# 16 字节：4B magic + 4B 消息类型 + 8B payload 长度
HEADER_FORMAT = "!4sIQ"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # = 16

# 有效消息类型集合（用于 header 校验）
_VALID_TYPES = {MSG_TEXT, MSG_IMAGE, MSG_FILES, MSG_FETCH_REQUEST, MSG_ACK, MSG_FILE_CHECK}


def pack_header(msg_type: int, data_len: int) -> bytes:
    """打包消息头"""
    return struct.pack(HEADER_FORMAT, config.PROTOCOL_MAGIC, msg_type, data_len)


def unpack_header(data: bytes) -> tuple:
    """解包消息头，返回 (magic, msg_type, data_len)"""
    return struct.unpack(HEADER_FORMAT, data)


def validate_header(magic: bytes, msg_type: int, data_len: int) -> bool:
    """校验消息头是否合法"""
    return (magic == config.PROTOCOL_MAGIC
            and msg_type in _VALID_TYPES
            and 0 <= data_len <= config.MAX_PAYLOAD_SIZE)


def recv_exact(sock: socket.socket, n: int):
    """精确接收 n 字节，失败或断连返回 None"""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(min(65536, n - len(buf)))
        except (socket.timeout, ConnectionError, OSError):
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def recv_msg(sock: socket.socket):
    """
    接收一条完整消息，返回 (msg_type, payload) 或 None。

    header 校验失败（magic 不对、类型非法、数据超长）时
    返回 None，上层会自动关闭连接并触发发送端重试。
    """
    header = recv_exact(sock, HEADER_SIZE)
    if not header:
        return None
    magic, msg_type, data_len = unpack_header(header)
    if not validate_header(magic, msg_type, data_len):
        return None
    if data_len > 0:
        payload = recv_exact(sock, data_len)
        if not payload:
            return None
    else:
        payload = b""
    return msg_type, payload


def send_msg(sock: socket.socket, msg_type: int, payload: bytes = b"") -> bool:
    """发送一条完整消息，成功返回 True"""
    try:
        sock.sendall(pack_header(msg_type, len(payload)) + payload)
        return True
    except (socket.timeout, ConnectionError, OSError):
        return False
