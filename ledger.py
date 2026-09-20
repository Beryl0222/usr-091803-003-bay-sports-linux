"""只追加事件台账。

所有领域状态变化都以事件形式追加到本台账：事件一旦写入即不可修改、不可删除
（裁判原始记录与送达回执因此天然可追溯）。台账支持：

- 稳定递增的序列号与双时间戳（事件发生时间 occurred_at / 登记入库时间 ingested_at），
  离线设备补传时两者可能相差数小时；
- 幂等键：现场设备生成的登记号补传多次也只会产生一条事件；
- 按聚合对象与截止时间读取，支撑“赛后回到任意时点”的快照查询；
- JSON 落盘，便于本地联调与运维巡检。
"""

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


class IdempotentReplay(Exception):
    """同一幂等键再次提交时抛出，携带首次写入的事件。"""

    def __init__(self, original: "Event"):
        super().__init__(f"幂等键 {original.idempotency_key} 已登记")
        self.original = original


def parse_ts(value: str) -> datetime:
    """解析 ISO8601 时间，兼容结尾的 Z。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Event:
    seq: int
    event_id: str
    type: str
    occurred_at: str  # 业务事件实际发生时间（离线补传时早于入库时间）
    ingested_at: str  # 台账接收时间
    actor: str
    payload: dict[str, Any]
    idempotency_key: Optional[str] = None
    correlation_id: Optional[str] = None  # 串联申请-批准-进入等同一业务过程

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Ledger:
    """线程安全的只追加事件存储。"""

    def __init__(self, store_path: Optional[str] = None, clock: Callable[[], str] = now_ts):
        self._events: list[Event] = []
        self._idempotency: dict[str, Event] = {}
        self._lock = threading.RLock()
        self._clock = clock
        self.store_path = Path(store_path) if store_path else None
        if self.store_path and self.store_path.exists():
            self._load()

    def append(
        self,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
        *,
        occurred_at: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> Event:
        with self._lock:
            if idempotency_key is not None and idempotency_key in self._idempotency:
                raise IdempotentReplay(self._idempotency[idempotency_key])
            event = Event(
                seq=len(self._events) + 1,
                event_id=f"evt-{len(self._events) + 1:06d}",
                type=event_type,
                occurred_at=occurred_at or self._clock(),
                ingested_at=self._clock(),
                actor=actor,
                payload=payload,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
            )
            self._events.append(event)
            if idempotency_key is not None:
                self._idempotency[idempotency_key] = event
            if self.store_path:
                self._save()
            return event

    def events(
        self,
        *,
        type_in: Optional[set[str]] = None,
        aggregate_field: Optional[tuple[str, str]] = None,
        at: Optional[str] = None,
    ) -> list[Event]:
        """按条件读取事件；at 给出时只包含发生时间不晚于该时刻的事件。"""
        cutoff = parse_ts(at) if at else None
        result = []
        with self._lock:
            for event in self._events:
                if type_in and event.type not in type_in:
                    continue
                if aggregate_field and event.payload.get(aggregate_field[0]) != aggregate_field[1]:
                    continue
                if cutoff and parse_ts(event.occurred_at) > cutoff:
                    continue
                result.append(event)
        return result

    def all_events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def find_idempotent(self, key: str) -> Optional[Event]:
        with self._lock:
            return self._idempotency.get(key)

    def _save(self) -> None:
        assert self.store_path is not None
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([e.to_dict() for e in self._events], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.store_path)

    def _load(self) -> None:
        assert self.store_path is not None
        raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        self._events = [Event(**item) for item in raw]
        self._idempotency = {
            e.idempotency_key: e for e in self._events if e.idempotency_key is not None
        }
