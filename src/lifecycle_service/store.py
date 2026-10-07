"""事件存储：只追加的 JSONL 日志。

耐久性：每次追加 flush + fsync，进程崩溃/被 kill -9 后已确认事件不丢；
服务重启通过 replay 重建全部投影，未关闭工单自然恢复。

完整性：重放时逐条校验 SHA-256 哈希链，链断立即报 LogIntegrityError，
避免在被篡改/截断的日志上给出服务记录。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .errors import LogIntegrityError
from .events import Event, GENESIS


class EventLog:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0
        self._prev_hash = GENESIS
        if self.path.exists():
            # 启动即重放，确定尾指针；链断裂直接失败（fail-closed）。
            for event in self._read_unlocked():
                self._seq = event.seq
                self._prev_hash = event.hash

    # -- 读 ----------------------------------------------------------------

    def _read_unlocked(self):
        with self.path.open("r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = Event.from_json(line)
                except json.JSONDecodeError as exc:
                    raise LogIntegrityError(
                        f"日志第 {lineno} 行无法解析", broken_seq=lineno
                    ) from exc
                if event.seq != lineno:
                    raise LogIntegrityError(
                        f"日志第 {lineno} 行序号异常：{event.seq}", broken_seq=event.seq
                    )
                if not event.verify(self._prev_hash):
                    raise LogIntegrityError(
                        f"日志哈希链在第 {lineno} 行断裂", broken_seq=event.seq
                    )
                self._prev_hash = event.hash
                yield event

    def read_all(self) -> list[Event]:
        with self._lock:
            if not self.path.exists():
                return []
            prev = GENESIS
            events: list[Event] = []
            with self.path.open("r", encoding="utf-8") as handle:
                for lineno, line in enumerate(handle, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    event = Event.from_json(line)
                    if not event.verify(prev):
                        raise LogIntegrityError(
                            f"日志哈希链在第 {lineno} 行断裂", broken_seq=event.seq
                        )
                    prev = event.hash
                    events.append(event)
            return events

    # -- 写 ----------------------------------------------------------------

    def append(
        self,
        event_type: str,
        aggregate_id: str,
        occurred_at: str,
        operator: str,
        payload: dict,
        backfill: bool = False,
    ) -> Event:
        """串行化追加；调用方在更大的锁内完成“校验+追加”以保证原子性。"""
        with self._lock:
            event = Event.create(
                seq=self._seq + 1,
                event_type=event_type,
                aggregate_id=aggregate_id,
                occurred_at=occurred_at,
                operator=operator,
                payload=payload,
                prev_hash=self._prev_hash,
                backfill=backfill,
            )
            line = event.to_json() + "\n"
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            self._seq = event.seq
            self._prev_hash = event.hash
            return event

    @property
    def sequence(self) -> int:
        return self._seq
