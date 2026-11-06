"""Recovery evidence from an explicit test supervisor; no D5000 assumptions."""

from copy import deepcopy
from math import sqrt

from oilfield_energy.bootstrap.field_initialization import create_recovery_services
from oilfield_energy.workflows.field_initialization.contracts import (
    PrepareResynchronizationRequest,
    ResumeRunRequest,
)
from tests.support.field_execution import Harness
from tests.support.field_snapshots import snapshot_document


class SoftwareRecoveryVerifier:
    allowed = True

    def verify_quiescence(self, grid, authority, evidence):
        return (
            self.allowed
            and evidence.source_ref == "analytic-supervisor:fence"
            and authority.epoch > evidence.previous_authority.epoch
        )


class RecoveryHarness(Harness):
    def __init__(self, root, document=None):
        super().__init__(root, document)
        self.verifier = SoftwareRecoveryVerifier()

    @property
    def recovery_services(self):
        return create_recovery_services(
            self.root, clock=lambda: self.now, transport=self.transport, verifier=self.verifier
        )

    def recovery_request(self, recovery_id="recovery-1"):
        self.sequence += 1
        snap = snapshot_document(
            self.document, self.initial, minute=self.minute, sequence=self.sequence
        )["payload"]
        for d in snap["device_states"]:
            d["p_mw"]["value"], d["q_mvar"]["value"] = self.state[d["resource_id"]]
            if "energy_mwh" in d:
                d["energy_mwh"]["value"] = self.energy
        netp = 1 - sum(x[0] for x in self.state.values())
        netq = -sum(x[1] for x in self.state.values())
        pp, qq, x = netp / 10, netq / 10, 0.001
        w = (1 - 2 * x * qq + sqrt((1 - 2 * x * qq) ** 2 - 4 * x * x * (pp * pp + qq * qq))) / 2
        snap["pcc"]["p_import_mw"]["value"] = netp
        snap["pcc"]["q_import_mvar"]["value"] = netq + x * (pp * pp + qq * qq) / w * 10
        current = self.query().payload.data
        authority = deepcopy(self.document["payload"]["authority"])
        authority["epoch"] += 1
        quiescence = None
        if current.pending_batch_id and self.transport.sent:
            batch = next(
                b
                for b in self.transport.sent
                if b.evidence.command_batch_id == current.pending_batch_id
            )
            quiescence = dict(
                command_batch_id=current.pending_batch_id,
                previous_authority=batch.evidence.request.payload.authority.model_dump(mode="json"),
                fenced_at=self.now,
                source_ref="analytic-supervisor:fence",
                settlements=[
                    dict(
                        command_id=c.command_id,
                        resource_id=c.resource_id,
                        disposition="applied",
                        terminal_at=batch.send_claimed_at,
                        source_ref="analytic-supervisor:terminal",
                    )
                    for c in batch.commands
                ],
            )
        return PrepareResynchronizationRequest.model_validate(
            self.envelope(
                "PrepareResynchronization",
                dict(
                    context_id=self.initial.payload.data.context_id,
                    resynchronization_id=recovery_id,
                    pending_batch_id=current.pending_batch_id,
                    snapshot=snap,
                    planning_inputs=self.document["payload"]["planning_inputs"],
                    authority=authority,
                    quiescence=quiescence,
                    reason="operator verified resynchronization",
                ),
            )
        )

    def prepare_recovery(self, request=None):
        self.proposal = request or self.recovery_request()
        return self.recovery_services[0].execute(self.proposal)

    def resume_request(self, prepared):
        return ResumeRunRequest.model_validate(
            self.envelope(
                "ResumeRun",
                dict(
                    context_id=self.proposal.payload.context_id,
                    resynchronization_id=self.proposal.payload.resynchronization_id,
                    proposed_context_id=prepared.payload.data.proposed_context_id,
                    authority=self.proposal.payload.authority,
                ),
            )
        )

    def adopt(self, resumed):
        assert resumed.payload.outcome == "accepted", resumed
        self.initial = self.initial.model_copy(
            update={
                "payload": self.initial.payload.model_copy(
                    update={
                        "data": self.initial.payload.data.model_copy(
                            update={"context_id": resumed.payload.data.context_id}
                        )
                    }
                )
            }
        )
        self.document["payload"]["authority"] = self.proposal.payload.authority.model_dump(
            mode="json"
        )
        self.snapshot_id = self.proposal.payload.snapshot.snapshot_id
