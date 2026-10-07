from __future__ import annotations

import json
import socket
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 1024 * 1024


def package_version() -> str:
    try:
        return version("endstone-endkeep")
    except PackageNotFoundError:
        return "0+unknown"


@dataclass(frozen=True)
class RuntimeInfo:
    protocol: int
    version: str
    pid: int
    port: int
    token: str
    instance_id: str
    storage_root: str
    priority: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RuntimeInfo:
        return cls(
            protocol=int(raw["protocol"]),
            version=str(raw.get("version", "0+unknown")),
            pid=int(raw["pid"]),
            port=int(raw["port"]),
            token=str(raw["token"]),
            instance_id=str(raw["instance_id"]),
            storage_root=str(raw["storage_root"]),
            priority=str(raw["priority"]),
        )

    @classmethod
    def load(cls, path: Path) -> RuntimeInfo:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def send_message(sock: socket.socket, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
    if len(data) > MAX_MESSAGE_BYTES:
        raise ValueError("worker message exceeds maximum size")
    sock.sendall(data)


def receive_message(sock: socket.socket) -> dict[str, Any]:
    chunks = bytearray()
    while len(chunks) <= MAX_MESSAGE_BYTES:
        chunk = sock.recv(65536)
        if not chunk:
            break
        newline = chunk.find(b"\n")
        if newline >= 0:
            chunks.extend(chunk[:newline])
            break
        chunks.extend(chunk)
    if len(chunks) > MAX_MESSAGE_BYTES:
        raise ValueError("worker message exceeds maximum size")
    if not chunks:
        raise ConnectionError("worker closed connection without a response")
    raw = json.loads(chunks.decode("utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("worker message must be a JSON object")
    return raw
