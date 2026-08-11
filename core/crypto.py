# -*- coding: utf-8 -*-
"""
AES-256-GCM 加密模块

统一格式：nonce(12) + tag(16) + ciphertext
密钥从 DEMO_KEY 环境变量读取（hex 64 位 = 32 字节）
"""
import json
import os
import base64

from Crypto.Cipher import AES

import config


class CryptoError(Exception):
    pass


def _get_key():
    if not config.DEMO_KEY:
        raise CryptoError("DEMO_KEY 未配置")
    try:
        return bytes.fromhex(config.DEMO_KEY)
    except ValueError as e:
        raise CryptoError(f"DEMO_KEY 不是合法 hex: {e}") from e


def encrypt_bytes(data: bytes) -> bytes:
    """加密字节流，返回 nonce(12) + tag(16) + ciphertext"""
    key = _get_key()
    nonce = os.urandom(12)  # 12字节nonce（标准GCM，pycryptodome默认16字节会错位）
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(data)
    return nonce + tag + ct


def decrypt_bytes(blob: bytes) -> bytes:
    """解密字节流"""
    if not blob or len(blob) < 28:
        raise CryptoError("密文长度非法")
    key = _get_key()
    nonce, tag, ct = blob[:12], blob[12:28], blob[28:]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    try:
        return cipher.decrypt_and_verify(ct, tag)
    except ValueError as e:
        raise CryptoError(f"解密失败: {e}") from e


def encrypt_json(obj) -> bytes:
    return encrypt_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def decrypt_json(blob: bytes):
    return json.loads(decrypt_bytes(blob).decode("utf-8"))
