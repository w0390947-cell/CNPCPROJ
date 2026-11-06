"""Biased sensors and an independent bounded-response plant; analytic tests only."""

from datetime import timedelta
from fractions import Fraction as F

from oilfield_energy.bootstrap.field_initialization import create_snapshot_submitter
from oilfield_energy.workflows.field_initialization.contracts import (
    SubmitSnapshotRequest,
    TransportCapabilities,
)
from tests.support.field_execution import Harness, SoftwareTransport
from tests.support.field_initialization import NOW
from tests.support.field_snapshots import snapshot_document
from tests.support.field_timing import TimedHarness, set_pcc, timing_document


def clock_evidence():
    return dict(
        error_seconds=0.1,
        clock_revision="analytic-clock-1",
        provenance_ref="analytic-clock-only",
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(hours=3),
    )


def scalar_values(snapshot):
    return (
        [v for b in snapshot["bus_loads"] for v in (b["p_mw"], b["q_mvar"])]
        + [
            v
            for d in snapshot["device_states"]
            for key in ("p_mw", "q_mvar", "energy_mwh")
            if (v := d.get(key)) is not None
        ]
        + [g["available_p_mw"] for g in snapshot["renewable_availability"]]
        + [snapshot["pcc"][k] for k in ("p_import_mw", "q_import_mvar", "voltage_pu")]
    )


def error_size(source):
    if (
        source.startswith("bus_load:pcc")
        or source.endswith(":q")
        and not source.startswith("pcc:")
    ):
        return 0.0
    if source == "resource:storage:energy" or source == "fixture-estimate":
        return 0.002
    if source.endswith(":voltage") or source == "pcc:pcc:q":
        return 0.001
    return 0.005


def decorate(snapshot, held, *, energy_boundary=1.0):
    boundary = (
        NOW
        + timedelta(minutes=round((snapshot["snapshot_at"] - NOW).total_seconds() / 60))
        if not isinstance(snapshot["snapshot_at"], str)
        else None
    )
    if boundary is None:
        from datetime import datetime

        boundary = datetime.fromisoformat(
            snapshot["snapshot_at"].replace("Z", "+00:00")
        )
    snapshot["received_at"] = boundary + timedelta(seconds=1)
    for state in snapshot["device_states"]:
        state["status_timestamp_uncertainty"] = clock_evidence()
    for value in scalar_values(snapshot):
        ref = value["source_ref"]
        value["observed_at"] = boundary + timedelta(seconds=0.2)
        value["uncertainty"] = dict(
            absolute_error=error_size(ref),
            calibration_revision="analytic-calibration-1",
            provenance_ref="analytic-instrument-only",
            valid_from=NOW - timedelta(days=1),
            valid_until=NOW + timedelta(hours=3),
            timestamp=clock_evidence(),
        )
        if ref.startswith("resource:") and ref.endswith(":p"):
            value["value"] += 0.001
        elif ref == "bus_load:load:p" or ref == "pcc:pcc:p":
            value["value"] += 0.001
        elif ref == "pcc:pcc:q" or ref == "pcc:pcc:voltage":
            value["value"] += 0.0001
        elif ref == "resource:storage:energy":
            power = F(held["storage"][0])
            rate = (-power / F(0.95) if power >= 0 else -power * F(0.95)) / 3600
            value["value"] = float(F(energy_boundary) + rate * F(1, 10)) + 0.001
    snapshot["held_setpoints"] = [
        dict(
            resource_id=identity,
            p_mw=p,
            q_mvar=q,
            observed_at=boundary + timedelta(seconds=0.6),
            timestamp_uncertainty=clock_evidence(),
            held_since=since,
            since_uncertainty=clock_evidence(),
            source_ref="analytic-controller:held",
        )
        for identity, (p, q, since) in held.items()
    ]
    return snapshot


def uncertainty_document():
    d = timing_document()
    p = d["payload"]
    t = p["control_policy"]["bounded_timing"]
    t.update(
        profile="bounded_uncertain_v1",
        revision="analytic-uncertain-timing-1",
        response_semantics="monotone_no_overshoot_bounded_settling",
        clock_error_bound_seconds=0.1,
        prepare_max_seconds=8.0,
        apply_not_before_seconds=12.0,
        apply_not_after_seconds=18.0,
    )
    t["resources"][0].update(settled_p_error_mw=0.003, zero_p_is_exact=True)
    p["control_policy"].update(
        model_error_policy_ref="bounded-observation-v1", compute_budget_seconds=3.0
    )
    for point in p["planning_inputs"]["forecast_points"]:
        for load in point["bus_loads"]:
            if load["bus_id"] == "load":
                load["p_bounds_mw"].update(low=0.98, high=1.02)
    snapshot = p["initial_snapshot"]
    # Actual wind output differs from held setpoint; meter introduces a further bias.
    snapshot["device_states"][0]["p_mw"]["value"] = 0.201
    set_pcc(snapshot)
    held = {
        "wind": (0.2, 0.0, NOW - timedelta(minutes=2)),
        "storage": (0.1, 0.0, NOW - timedelta(minutes=2)),
    }
    decorate(snapshot, held)
    p["control_policy"]["observation_uncertainty"] = dict(
        profile="bounded_observation_v1",
        revision="analytic-metrology-1",
        root_voltage_bounds_pu=dict(low=0.998, high=1.002),
        clock_revision="analytic-clock-1",
        provenance_ref="analytic-guard-only",
        valid_from=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(hours=2),
        boundary_radius_seconds=0.5,
        measurements=[
            dict(
                source_ref=v["source_ref"],
                calibration_revision="analytic-calibration-1",
                source_kind=v["source_kind"],
                max_absolute_error=error_size(v["source_ref"]),
                max_timestamp_error_seconds=0.1,
                alignment_rate_per_second=0.001
                if v["source_ref"] in ("bus_load:load:p", "resource:storage:energy")
                else 0.0,
                provenance_ref="analytic-calibration-and-drift",
            )
            for v in scalar_values(snapshot)
        ],
    )
    return d


class UncertainTransport(SoftwareTransport):
    capabilities = TransportCapabilities(
        timing_profile="bounded_uncertain_v1",
        timing_revision="analytic-uncertain-timing-1",
        provenance_ref="analytic-only",
    )

    def __init__(self, clock, advance, policy=None, timing=None):
        super().__init__()
        self.clock, self.advance = clock, advance
        if policy is not None:
            from hashlib import sha256

            from oilfield_energy.workflows.field_initialization.contracts_uncertainty import (
                ObservationUncertaintyPolicy,
            )
            from oilfield_energy.workflows.field_initialization.messages import (
                canonical,
            )

            self.capabilities = self.capabilities.model_copy(
                update={
                    "observation_policy_sha256": sha256(
                        canonical(ObservationUncertaintyPolicy.model_validate(policy))
                    ).hexdigest()
                }
            )
            if timing is not None:
                from oilfield_energy.workflows.field_initialization.contracts_timing import (
                    BoundedTimingPolicy,
                )

                self.capabilities = self.capabilities.model_copy(
                    update={
                        "timing_policy_sha256": sha256(
                            canonical(BoundedTimingPolicy.model_validate(timing))
                        ).hexdigest()
                    }
                )

    def send(self, grid, batch):
        results = super().send(grid, batch)
        ack = self.clock() + timedelta(seconds=0.3)
        self.advance(0.6)
        from oilfield_energy.workflows.field_initialization.contracts_values import (
            TimestampUncertainty,
        )

        self.results = tuple(
            r.model_copy(
                update=dict(
                    device_status="unknown",
                    applied_at=None,
                    acknowledged_at=ack,
                    timestamp_uncertainty=TimestampUncertainty.model_validate(
                        clock_evidence()
                    ),
                    echoed_p_setpoint_mw=None,
                    echoed_q_setpoint_mvar=None,
                )
            )
            for r in results
        )
        return self.results


class UncertainHarness(TimedHarness):
    def __init__(self, root, document=None):
        document = document or uncertainty_document()
        transport = UncertainTransport(
            lambda: self.now,
            self.advance,
            document["payload"]["control_policy"]["observation_uncertainty"],
            document["payload"]["control_policy"]["bounded_timing"],
        )
        Harness.__init__(
            self, root, document, transport, initial_time=NOW + timedelta(seconds=1)
        )
        self.state = {"wind": [0.201, 0.0], "storage": [0.1, 0.0]}
        self.energy = 1.0
        self.held = {
            "wind": (0.2, 0.0, NOW - timedelta(minutes=2)),
            "storage": (0.1, 0.0, NOW - timedelta(minutes=2)),
        }
        self.now = NOW + timedelta(seconds=2)

    def advance(self, value):
        self.now += timedelta(seconds=value)

    def observe(self, offsets=(14, 16), mutate=None, submit=True):
        start = NOW + timedelta(minutes=self.minute)
        results = []
        for cmd, offset in zip(
            self.prepared.payload.data.commands, offsets, strict=True
        ):
            old = self.held[cmd.resource_id][0]
            new = cmd.p_setpoint_mw if cmd.p_setpoint_mw is not None else old
            q = (
                cmd.q_setpoint_mvar
                if cmd.q_setpoint_mvar is not None
                else self.held[cmd.resource_id][1]
            )
            if cmd.resource_id == "storage":

                def delta(power, time):
                    return (
                        (-F(power) / F(0.95) if power >= 0 else -F(power) * F(0.95))
                        * time
                        / 3600
                    )

                self.energy = float(
                    F(self.energy) + delta(old, offset) + delta(new, 60 - offset)
                )
            true_p = new + (0.001 if cmd.resource_id == "wind" and new else 0.0)
            self.state[cmd.resource_id] = [true_p, q]
            stamp = start + timedelta(seconds=offset + 0.05)
            self.held[cmd.resource_id] = (new, q, stamp)
            prior = next(
                r for r in self.transport.results if r.command_id == cmd.command_id
            )
            results.append(
                prior.model_copy(
                    update=dict(
                        applied_at=stamp,
                        device_status="applied",
                        echoed_p_setpoint_mw=cmd.p_setpoint_mw,
                        echoed_q_setpoint_mvar=cmd.q_setpoint_mvar,
                    )
                )
            )
        self.transport.results = tuple(results)
        self.minute += 1
        self.sequence += 1
        boundary = NOW + timedelta(minutes=self.minute)
        self.now = boundary + timedelta(seconds=1)
        document = snapshot_document(
            self.document, self.initial, minute=self.minute, sequence=self.sequence
        )
        document["created_at"] = self.now
        snapshot = document["payload"]
        for d in snapshot["device_states"]:
            d["p_mw"]["value"], d["q_mvar"]["value"] = self.state[d["resource_id"]]
        # Reset inherited meter values to the analytic physical values before biasing.
        snapshot["bus_loads"][1]["p_mw"]["value"] = 1.0
        snapshot["pcc"]["voltage_pu"]["value"] = 1.0
        set_pcc(snapshot)
        decorate(snapshot, self.held, energy_boundary=self.energy)
        if mutate:
            mutate(document)
        self.snapshot_document = document
        self.snapshot_id = snapshot["snapshot_id"]
        request = SubmitSnapshotRequest.model_validate(document)
        return (
            create_snapshot_submitter(self.root, clock=lambda: self.now).execute(
                request
            )
            if submit
            else request
        )

    def report_request(self, stage="cycle_end", results=None, **changes):
        from oilfield_energy.workflows.field_initialization.contracts import (
            ReportExecutionRequest,
        )

        energy = None
        if stage == "cycle_end":
            sample = next(
                d["energy_mwh"]
                for d in self.snapshot_document["payload"]["device_states"]
                if "energy_mwh" in d
            )
            end = NOW + timedelta(minutes=self.minute)
            energy = dict(
                method="bounded_estimate"
                if sample["source_kind"] == "estimated"
                else "measured_energy",
                interval_start=end - timedelta(minutes=1),
                interval_end=end,
                sample_observed_at=sample["observed_at"],
                source_ref=sample["source_ref"],
                energy_end_mwh=sample["value"],
                uncertainty_mwh=sample["uncertainty"]["absolute_error"],
            )
        payload = dict(
            context_id=self.initial.payload.data.context_id,
            cycle_id=self.prepared.payload.data.cycle_id,
            command_batch_id=self.prepared.payload.data.command_batch_id,
            feedback_stage=stage,
            reported_at=self.now,
            command_results=self.transport.results if results is None else results,
            observed_snapshot_id=self.snapshot_id if stage == "cycle_end" else None,
            energy_evidence=energy,
            reason="",
        )
        payload.update(changes)
        return ReportExecutionRequest.model_validate(
            self.envelope("ReportExecution", payload)
        )

    def report(self, stage="cycle_end", results=None, **changes):
        return self.services[0].execute(self.report_request(stage, results, **changes))
