from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from clogguard.core.commands import PRINTER_KINDS, Command
from clogguard.core.events import Assessment, CommandResult, Decision
from clogguard.service.ladder import RecoveryLadder

WALL = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def A(t: float, p: float | None, **kw: Any) -> Assessment:
    state = kw.pop("state", "unknown" if p is None else ("clog" if p >= 0.8 else "normal"))
    return Assessment(t_s=t, wall_time=WALL, state=state, p_clog=p, **kw)


def result(cmd: Command, t: float, *, accepted: bool = True, acked: bool = True) -> CommandResult:
    return CommandResult(
        t_s=t, wall_time=WALL, command=cmd, accepted=accepted,
        rejected_reason=None if accepted and acked else ("min_interval: x" if not accepted
                                                          else "not_acked: timeout"),
        sent_at_s=t if accepted else None, acked_at_s=t + 0.1 if accepted and acked else None,
        decision_t_s=t,
    )


def drive(
    ladder: RecoveryLadder,
    p_of_t: Any,
    t_end: float,
    *,
    dt: float = 1.0,
    t0: float = 0.0,
    extra: Any = None,
) -> list[Decision]:
    """Step the ladder once per dt with p_clog = p_of_t(t); ack every printer command."""
    out: list[Decision] = []
    t = t0
    while t <= t_end + 1e-9:
        kw = extra(t) if extra else {}
        d = ladder.step(A(t, p_of_t(t), **kw))
        out.append(d)
        if d.command is not None and d.command.kind in PRINTER_KINDS:
            ladder.on_result(result(d.command, t))
            q = ladder.take_queued(t, WALL)
            while q is not None:
                out.append(q)
                q = ladder.take_queued(t, WALL)
        t = round(t + dt, 6)
    return out


def commands(decisions: list[Decision]) -> list[tuple[float, Command]]:
    return [(d.t_s, d.command) for d in decisions if d.command is not None]
