"""Independent bounded-time plant fixture; never a real communication adapter."""

from copy import deepcopy
from datetime import timedelta
from fractions import Fraction as F
from math import sqrt

from oilfield_energy.bootstrap.field_initialization import (
    create_control_cycle_preparer,
    create_snapshot_submitter,
)
from oilfield_energy.workflows.field_initialization.contracts import (
    PrepareControlCycleRequest,
    SubmitSnapshotRequest,
    TransportCapabilities,
)
from tests.support.field_cycles import cycle_document
from tests.support.field_execution import Harness, SoftwareTransport
from tests.support.field_initialization import NOW, request_document
from tests.support.field_snapshots import snapshot_document


def set_pcc(snapshot):
    netp = 1 - sum(d["p_mw"]["value"] for d in snapshot["device_states"])
    netq = -sum(d["q_mvar"]["value"] for d in snapshot["device_states"])
    pp, qq, x = netp / 10, netq / 10, 0.001
    w = (1 - 2 * x * qq + sqrt((1 - 2 * x * qq) ** 2 - 4 * x * x * (pp * pp + qq * qq))) / 2
    snapshot["pcc"]["p_import_mw"]["value"] = netp
    snapshot["pcc"]["q_import_mvar"]["value"] = netq + x * (pp * pp + qq * qq) / w * 10


def hold(snapshot):
    snapshot["held_setpoints"] = [
        dict(
            resource_id=d["resource_id"],
            p_mw=d["p_mw"]["value"],
            q_mvar=d["q_mvar"]["value"],
            observed_at=snapshot["snapshot_at"],
            source_ref="analytic-controller:held",
        )
        for d in snapshot["device_states"]
    ]


def timing_document(nonzero=True):
    document = request_document()
    p = document["payload"]
    p["control_policy"].update(
        compute_budget_seconds=2.0,
        feedback_timeout_seconds=4.0,
        bounded_timing=dict(
            profile="bounded_monotone_v1",
            revision="analytic-timing-1",
            provenance_ref="analytic-bounds-only",
            response_semantics="monotone_no_overshoot_exact_settling",
            clock_error_bound_seconds=0.0,
            prepare_max_seconds=7.0,
            transport_max_seconds=2.0,
            apply_not_before_seconds=10.0,
            apply_not_after_seconds=16.0,
            application_skew_max_seconds=4.0,
            snapshot_delivery_max_seconds=2.0,
            resources=[
                dict(resource_id="wind", settling_max_seconds=8.0, provenance_ref="analytic-wind"),
                dict(
                    resource_id="storage", settling_max_seconds=0.0, provenance_ref="analytic-pcs"
                ),
            ],
        ),
    )
    if nonzero:
        p["initial_snapshot"]["device_states"][0]["p_mw"]["value"] = 0.2
        p["initial_snapshot"]["device_states"][1]["p_mw"]["value"] = 0.1
    hold(p["initial_snapshot"])
    set_pcc(p["initial_snapshot"])
    return document


def closed_loop_document(document):
    result = deepcopy(document)
    p = result["payload"]
    p["mode"] = "closed_loop"
    p["authority"].update(write_enabled=True, owner_id="fixture-owner")
    p["control_policy"]["dispatch_lead_seconds"] = 0.0
    for device in p["model_package"]["devices"]:
        for axis in ("p", "q"):
            if device[axis + "_control_enabled"]:
                point = deepcopy(
                    next(
                        x
                        for x in p["model_package"]["point_mapping"]
                        if x["entity_id"] == device["resource_id"] and x["quantity"] == axis
                    )
                )
                point.update(
                    mapping_id=device["resource_id"] + ":" + axis + "-write",
                    point_id=device["resource_id"] + ":" + axis + "-write",
                    quantity=axis + "_setpoint",
                    access="write",
                )
                p["model_package"]["point_mapping"].append(point)
    return result


class TimedTransport(SoftwareTransport):
    capabilities = TransportCapabilities(
        timing_profile="bounded_monotone_v1",
        timing_revision="analytic-timing-1",
        provenance_ref="analytic-only",
    )

    def send(self, grid, batch):
        results = super().send(grid, batch)
        at = batch.send_claimed_at
        self.results = tuple(
            r.model_copy(
                update=dict(
                    device_status="unknown",
                    applied_at=None,
                    acknowledged_at=at,
                    echoed_p_setpoint_mw=None,
                    echoed_q_setpoint_mvar=None,
                )
            )
            for r in results
        )
        return self.results


class TimedHarness(Harness):
    def __init__(self, root, document=None):
        super().__init__(root, document or timing_document(), TimedTransport())
        self.now += timedelta(seconds=2)

    def prepare(self):
        d = cycle_document(self.document, self.initial)
        boundary = NOW + timedelta(minutes=self.minute)
        d.update(message_id=self.envelope("prepare", {})["message_id"], created_at=self.now)
        d["payload"].update(
            cycle_id=f"cycle-{self.minute}",
            cycle_start_at=boundary,
            cycle_end_at=boundary + timedelta(minutes=1),
            snapshot_id=self.snapshot_id,
        )
        self.prepared = create_control_cycle_preparer(
            self.root, clock=lambda: self.now, transport=self.transport
        ).execute(PrepareControlCycleRequest.model_validate(d))
        return self.prepared

    def observe(self, offsets=(12, 14)):
        start = NOW + timedelta(minutes=self.minute)
        commands = self.prepared.payload.data.commands
        results = []
        for command, offset in zip(commands, offsets, strict=True):
            old = self.state[command.resource_id][0]
            new = command.p_setpoint_mw if command.p_setpoint_mw is not None else old
            if command.resource_id == "storage":

                def delta(p, seconds):
                    return (-F(p) / F(0.95) if p >= 0 else -F(p) * F(0.95)) * seconds / 3600

                self.energy = float(F(self.energy) + delta(old, offset) + delta(new, 60 - offset))
            self.state[command.resource_id] = [
                new,
                command.q_setpoint_mvar
                if command.q_setpoint_mvar is not None
                else self.state[command.resource_id][1],
            ]
            prior = next(r for r in self.transport.results if r.command_id == command.command_id)
            results.append(
                prior.model_copy(
                    update=dict(
                        applied_at=start + timedelta(seconds=offset),
                        echoed_p_setpoint_mw=command.p_setpoint_mw,
                        echoed_q_setpoint_mvar=command.q_setpoint_mvar,
                        device_status="applied",
                    )
                )
            )
        self.transport.results = tuple(results)
        self.minute += 1
        self.sequence += 1
        sample = NOW + timedelta(minutes=self.minute)
        self.now = sample + timedelta(seconds=1)
        document = snapshot_document(
            self.document, self.initial, minute=self.minute, sequence=self.sequence
        )
        document["created_at"] = self.now
        snapshot = document["payload"]
        snapshot["received_at"] = self.now
        for d in snapshot["device_states"]:
            d["p_mw"]["value"], d["q_mvar"]["value"] = self.state[d["resource_id"]]
            if "energy_mwh" in d:
                d["energy_mwh"]["value"] = self.energy
        hold(snapshot)
        set_pcc(snapshot)
        self.snapshot_id = snapshot["snapshot_id"]
        self.snapshot_document = document
        return create_snapshot_submitter(self.root, clock=lambda: self.now).execute(
            SubmitSnapshotRequest.model_validate(document)
        )

    def report(self, stage="cycle_end", results=None, **changes):
        end = NOW + timedelta(minutes=self.minute)
        if stage == "cycle_end":
            changes.setdefault(
                "energy_evidence",
                dict(
                    method="measured_energy",
                    interval_start=end - timedelta(minutes=1),
                    interval_end=end,
                    source_ref="resource:storage:energy",
                    energy_end_mwh=self.energy,
                    uncertainty_mwh=0.0,
                ),
            )
        return super().report(stage, results, **changes)
