# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""API Key 的加密存储、脱敏展示与指纹对账。

三条硬约束（contracts.forbidden_actions 第 4 条）：

1. **禁止明文落库**：``model_configs.api_key_enc`` 只存密文或 ``env:VAR`` 引用
2. **禁止接口回传明文**：所有响应只出 :func:`mask_api_key` 的结果
3. **禁止写入前端包**：前端只能拿到脱敏值与「是否已配置」布尔量

密文形态 ``fernet:v1:<token>``，密钥由 ``APP_SECRET_KEY`` 经 PBKDF2-HMAC-SHA256 派生，
因此换 ``APP_SECRET_KEY`` 会导致旧密文不可解（预期行为，需重新填 Key）。
"""

from __future__ import annotations

import base64
import hashlib
import logging
from functools import lru_cache

from app.llm.errors import SecretBackendUnavailable

logger = logging.getLogger(__name__)

ENV_REF_PREFIX = "env:"
_CIPHER_PREFIX = "fernet:v1:"
_PBKDF2_ITERATIONS = 200_000
_SALT = b"sciloop-llm-secret-v1"


def mask_api_key(raw: str | None) -> str:
    """脱敏展示：保留前 4 与后 4 位，中间以 ``***`` 代替。"""
    if not raw:
        return ""
    text = str(raw)
    if text.startswith(ENV_REF_PREFIX):
        # 环境变量引用本身不含密钥，可原样展示（前端需要知道去哪配）
        return text
    if text.startswith(_CIPHER_PREFIX):
        return _CIPHER_PREFIX + "***"
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}***{text[-4:]}"


def is_encrypted(value: str | None) -> bool:
    return bool(value) and str(value).startswith(_CIPHER_PREFIX)


def is_env_ref(value: str | None) -> bool:
    return bool(value) and str(value).startswith(ENV_REF_PREFIX)


def env_ref_name(value: str) -> str:
    return str(value)[len(ENV_REF_PREFIX) :].strip()


def key_fingerprint(raw: str | None) -> str | None:
    """密钥指纹（sha256 前 12 位），用于「是否换过 Key」对账，不可逆推密钥。"""
    if not raw:
        return None
    return hashlib.sha256(str(raw).encode("utf-8")).hexdigest()[:12]


@lru_cache(maxsize=4)
def _fernet(secret: str) -> object:
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出可执行提示
        raise SecretBackendUnavailable(
            "缺少 cryptography 依赖，无法加密存储 API Key。"
            "请在 backend/pyproject.toml 增加 cryptography，或在设置页使用 env:变量名 引用方式。",
            detail={"missing": "cryptography"},
        ) from exc
    key = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), _SALT, _PBKDF2_ITERATIONS, dklen=32)
    return Fernet(base64.urlsafe_b64encode(key))


def _app_secret() -> str:
    try:
        from app.core.config import get_settings
    except Exception:  # pragma: no cover - 单独跑脚本时
        import os

        return os.environ.get("APP_SECRET_KEY", "change_me")
    return get_settings().app_secret_key


def encrypt_api_key(plain: str, *, secret: str | None = None) -> str:
    """加密 API Key。已是 ``env:VAR`` 引用时原样返回（引用不是秘密）。"""
    if not plain:
        raise ValueError("api_key 不能为空")
    text = str(plain).strip()
    if text.startswith(ENV_REF_PREFIX):
        return text
    fernet = _fernet(secret or _app_secret())
    return _CIPHER_PREFIX + fernet.encrypt(text.encode("utf-8")).decode("ascii")  # type: ignore[attr-defined]


def decrypt_api_key(stored: str | None) -> str:
    """解密（或解析 ``env:VAR`` 引用）。

    返回空串表示「未配置」；解析失败返回空串并记 error 日志，**不抛错**——
    一个坏 Key 不应导致整个设置页 500。
    """
    import os

    if not stored:
        return ""
    text = str(stored)
    if text.startswith(ENV_REF_PREFIX):
        return os.environ.get(env_ref_name(text), "")
    if text.startswith(_CIPHER_PREFIX):
        token = text[len(_CIPHER_PREFIX) :]
        try:
            fernet = _fernet(_app_secret())
            return fernet.decrypt(token.encode("ascii")).decode("utf-8")  # type: ignore[attr-defined]
        except SecretBackendUnavailable:
            raise
        except Exception:  # noqa: BLE001 - 密钥轮换/密文损坏
            logger.error(
                "API Key 解密失败（可能是 APP_SECRET_KEY 变更）；请在设置页重新填写该供应商 Key"
            )
            return ""
    # 历史脏数据：非预期格式一律视为不可用，绝不当明文使用
    logger.error("api_key_enc 格式非法（既非密文也非 env 引用），已忽略")
    return ""


__all__ = [
    "ENV_REF_PREFIX",
    "decrypt_api_key",
    "encrypt_api_key",
    "env_ref_name",
    "is_encrypted",
    "is_env_ref",
    "key_fingerprint",
    "mask_api_key",
]
