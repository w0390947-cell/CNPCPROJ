"""Initialize a durable shadow session using the shared admission proofs."""

import json
from collections.abc import Callable
from datetime import datetime
from hashlib import sha256
from time import monotonic
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter, ValidationError

from .certification import certify_conditions
from .contracts import (
    ArtifactReference,
    InitializationEvidence,
    InitializationFailure,
    InitializationResult,
    InitializeRequest,
    InitializeResponse,
    PointMapping,
    ResponsePayload,
)
from .messages import canonical, decode_json, policy_digest
from .ports import ArtifactReader, ExecutionTransport, NetworkAssembler, SessionStore
from .validation import require


class InitializeRun:
    def __init__(
        self,
        store: SessionStore,
        assembler: NetworkAssembler,
        artifacts: ArtifactReader,
        clock: Callable[[], datetime],
        elapsed: Callable[[], float] = monotonic,
        transport: ExecutionTransport | None = None,
    ) -> None:
        self._store, self._assembler, self._artifacts = store, assembler, artifacts
        self._clock, self._elapsed = clock, elapsed
        self._transport = transport

    def execute(self, request: InitializeRequest) -> InitializeResponse:
        # Revalidate a private copy, including any mutable embedded JSON objects.
        request = InitializeRequest.model_validate_json(canonical(request))
        fingerprint = sha256(canonical(request)).hexdigest()
        try:
            return self._store.execute_once(
                request.microgrid_id,
                request.message_id,
                fingerprint,
                lambda: self._prepare(request),
            )
        except InitializationFailure as exc:
            return self._rejected(request, exc)

    def _rejected(
        self, request: InitializeRequest, failure: InitializationFailure
    ) -> InitializeResponse:
        return InitializeResponse(
            message_id=str(uuid4()),
            correlation_id=request.message_id,
            microgrid_id=request.microgrid_id,
            run_id=None,
            created_at=self._clock(),
            payload=ResponsePayload(outcome="rejected", errors=(failure.error,)),
        )

    def _resolve(
        self, value: ArtifactReference | dict[str, JsonValue] | tuple[PointMapping, ...]
    ) -> JsonValue | tuple[PointMapping, ...]:
        if isinstance(value, ArtifactReference):
            content = self._artifacts.read(value)
            require(
                sha256(content).hexdigest() == value.sha256,
                "ARTIFACT_INVALID",
                "Artifact content digest differs",
            )
            return decode_json(content)
        return value

    def _prepare(
        self, request: InitializeRequest
    ) -> tuple[InitializeResponse, InitializationEvidence | None]:
        started = self._elapsed()
        try:
            p = request.payload
            if p.mode == "closed_loop":
                require(
                    self._transport is not None,
                    "CLOSED_LOOP_UNAVAILABLE",
                    "No trusted software execution transport is configured",
                )
                assert self._transport is not None
                timing = p.control_policy.bounded_timing
                observation = p.control_policy.observation_uncertainty
                require(
                    timing is None
                    or timing.profile != "bounded_uncertain_v1"
                    or self._transport.capabilities.timing_policy_sha256
                    == policy_digest(timing),
                    "TIMING_POLICY_UNVERIFIED",
                    "The configured trusted port has not approved this timed-response policy",
                )
                require(
                    observation is None
                    or self._transport.capabilities.observation_policy_sha256
                    == policy_digest(observation),
                    "OBSERVATION_POLICY_UNVERIFIED",
                    "The configured trusted port has not approved this metrology-policy content",
                )
                require(
                    (
                        (
                            timing is None
                            and self._transport.capabilities.timing_profile
                            == "software_synchronous_v1"
                        )
                        or (
                            timing is not None
                            and self._transport.capabilities.timing_profile
                            == timing.profile
                            and self._transport.capabilities.timing_revision
                            == timing.revision
                        )
                    )
                    and p.authority.write_enabled
                    and self._transport.authorized(request.microgrid_id, p.authority),
                    "AUTHORITY_LOST",
                    "Transport did not verify the declared control authority",
                )
                require(
                    p.control_policy.dispatch_lead_seconds == 0,
                    "MODEL_UNSUPPORTED",
                    "Use explicit application windows; dispatch_lead_seconds is reserved for the synchronous profile",
                )
            network_json = self._resolve(p.model_package.network_model)
            network = self._assembler.assemble(
                json.dumps(network_json, allow_nan=False).encode(),
                p.model_package.operating_mode_id,
                p.control_policy,
            )
            points = p.model_package.point_mapping
            mapping = (
                TypeAdapter(tuple[PointMapping, ...]).validate_json(
                    json.dumps(self._resolve(points)).encode()
                )
                if isinstance(points, ArtifactReference)
                else points
            )
            resolved = p.model_package.model_copy(
                update={"network_model": network_json, "point_mapping": mapping}
            )
            if p.mode == "closed_loop":
                writes = {
                    (x.entity_id, x.quantity) for x in mapping if x.access == "write"
                }
                for d in resolved.devices:
                    for axis, enabled in (
                        ("p", d.p_control_enabled),
                        ("q", d.q_control_enabled),
                    ):
                        require(
                            not enabled
                            or (d.resource_id, axis + "_setpoint") in writes,
                            "IDENTITY_MISMATCH",
                            f"Missing authorized {axis} write point for {d.resource_id}",
                        )
            resolved_request = request.model_copy(
                update={"payload": p.model_copy(update={"model_package": resolved})}
            )
            evidence = certify_conditions(
                resolved_request, network, mapping, self._clock, self._elapsed, started
            )
            domain, proof = evidence.domain, evidence.plan
            context = domain.context_id
            task = p.planning_inputs.energy_task
            run_id = str(uuid4())
            result = InitializationResult(
                run_id=run_id,
                context_id=context,
                accepted_versions={
                    "interface": request.interface_version,
                    "algorithm": domain.version,
                    "model": p.model_package.model_revision,
                    "mapping": p.model_package.mapping_revision,
                    "planning": p.planning_inputs.input_revision,
                },
                energy_task_guarantee=evidence.timed_proof.energy_task_guarantee
                if evidence.timed_proof
                else "not_required"
                if task is None
                else "maintained"
                if proof.task_continuation is not None
                else "unproven",
                evidence_ref=f"initialization:{run_id}",
                valid_until=evidence.valid_until,
                mode=p.mode,
                dispatch_enabled=p.mode == "closed_loop",
                missing_requirements=()
                if p.mode == "closed_loop"
                else (
                    "Shadow mode does not grant dispatch permission; configure a supported software execution port for closed_loop.",
                ),
            )
            evidence = evidence.model_copy(update={"request": request})
            return InitializeResponse(
                message_id=str(uuid4()),
                correlation_id=request.message_id,
                microgrid_id=request.microgrid_id,
                run_id=run_id,
                created_at=self._clock(),
                payload=ResponsePayload(outcome="accepted", data=result),
            ), evidence
        except InitializationFailure as exc:
            return self._rejected(request, exc), None
        except (ValueError, ValidationError) as exc:
            return self._rejected(
                request, InitializationFailure("INVALID_INITIALIZATION", str(exc))
            ), None
