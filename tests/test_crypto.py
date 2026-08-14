# -*- coding: utf-8 -*-
"""
AES-256-GCM 加密模块测试

测试：加解密往返、非法密文、篡改检测、常量值
"""
import os
import sys

# 设置测试环境变量（必须在 import config 之前）
os.environ["DEMO_KEY"] = "0a7deb9b0978e05d5a1ffe405ece28fa2158360c54ff2cf5847b3e8392e3069a"
os.environ.setdefault("EXEC_TOKEN", "test-token")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from core import crypto


def test_encrypt_decrypt_bytes():
    """AES 加解密往返"""
    data = b"hello world"
    encrypted = crypto.encrypt_bytes(data)
    assert encrypted != data
    assert len(encrypted) > len(data)  # 有 nonce + tag 开销
    decrypted = crypto.decrypt_bytes(encrypted)
    assert decrypted == data


def test_encrypt_decrypt_large():
    """大数据加解密"""
    data = os.urandom(1024 * 1024)  # 1MB
    encrypted = crypto.encrypt_bytes(data)
    decrypted = crypto.decrypt_bytes(encrypted)
    assert decrypted == data


def test_encrypt_decrypt_json():
    """JSON 加解密"""
    obj = {"key": "value", "list": [1, 2, 3], "nested": {"a": True}}
    encrypted = crypto.encrypt_json(obj)
    decrypted = crypto.decrypt_json(encrypted)
    assert decrypted == obj


def test_decrypt_empty_blob():
    """空密文应抛 CryptoError"""
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_bytes(b"")
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_bytes(None)


def test_decrypt_short_blob():
    """过短密文应抛 CryptoError"""
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_bytes(b"short")


def test_decrypt_tampered():
    """篡改密文应抛 CryptoError（GCM 认证标签校验）"""
    data = b"sensitive data"
    encrypted = crypto.encrypt_bytes(data)
    # 翻转最后一个字节
    tampered = encrypted[:-1] + bytes([encrypted[-1] ^ 1])
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_bytes(tampered)


def test_header_constants():
    """常量值校验"""
    assert crypto.NONCE_LEN == 12
    assert crypto.TAG_LEN == 16
    assert crypto.HEADER_LEN == 28
