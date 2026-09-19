"""统一时钟。

业务时间一律经 Clock 获取，保证"有效期""窗口""赛后回放"使用同一时间源，
测试中可注入固定时钟复现两天赛程。
"""

from datetime import datetime, timezone


class Clock:
    """可替换的时间源。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def now_iso(self) -> str:
        return to_iso(self.now())


class FixedClock(Clock):
    """测试/离线补传复放使用的固定时钟。"""

    def __init__(self, moment: datetime | str):
        if isinstance(moment, str):
            moment = parse_iso(moment)
        self._moment = moment

    def set(self, moment: datetime | str) -> None:
        if isinstance(moment, str):
            moment = parse_iso(moment)
        self._moment = moment

    def now(self) -> datetime:
        return self._moment


def to_iso(value: datetime) -> str:
    """统一输出带时区偏移的 ISO8601（Z 表示 UTC）。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    """宽松解析 ISO8601，允许结尾 Z。"""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
