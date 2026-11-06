"""Virtual transport and independent two-bus measurements; never a live connector."""

from copy import deepcopy
from datetime import timedelta
from fractions import Fraction as F
from math import sqrt
from uuid import uuid4

from oilfield_energy.bootstrap.field_initialization import (
    create_control_cycle_preparer,
    create_execution_services,
    create_field_initializer,
    create_snapshot_submitter,
)
from oilfield_energy.workflows.field_initialization.contracts import (
    CommandResult,
    DispatchControlCycleRequest,
    InitializeRequest,
    PrepareControlCycleRequest,
    QueryRunStatusRequest,
    ReportExecutionRequest,
    StopRunRequest,
    SubmitSnapshotRequest,
    TransportCapabilities,
)
from tests.support.field_cycles import cycle_document
from tests.support.field_initialization import NOW, request_document
from tests.support.field_snapshots import snapshot_document


class SoftwareTransport:
    capabilities = TransportCapabilities(
        timing_profile="software_synchronous_v1", provenance_ref="analytic-test-only"
    )

    def __init__(self):
        self.allowed = True
        self.sent = []
        self.results = ()
        self.fail = False
        self.transport_only = False

    def authorized(self, grid, authority):
        return (
            self.allowed
            and authority.write_enabled
            and authority.owner_id == "fixture-owner"
        )

    def send(self, grid, batch):
        self.sent.append(batch)
        if self.fail:
            raise OSError("Ambiguous transport failure after send")
        at = batch.evidence.request.payload.cycle_start_at
        self.results = tuple(
            CommandResult(
                command_id=c.command_id,
                resource_id=c.resource_id,
                transport_status="accepted",
                device_status="unknown" if self.transport_only else "applied",
                acknowledged_at=at,
                applied_at=None if self.transport_only else at,
                echoed_p_setpoint_mw=None if self.transport_only else c.p_setpoint_mw,
                echoed_q_setpoint_mvar=None
                if self.transport_only
                else c.q_setpoint_mvar,
                reason="",
            )
            for c in batch.commands
        )
        return self.results

    def query(self, grid, batch):
        return self.results


class Harness:
    def __init__(self, root, document=None, transport=None, initial_time=None):
        self.root, self.now, self.transport = (
            root,
            initial_time or NOW,
            transport or SoftwareTransport(),
        )
        self.document = deepcopy(document or request_document())
        p = self.document["payload"]
        p["mode"] = "closed_loop"
        p["authority"]["write_enabled"] = True
        p["authority"]["owner_id"] = "fixture-owner"
        p["control_policy"]["dispatch_lead_seconds"] = 0.0
        for d in p["model_package"]["devices"]:
            for axis in ("p", "q"):
                if d[axis + "_control_enabled"]:
                    point = deepcopy(
                        next(
                            x
                            for x in p["model_package"]["point_mapping"]
                            if x["entity_id"] == d["resource_id"]
                            and x["quantity"] == axis
                        )
                    )
                    point.update(
                        mapping_id=d["resource_id"] + ":" + axis + "-write",
                        point_id=d["resource_id"] + ":" + axis + "-write",
                        quantity=axis + "_setpoint",
                        access="write",
                    )
                    p["model_package"]["point_mapping"].append(point)
        self.initial = create_field_initializer(
            root, clock=lambda: self.now, transport=self.transport
        ).execute(InitializeRequest.model_validate(self.document))
        assert self.initial.payload.outcome == "accepted", self.initial
        self.state = {
            d["resource_id"]: [d["p_mw"]["value"], d["q_mvar"]["value"]]
            for d in p["initial_snapshot"]["device_states"]
        }
        self.energy = next(
            d["energy_mwh"]["value"]
            for d in p["initial_snapshot"]["device_states"]
            if "energy_mwh" in d
        )
        self.snapshot_id = p["initial_snapshot"]["snapshot_id"]
        self.minute = 0
        self.sequence = 1

    @property
    def services(self):
        return create_execution_services(
            self.root, clock=lambda: self.now, transport=self.transport
        )

    def envelope(self, operation, payload):
        return dict(
            interface_version=self.initial.interface_version,
            operation=operation,
            message_id=str(uuid4()),
            correlation_id=None,
            microgrid_id=self.initial.microgrid_id,
            run_id=self.initial.run_id,
            created_at=self.now,
            payload=payload,
        )

    def prepare(self):
        d = cycle_document(self.document, self.initial)
        d.update(message_id=str(uuid4()), created_at=self.now)
        d["payload"].update(
            cycle_id=f"cycle-{self.minute}",
            cycle_start_at=self.now,
            cycle_end_at=self.now + timedelta(minutes=1),
            snapshot_id=self.snapshot_id,
        )
        self.prepared = create_control_cycle_preparer(
            self.root, clock=lambda: self.now, transport=self.transport
        ).execute(PrepareControlCycleRequest.model_validate(d))
        return self.prepared

    def dispatch_request(self):
        return DispatchControlCycleRequest.model_validate(
            self.envelope(
                "DispatchControlCycle",
                dict(
                    context_id=self.initial.payload.data.context_id,
                    command_batch_id=self.prepared.payload.data.command_batch_id,
                    authority=self.document["payload"]["authority"],
                ),
            )
        )

    def observe(self, corrupt=False):
        """Calculate an analytic plant sample independently from prediction evidence."""
        p = self.document["payload"]
        for cmd in self.prepared.payload.data.commands:
            d = next(
                x
                for x in p["model_package"]["devices"]
                if x["resource_id"] == cmd.resource_id
            )
            for i, (u, alpha) in enumerate(
                (
                    (cmd.p_setpoint_mw, d["alpha_p"]),
                    (cmd.q_setpoint_mvar, p["control_policy"]["alpha_q"]),
                )
            ):
                if u is not None:
                    value = (1 - F(alpha)) * F(self.state[cmd.resource_id][i]) + F(
                        alpha
                    ) * F(u)
                    self.state[cmd.resource_id][i] = float(
                        F(u)
                        if abs(value - F(u))
                        <= F(p["control_policy"]["settling_resolution"])
                        else value
                    )
            if d["kind"] == "storage":
                power = F(cmd.p_setpoint_mw)
                self.energy = float(
                    F(self.energy)
                    + (
                        -power / F(d["eta_discharge"])
                        if power >= 0
                        else -power * F(d["eta_charge"])
                    )
                    / 60
                )
        self.now += timedelta(minutes=1)
        self.minute += 1
        self.sequence += 1
        d = snapshot_document(
            self.document, self.initial, minute=self.minute, sequence=self.sequence
        )
        for r in d["payload"]["device_states"]:
            r["p_mw"]["value"], r["q_mvar"]["value"] = self.state[r["resource_id"]]
            if "energy_mwh" in r:
                r["energy_mwh"]["value"] = self.energy + (0.1 if corrupt else 0)
        netp = 1 - sum(x[0] for x in self.state.values())
        netq = -sum(x[1] for x in self.state.values())
        pp, qq, x = netp / 10, netq / 10, 0.001
        w = (
            1
            - 2 * x * qq
            + sqrt((1 - 2 * x * qq) ** 2 - 4 * x * x * (pp * pp + qq * qq))
        ) / 2
        d["payload"]["pcc"]["p_import_mw"]["value"] = netp
        d["payload"]["pcc"]["q_import_mvar"]["value"] = (
            netq + x * (pp * pp + qq * qq) / w * 10
        )
        self.snapshot_id = d["payload"]["snapshot_id"]
        self.snapshot_document = d
        return create_snapshot_submitter(self.root, clock=lambda: self.now).execute(
            SubmitSnapshotRequest.model_validate(d)
        )

    def report(self, stage="cycle_end", results=None, **changes):
        p = dict(
            context_id=self.initial.payload.data.context_id,
            cycle_id=self.prepared.payload.data.cycle_id,
            command_batch_id=self.prepared.payload.data.command_batch_id,
            feedback_stage=stage,
            reported_at=self.now,
            command_results=self.transport.results if results is None else results,
            observed_snapshot_id=self.snapshot_id if stage == "cycle_end" else None,
            energy_evidence=dict(
                method="measured_energy",
                interval_start=self.now - timedelta(minutes=1),
                interval_end=self.now,
                source_ref="resource:storage:energy",
                energy_end_mwh=self.energy,
                uncertainty_mwh=0.0,
            )
            if stage == "cycle_end"
            else None,
            reason="",
        )
        p.update(changes)
        return self.services[0].execute(
            ReportExecutionRequest.model_validate(self.envelope("ReportExecution", p))
        )

    def query(self, **payload):
        return self.services[1].execute(
            QueryRunStatusRequest.model_validate(
                self.envelope("QueryRunStatus", payload)
            )
        )

    def stop(self):
        return self.services[2].execute(
            StopRunRequest.model_validate(
                self.envelope(
                    "StopRun",
                    dict(
                        reason="operator stop",
                        requested_at=self.now,
                        authority=self.document["payload"]["authority"],
                    ),
                )
            )
        )
