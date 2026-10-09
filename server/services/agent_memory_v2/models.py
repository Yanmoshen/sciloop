"""记忆记录的存储信封（计划书 WP-07）。

为什么需要信封：计划书要求记录带「创建者、置信度、内容哈希」，而
``contracts.agent_v2.MemoryRecord`` 已在 ``agent-v2-contract-v1`` 冻结
（字段增加须走新版本）。所以在**存储层**加一层信封：

    {"envelope": 1, "record": <契约 MemoryRecord 的 to_dict()>, "creator": …, "confidence": …, "content_hash": …, "revision": n}

这样契约模型保持原样、Agent 2/3 读到的仍是标准 ``MemoryRecord``，
而落盘又满足计划书的字段要求。信封是本地存储格式，不属于跨 Agent 契约。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from contracts.agent_v2.models import MemoryRecord

ENVELOPE_VERSION = 1


def content_hash(text: str) -> str:
    """内容哈希：用于查重与篡改检测（取 sha256 前 16 位十六进制）。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


@dataclass
class MemoryEnvelope:
    """一条记忆修订的落盘单元（append-only，多行构成一条记忆的历史）。"""

    record: MemoryRecord
    creator: str = "system"
    confidence: float = 0.5
    #: 内容哈希（写入时按 text 计算，读取时校验）
    content_hash: str = ""
    #: 该记忆的第几次修订（1 起）
    revision: int = 1
    #: 本行在文件中的行号（1 起），用于审计定位
    line_no: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = content_hash(self.record.text)
        self.confidence = max(0.0, min(1.0, float(self.confidence)))

    @property
    def memory_id(self) -> str:
        return self.record.memory_id

    @property
    def intact(self) -> bool:
        """内容哈希是否与文本一致（不一致说明文件被手改）。"""
        return self.content_hash == content_hash(self.record.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "envelope": ENVELOPE_VERSION,
            "record": self.record.to_dict(),
            "creator": self.creator,
            "confidence": self.confidence,
            "content_hash": self.content_hash,
            "revision": self.revision,
            **({"extra": self.extra} if self.extra else {}),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, line_no: int = 0) -> MemoryEnvelope:
        if not isinstance(raw, dict) or "record" not in raw:
            raise ValueError("memory envelope must contain a 'record' object")
        return cls(
            record=MemoryRecord.from_dict(raw["record"]),
            creator=str(raw.get("creator", "system")),
            confidence=float(raw.get("confidence", 0.5)),
            content_hash=str(raw.get("content_hash", "")),
            revision=int(raw.get("revision", 1)),
            line_no=line_no,
            extra=dict(raw.get("extra") or {}),
        )


__all__ = ["ENVELOPE_VERSION", "MemoryEnvelope", "content_hash"]
