"""追加式事件日志（JSON Lines）：只追加、不改写，重启后可完整回放。

设计要点：

- 每个事件占一行 JSON，带全局递增 ``seq``；写入后立即 ``flush + fsync``，
  进程崩溃或被 kill 时，已经确认的事件不会丢。
- 支持幂等键（``idem_key``）：同一键重复写入直接返回旧事件。
  迁移重试、日志重放都不会让同一条记录长出副本。
- 回放时遇到尾部损坏/不完整的行会停止解析，并把文件截断到最后一个完整事件
  之后，保留此前全部已确认数据，后续追加仍可继续。
- 仅依赖标准库，克隆下来即可运行，不引入新的第三方依赖。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class Event:
    """一条不可变的追加事件。"""

    seq: int
    id: str
    ts: str
    type: str
    stream: str
    idem_key: str
    payload: dict[str, Any]


def utc_now_iso() -> str:
    """统一使用带时区的 ISO8601 时间串，保证重放后排序口径一致。"""
    return datetime.now(timezone.utc).isoformat()


class AppendOnlyLog:
    """文件型追加日志，所有读写都在同一把可重入锁内完成。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._events: list[Event] = []
        self._idem: dict[str, Event] = {}
        self._write_next_seq = 1
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self._replay()

    # ------------------------------------------------------------------ 回放
    def _replay(self) -> None:
        """从磁盘回放全部事件；尾部损坏行就地截断，保住之前的完整事件。"""
        if not os.path.exists(self.path):
            return
        valid_offset = 0
        with open(self.path, "rb") as fh:
            while True:
                raw_line = fh.readline()
                if not raw_line:
                    break
                # 始终记录原始字节偏移，truncate 才能精确落到完整行边界
                offset = fh.tell()
                try:
                    text = raw_line.decode("utf-8").strip()
                    if not text:
                        valid_offset = offset
                        continue
                    raw = json.loads(text)
                    event = Event(
                        seq=int(raw["seq"]),
                        id=str(raw["id"]),
                        ts=str(raw.get("ts", "")),
                        type=str(raw["type"]),
                        stream=str(raw.get("stream", "audit")),
                        idem_key=str(raw.get("idem_key", "")),
                        payload=dict(raw.get("payload", {})),
                    )
                except (UnicodeDecodeError, ValueError, KeyError, TypeError):
                    # 尾部半截写入：停在这里，后面统一截断
                    break
                self._events.append(event)
                if event.idem_key:
                    self._idem.setdefault(event.idem_key, event)
                valid_offset = offset
        file_size = os.path.getsize(self.path)
        if valid_offset < file_size:
            with open(self.path, "r+b") as fh:
                fh.truncate(valid_offset)
        self._write_next_seq = max((e.seq for e in self._events), default=0) + 1

    # ------------------------------------------------------------------ 写入
    def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        stream: str = "audit",
        idem_key: str = "",
        event_id: str | None = None,
    ) -> Event:
        """追加一条事件；``idem_key`` 已存在时原样返回旧事件，不产生重复行。"""
        with self._lock:
            if idem_key and idem_key in self._idem:
                return self._idem[idem_key]
            seq = self._write_next_seq
            event = Event(
                seq=seq,
                id=event_id or f"EVT-{seq:08d}",
                ts=utc_now_iso(),
                type=event_type,
                stream=stream,
                idem_key=idem_key,
                payload=payload,
            )
            line = json.dumps(
                {
                    "seq": event.seq,
                    "id": event.id,
                    "ts": event.ts,
                    "type": event.type,
                    "stream": event.stream,
                    "idem_key": event.idem_key,
                    "payload": event.payload,
                },
                ensure_ascii=False,
            )
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._events.append(event)
            self._write_next_seq = seq + 1
            if idem_key:
                self._idem[idem_key] = event
            return event

    # ------------------------------------------------------------------ 读取
    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def events(self) -> list[Event]:
        """按写入顺序（seq 升序）返回全部事件。"""
        with self._lock:
            return list(self._events)

    def find_idem(self, idem_key: str) -> Event | None:
        with self._lock:
            return self._idem.get(idem_key)
