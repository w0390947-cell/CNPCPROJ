"""Deterministic device dynamics and command lifecycle, without I/O."""

from datetime import datetime, timedelta
from math import hypot, sqrt

from .contracts import Bundle, Command, Fault, Frame, NetworkPort, Reading, Receipt


class Fleet:
    def __init__(self, bundle: Bundle, network: NetworkPort, epoch: str):
        self.bundle, self.network, self.epoch = bundle, network, epoch
        self.minute = -1
        self.fault: Fault = "normal"
        self.blocked = False
        self.safe_cycles = 0
        self.readings: tuple[Reading, ...] = ()
        self.receipts: dict[str, Receipt] = {}
        self.targets: dict[str, Command] = {}
        self._awaiting_fresh_command: set[str] = set()
        self.frame: Frame | None = None

    def submit(self, command: Command) -> Receipt:
        if self.frame is None:
            raise ValueError("simulator is not ready")
        previous = self.receipts.get(command.command_id)
        if previous is not None:
            if previous.command != command:
                raise ValueError("command_id conflicts with earlier payload")
            return previous
        # Bounded epoch: never evict idempotency keys and silently execute a replay.
        if len(self.receipts) >= 10000:
            raise ValueError("epoch command capacity reached; restart to create a new epoch")
        now = self.frame.simulated_at
        reason = ""
        spec = next((d for d in self.bundle.devices if d.device_id == command.device_id), None)
        if command.epoch != self.epoch:
            reason = "EPOCH_MISMATCH"
        elif command.expires_at <= now:
            reason = "EXPIRED"
        elif (command.expires_at - now).total_seconds() > 3600:
            reason = "TTL_EXCEEDS_ONE_SIMULATED_HOUR"
        elif spec is None:
            reason = "UNKNOWN_DEVICE"
        elif self.fault == "communication_loss":
            reason = "COMMUNICATION_UNAVAILABLE"
        elif (
            not spec.p_min_mw <= command.p_mw <= spec.p_max_mw
            or abs(command.q_mvar) > spec.q_max_mvar
            or hypot(command.p_mw, command.q_mvar) > spec.s_max_mva + 1e-9
            or (
                spec.q_abs_over_p_max is not None
                and abs(command.q_mvar) > spec.q_abs_over_p_max * abs(command.p_mw) + 1e-9
            )
        ):
            reason = "CAPABILITY_VIOLATION"
        elif self.blocked:
            actual = next(r for r in self.readings if r.device_id == command.device_id)
            if command.p_mw > actual.p_mw + 1e-9 or command.q_mvar != actual.q_mvar:
                reason = "RECOVERY_INTERLOCK"
        status = "rejected" if reason else "accepted"
        receipt = Receipt(command=command, status=status, reason=reason, updated_at=now)
        if not reason:
            old = self.targets.get(command.device_id)
            if old and self.receipts[old.command_id].status in {"accepted", "executing"}:
                self._receipt(old, "superseded", now, "NEW_COMMAND")
            self.targets[command.device_id] = command
            if not self.blocked:
                # Only a newly accepted command releases this device. Replayed,
                # rejected or still-interlocked requests grant no recovery authority.
                self._awaiting_fresh_command.discard(command.device_id)
        self.receipts[command.command_id] = receipt
        return receipt

    def _receipt(self, command: Command, status: str, at: datetime, reason: str = "") -> None:
        self.receipts[command.command_id] = Receipt.model_validate(
            dict(command=command, status=status, reason=reason, updated_at=at)
        )

    def rearm(self) -> bool:
        if self.frame is None or not self.blocked or self.safe_cycles < 2:
            return False
        if not all(n.recovery_safe for n in self.frame.networks):
            return False
        self.blocked = False
        # Network permission does not authorize restoration of historical targets.
        self._awaiting_fresh_command = {d.device_id for d in self.bundle.devices}
        return True

    def step(self) -> Frame:
        self.minute += 1
        at = self.bundle.start + timedelta(minutes=self.minute)
        previous = {r.device_id: r for r in self.readings}
        values: list[Reading] = []
        for spec in self.bundle.devices:
            old = previous.get(spec.device_id)
            available = self.network.available(spec, self.minute)
            if self.fault == "wind_trip" and spec.kind == "wind":
                available = 0.0
            command = self.targets.get(spec.device_id)
            was_executing = bool(
                command and self.receipts[command.command_id].status == "executing"
            )
            p_target, q_target = (available, 0.0) if spec.kind in {"wind", "pv"} else (0.0, 0.0)
            if command:
                status = self.receipts[command.command_id].status
                if status in {"accepted", "executing"} and command.expires_at <= at:
                    self._receipt(command, "expired", at, "DEADLINE_PASSED")
                elif status == "accepted":
                    self._receipt(command, "executing", at)
                status = self.receipts[command.command_id].status
                if status in {"executing", "executed"}:
                    p_target, q_target = command.p_mw, command.q_mvar
                elif old:
                    p_target, q_target = old.p_mw, old.q_mvar
            if old and (
                self.blocked
                or spec.device_id in self._awaiting_fresh_command
                or self.fault == "communication_loss"
            ):
                p_target, q_target = min(p_target, old.p_mw), old.q_mvar
            if spec.kind in {"wind", "pv"}:
                p_target = min(p_target, available)
            p_old = old.p_mw if old else 0.0
            ramp = spec.ramp_mw_per_minute
            p = max(p_old - ramp, min(p_old + ramp, p_target))
            if spec.kind in {"wind", "pv"}:
                p = min(p, available)
            energy = old.energy_mwh if old else spec.initial_mwh
            if spec.kind == "storage":
                assert energy is not None
                p = min(p, (energy - spec.minimum_mwh) * 60 * spec.eta_discharge)
                p = max(p, -(spec.energy_mwh - energy) * 60 / spec.eta_charge)
                energy -= (p / spec.eta_discharge if p >= 0 else p * spec.eta_charge) / 60
            q_cap = min(spec.q_max_mvar, sqrt(max(0.0, spec.s_max_mva**2 - p**2)))
            if spec.q_abs_over_p_max is not None:
                q_cap = min(q_cap, spec.q_abs_over_p_max * abs(p))
            q = max(-q_cap, min(q_cap, q_target))
            values.append(
                Reading(
                    device_id=spec.device_id,
                    region=spec.region,
                    bus_id=spec.bus_id,
                    kind=spec.kind,
                    p_mw=p,
                    q_mvar=q,
                    available_mw=available,
                    energy_mwh=energy if spec.kind == "storage" else None,
                )
            )
            if command and self.receipts[command.command_id].status == "executing":
                if self.fault == "ack_timeout":
                    self._receipt(command, "timeout", at, "ACK_NOT_RECEIVED_EXECUTION_UNKNOWN")
                elif abs(p - command.p_mw) < 1e-6 and abs(q - command.q_mvar) < 1e-6:
                    # Require a separate tick between acceptance and execution acknowledgment.
                    if was_executing:
                        self._receipt(command, "executed", at)
        self.readings = tuple(values)
        observed = at - timedelta(minutes=2) if self.fault == "communication_loss" else at
        networks = self.network.evaluate(self.readings, self.minute, at, observed, self.fault)
        safe = bool(networks) and all(n.recovery_safe for n in networks)
        self.safe_cycles = self.safe_cycles + 1 if safe else 0
        if not safe:
            self.blocked = True
        self.frame = Frame(
            dataset_id=self.bundle.dataset_id,
            epoch=self.epoch,
            sequence=self.minute + 1,
            simulated_at=at,
            observed_at=observed,
            fault=self.fault,
            quality_valid=self.fault not in {"communication_loss", "bad_quality"},
            recovery_blocked=self.blocked,
            safe_cycles=self.safe_cycles,
            devices=self.readings,
            networks=networks,
            receipts=tuple(self.receipts.values())[-100:],
        )
        return self.frame
