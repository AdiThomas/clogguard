from __future__ import annotations

from typing import Any

from clogguard.core.config import LadderConfig, MaterialProfile, ServiceConfig


def _ladder(lad: dict[str, Any]) -> LadderConfig:
    """Build a LadderConfig. A purge_macro that config validation would refuse (e.g. "M112") is
    injected without validation, to test that the gate still refuses it on its own."""
    macro = lad.pop("purge_macro", None)
    cfg = LadderConfig(**lad)
    return cfg if macro is None else cfg.model_copy(update={"purge_macro": macro})


def make_cfg(
    mode: str = "act",
    *,
    nominal: float = 210.0,
    max_temp: float = 230.0,
    ack_timeout: float = 2.0,
    **ladder: Any,
) -> ServiceConfig:
    lad = {"speed_steps_percent": [85, 70, 55], "min_action_interval_s": 10.0,
           "verify_window_s": 10.0, "max_temp_raise_c": 10.0, **ladder}
    return ServiceConfig(
        mode=mode,  # type: ignore[arg-type]
        material=MaterialProfile(nominal_temp_c=nominal, max_temp_c=max_temp),
        ladder=_ladder(lad),
        command_ack_timeout_s=ack_timeout,
    )


class RecordingNotifier:
    def __init__(self, fail_with: BaseException | None = None, hang: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_with = fail_with
        self.hang = hang

    async def notify(self, level: str, message: str) -> None:
        import asyncio

        if self.fail_with is not None:
            raise self.fail_with
        if self.hang:
            await asyncio.Event().wait()
        self.sent.append((level, message))


class FakeClock:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt
