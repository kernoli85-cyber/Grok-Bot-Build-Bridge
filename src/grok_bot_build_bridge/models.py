from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


BackendId = Literal["native", "wsl"]
DiscoveryKind = Literal["none", "native_only", "wsl_only", "both"]


class BackendState(str, Enum):
    READY = "ready"
    NOT_FOUND = "not_found"
    STOPPED = "stopped"
    NOT_READY = "not_ready"  # e.g. root-only WSL user, version unparsable


@dataclass(frozen=True)
class NativeBackend:
    state: BackendState
    path: str | None = None
    version: str | None = None


@dataclass(frozen=True)
class WslCandidate:
    distro: str
    user: str
    uid: int
    state: BackendState  # READY / STOPPED / NOT_READY
    grok_path: str | None = None
    version: str | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscoveryReport:
    native: NativeBackend
    wsl: tuple[WslCandidate, ...] = ()
    kind: DiscoveryKind = "none"
    default_backend: BackendId | None = None
    warnings: tuple[str, ...] = ()

    def ready_backends(self) -> frozenset[BackendId]:
        ready: set[BackendId] = set()
        if self.native.state == BackendState.READY:
            ready.add("native")
        if any(c.state == BackendState.READY for c in self.wsl):
            ready.add("wsl")
        return frozenset(ready)


@dataclass(frozen=True)
class JobRequest:
    """Contract for a single Build job — one backend, one run."""

    task_id: str
    prompt: str
    cwd: str
    backend: BackendId | None = None  # None → use default; never silent other backend
    max_turns: int | None = None
    permission_mode: str | None = None
    allow_rules: tuple[str, ...] = ()
    deny_rules: tuple[str, ...] = ()


@dataclass(frozen=True)
class JobPlan:
    task_id: str
    backend: BackendId
    cwd: str
    argv: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RoutingError:
    code: str
    message: str
