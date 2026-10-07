"""不可变事件定义。

事件是系统唯一的事实来源（event sourcing）。每条事件携带：
- seq：日志内全局递增序号
- event_id / event_type
- aggregate_id / aggregate_type
- occurred_at：业务时间（现场发生时刻，允许补录过去）
- recorded_at：系统入库时间（恒为追加时的 UTC 时间）
- operator / backfill
- payload：事件专属数据
- prev_hash / hash：SHA-256 哈希链，重启校验可发现篡改或丢失
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    """宽容解析 ISO8601；无时区按 UTC 处理。"""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def normalize_iso(value: str) -> str:
    return parse_iso(value).isoformat()


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------

GENESIS = "0" * 64


@dataclass(frozen=True)
class Event:
    seq: int
    event_id: str
    event_type: str
    aggregate_id: str
    occurred_at: str
    recorded_at: str
    operator: str
    payload: dict[str, Any] = field(default_factory=dict)
    backfill: bool = False
    prev_hash: str = GENESIS
    hash: str = ""

    @staticmethod
    def compute_hash(
        seq: int,
        event_id: str,
        event_type: str,
        aggregate_id: str,
        occurred_at: str,
        recorded_at: str,
        operator: str,
        payload: dict[str, Any],
        backfill: bool,
        prev_hash: str,
    ) -> str:
        body = json.dumps(
            {
                "seq": seq,
                "event_id": event_id,
                "event_type": event_type,
                "aggregate_id": aggregate_id,
                "occurred_at": occurred_at,
                "recorded_at": recorded_at,
                "operator": operator,
                "payload": payload,
                "backfill": backfill,
                "prev_hash": prev_hash,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    @classmethod
    def create(
        cls,
        seq: int,
        event_type: str,
        aggregate_id: str,
        occurred_at: str,
        operator: str,
        payload: dict[str, Any],
        prev_hash: str,
        event_id: str | None = None,
        recorded_at: str | None = None,
        backfill: bool = False,
    ) -> "Event":
        import uuid

        eid = event_id or f"evt_{uuid.uuid4().hex[:16]}"
        rec = recorded_at or utc_now_iso()
        occ = normalize_iso(occurred_at)
        digest = cls.compute_hash(
            seq, eid, event_type, aggregate_id, occ, rec, operator, payload, backfill, prev_hash
        )
        return cls(seq, eid, event_type, aggregate_id, occ, rec, operator,
                   dict(payload), backfill, prev_hash, digest)

    def verify(self, expected_prev: str) -> bool:
        return self.prev_hash == expected_prev and self.hash == Event.compute_hash(
            self.seq, self.event_id, self.event_type, self.aggregate_id,
            self.occurred_at, self.recorded_at, self.operator, self.payload,
            self.backfill, self.prev_hash,
        )

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, line: str) -> "Event":
        data = json.loads(line)
        return cls(**data)
