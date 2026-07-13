"""
加密工具 — 共享密钥 + XOR 流加密 + HMAC 完整性校验

输出格式（encrypt）：
  salt(16B) + cipher(NB) + hmac(32B)

接收端先验 HMAC 再解密，防止主动篡改和比特翻转。
"""

import hashlib
import hmac as hmac_module
import os

_SALT_LEN = 16
_HMAC_LEN = 32          # SHA-256 → 32 字节
_DERIVE_ITER = 10000


def _derive_key(password: str, salt: bytes) -> bytes:
    """简单密钥派生（等效 PBKDF2-HMAC-SHA256，不依赖外部库）"""
    key = password.encode("utf-8")
    for _ in range(_DERIVE_ITER):
        key = hashlib.sha256(key + salt).digest()
    return key


def make_fingerprint(shared_key: str) -> str:
    """生成广播用的指纹（sha256 的前 8 位 hex），用于握手时初步校验"""
    return hashlib.sha256(shared_key.encode("utf-8")).hexdigest()[:8]


def verify_fingerprint(shared_key: str, fingerprint: str) -> bool:
    """验证对端广播中的指纹是否匹配"""
    return make_fingerprint(shared_key) == fingerprint


def _xor_stream(data: bytes, key: bytes) -> bytes:
    """基于 SHA-256 计数器模式的 XOR 流加密/解密"""
    keystream = b""
    counter = 0
    while len(keystream) < len(data):
        keystream += hashlib.sha256(key + counter.to_bytes(4, "big")).digest()
        counter += 1
    keystream = keystream[:len(data)]
    return bytes(a ^ b for a, b in zip(data, keystream))


class _XORStream:
    """增量 XOR 流 — 保持内部计数器，支持分块调用"""
    def __init__(self, key: bytes):
        self._key = key
        self._counter = 0

    def apply(self, data: bytes) -> bytes:
        keystream = b""
        while len(keystream) < len(data):
            keystream += hashlib.sha256(self._key + self._counter.to_bytes(4, "big")).digest()
            self._counter += 1
        keystream = keystream[:len(data)]
        return bytes(a ^ b for a, b in zip(data, keystream))


class FileEncryptor:
    """流式文件加密器 — 逐 chunk 加密 + 发送端 HMAC 累加

    用法：
        enc = FileEncryptor(shared_key)
        # enc.salt → 放入元数据
        for chunk in file_chunks:
            sock.sendall(enc.encrypt_chunk(chunk))
        hmac_tag = enc.finalize()  # 发送给接收端校验
    """

    def __init__(self, shared_key: str):
        self.salt = os.urandom(_SALT_LEN)
        self._key = _derive_key(shared_key, self.salt)
        self._x = _XORStream(self._key)
        self._hmac_ctx = hmac_module.new(self._key, digestmod="sha256")

    def encrypt_chunk(self, plain: bytes) -> bytes:
        """加密一个 chunk，同时更新 HMAC"""
        cipher = self._x.apply(plain)
        self._hmac_ctx.update(cipher)
        return cipher

    def finalize(self) -> bytes:
        """所有 chunk 加密完毕，返回 HMAC 校验值 (32B)"""
        return self._hmac_ctx.digest()


class FileDecryptor:
    """流式文件解密器 — 逐 chunk 解密 + 接收端 HMAC 累加

    用法：
        dec = FileDecryptor(shared_key, salt)
        for encrypted_chunk in recv_chunks:
            plain = dec.decrypt_chunk(encrypted_chunk)
            f.write(plain)
        ok = dec.verify(hmac_tag)  # 收到发送端的 HMAC 后校验
    """

    def __init__(self, shared_key: str, salt: bytes):
        self._key = _derive_key(shared_key, salt)
        self._x = _XORStream(self._key)
        self._hmac_ctx = hmac_module.new(self._key, digestmod="sha256")

    def decrypt_chunk(self, cipher: bytes) -> bytes:
        """解密一个 chunk，同时更新 HMAC"""
        self._hmac_ctx.update(cipher)
        return self._x.apply(cipher)

    def verify(self, hmac_tag: bytes) -> bool:
        """校验所有收到数据的 HMAC 完整性"""
        expected = self._hmac_ctx.digest()
        return hmac_module.compare_digest(expected, hmac_tag)


def encrypt(data: bytes, shared_key: str) -> bytes:
    """
    加密 + 签名：salt + cipher + hmac

    - salt 随机生成，每次加密不同
    - cipher = XOR 流加密
    - hmac = HMAC-SHA256(cipher, derived_key)，确保 cipher 未被篡改
    """
    salt = os.urandom(_SALT_LEN)
    key = _derive_key(shared_key, salt)
    cipher = _xor_stream(data, key)
    sig = hmac_module.new(key, cipher, "sha256").digest()
    return salt + cipher + sig


def decrypt(data: bytes, shared_key: str) -> bytes:
    """
    验证 + 解密

    先验 HMAC，如果数据被篡改过则抛 ValueError。
    """
    if len(data) < _SALT_LEN + _HMAC_LEN:
        raise ValueError("数据太短，无法解密")
    salt = data[:_SALT_LEN]
    cipher = data[_SALT_LEN:-_HMAC_LEN]
    received_sig = data[-_HMAC_LEN:]
    key = _derive_key(shared_key, salt)
    expected_sig = hmac_module.new(key, cipher, "sha256").digest()
    if not hmac_module.compare_digest(expected_sig, received_sig):
        raise ValueError("HMAC 校验失败，数据可能被篡改")
    return _xor_stream(cipher, key)
