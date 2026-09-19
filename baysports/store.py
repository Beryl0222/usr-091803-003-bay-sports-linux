"""只追加事件库。

设计原则：
- append 即登记：事件一经追加不可修改、不可删除（"不抹去裁判原始记录"）。
- 每次追加返回单调递增版本号 seq；任何状态修正都体现为"新版本事件"。
- 支持幂等键 registration_no：现场设备失联补传时，同一登记编号只产生一条事件；
  重复提交内容一致则回放原结果，内容不一致则拒绝（防止补传覆盖首次登记）。
- 事件本身携带 occurred_at（业务发生时间，可为离线补录的现场时间）
  与 recorded_at（系统接收时间），二者都保留。
"""

import json
import os
import threading
from dataclasses import dataclass, field, asdict

from .clock import Clock, parse_iso, to_iso


@dataclass
class Event:
    seq: int
    stream: str  # 聚合流，例如 tournament / game:G1 / person:P1
    etype: str
    data: dict
    occurred_at: str
    recorded_at: str
    actor: str
    registration_no: str | None = None
    basis: dict = field(default_factory=dict)  # 依据（规程版本、条款、证明材料编号）

    def to_dict(self) -> dict:
        return asdict(self)


class EventStore:
    """线程安全的内存只追加日志，可选 JSONL 快照落盘。"""

    def __init__(self, clock: Clock, path: str | None = None):
        self._clock = clock
        self._lock = threading.RLock()
        self._events: list[Event] = []
        self._idempotency: dict[str, int] = {}  # registration_no -> seq
        self.path = path
        if path and os.path.exists(path):
            self._load(path)

    # ---- 写入 ----------------------------------------------------------

    def append(
        self,
        stream: str,
        etype: str,
        data: dict,
        *,
        actor: str,
        occurred_at: str | None = None,
        registration_no: str | None = None,
        basis: dict | None = None,
    ) -> Event:
        """追加事件。

        registration_no 非空时做幂等：
        - 首次：登记并返回新事件；
        - 重复且指纹一致：不新增，返回原事件（调用方据此回放结果）；
        - 重复但指纹不一致：抛 ConflictError，首次登记保持不变。
        """
        from .errors import ConflictError

        recorded_at = self._clock.now_iso()
        occurred_at = occurred_at or recorded_at
        fingerprint = _fingerprint(stream, etype, data)
        with self._lock:
            if registration_no is not None:
                existing_seq = self._idempotency.get(registration_no)
                if existing_seq is not None:
                    existing = self._events[existing_seq - 1]
                    if existing.data.get("_fingerprint") == fingerprint:
                        return existing
                    raise ConflictError(
                        "登记编号已存在但内容与首次登记不一致",
                        registration_no=registration_no,
                        first_recorded_at=existing.recorded_at,
                    )
            event = Event(
                seq=len(self._events) + 1,
                stream=stream,
                etype=etype,
                data={**data, "_fingerprint": fingerprint},
                occurred_at=occurred_at,
                recorded_at=recorded_at,
                actor=actor,
                registration_no=registration_no,
                basis=basis or {},
            )
            self._events.append(event)
            if registration_no is not None:
                self._idempotency[registration_no] = event.seq
            if self.path:
                self._append_disk(event)
            return event

    # ---- 读取 ----------------------------------------------------------

    def replay(
        self,
        stream: str | None = None,
        *,
        as_of: str | None = None,
        include_substreams: bool = False,
    ) -> list[Event]:
        """按序回放事件。

        - stream=None：全局流（赛后按场次归档时使用）；
        - as_of：只包含 occurred_at <= as_of 的事件，得到"当时有效"状态；
        - include_substreams：匹配 stream 前缀（game:G1 下的 record/verification 等）。
        """
        if as_of is not None:
            as_of = to_iso(parse_iso(as_of))
        with self._lock:
            events = list(self._events)
        result = []
        for event in events:
            if stream is not None:
                if include_substreams:
                    if event.stream != stream and not event.stream.startswith(stream + ":"):
                        continue
                elif event.stream != stream:
                    continue
            if as_of is not None and event.occurred_at > as_of:
                continue
            result.append(event)
        return result

    def all(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def version(self) -> int:
        with self._lock:
            return len(self._events)

    # ---- 持久化 --------------------------------------------------------

    def _append_disk(self, event: Event) -> None:
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    def _load(self, path: str) -> None:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                event = Event(**raw)
                self._events.append(event)
                if event.registration_no:
                    self._idempotency[event.registration_no] = event.seq

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._idempotency.clear()
            if self.path and os.path.exists(self.path):
                os.remove(self.path)


def _fingerprint(stream: str, etype: str, data: dict) -> str:
    """登记内容指纹：同一登记编号重复提交时判定是否为同一事实。"""
    import hashlib

    payload = json.dumps(
        {"stream": stream, "etype": etype, "data": _strip_meta(data)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _strip_meta(data: dict) -> dict:
    return {k: v for k, v in data.items() if not k.startswith("_")}
