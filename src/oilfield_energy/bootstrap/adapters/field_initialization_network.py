"""Registered bridge from the existing public network inventory to execution."""

import json
from fractions import Fraction as F
from typing import Any

from pydantic import JsonValue, TypeAdapter

from oilfield_energy.modules.power_flow.contracts import (
    EnvelopeBranch,
    PowerInterval,
    RadialEnvelope,
)
from oilfield_energy.network_model import NetworkModelV2, assess_network_model
from oilfield_energy.workflows.field_initialization.contracts import (
    ControlPolicy,
    InitializationFailure,
)


def _reject_unknown(
    value: JsonValue, schema: dict[str, Any], root: dict[str, Any]
) -> None:
    if "$ref" in schema:
        schema = root["$defs"][schema["$ref"].split("/")[-1]]
    if "anyOf" in schema:
        choices = [x for x in schema["anyOf"] if x.get("type") != "null"]
        if value is not None and len(choices) == 1:
            _reject_unknown(value, choices[0], root)
        return
    if isinstance(value, dict) and "properties" in schema:
        extra = set(value) - set(schema["properties"])
        if extra:
            raise InitializationFailure(
                "INVALID_MESSAGE", f"Unknown network fields: {sorted(extra)}"
            )
        for key, child in value.items():
            _reject_unknown(child, schema["properties"][key], root)
    if isinstance(value, list) and "items" in schema:
        for child in value:
            _reject_unknown(child, schema["items"], root)


class FieldNetworkAssembler:
    def assemble(
        self, content: bytes, mode: str, policy: ControlPolicy
    ) -> RadialEnvelope:
        adapter = TypeAdapter(NetworkModelV2)
        schema = adapter.json_schema()
        _reject_unknown(json.loads(content), schema, schema)
        model = adapter.validate_json(content, strict=True)
        provenance = model.provenance
        if (
            provenance.approved_at_utc is None
            or provenance.approved_at_utc.utcoffset() is None
        ):
            raise InitializationFailure(
                "MODEL_UNSUPPORTED",
                "Field network approval requires an explicit timezone-aware timestamp",
            )
        ready = assess_network_model(model, mode, require_field_approval=True)
        if ready.blockers or ready.resolved_network is None:
            raise InitializationFailure("MODEL_UNSUPPORTED", "; ".join(ready.blockers))
        resolved = ready.resolved_network
        if any(x.phase_shift_degrees != 0 for x in resolved.branches):
            raise InitializationFailure(
                "MODEL_UNSUPPORTED",
                "Single initialization does not support phase shifting",
            )
        buses = tuple(x.bus_id for x in resolved.buses)
        if len(buses) > 512:
            raise InitializationFailure(
                "MODEL_UNSUPPORTED", "Initialization supports at most 512 buses"
            )
        voltage = (
            policy.observation_uncertainty.root_voltage_bounds_pu
            if policy.observation_uncertainty
            else None
        )
        return RadialEnvelope(
            slack_voltage_bounds_pu=PowerInterval(low=voltage.low, high=voltage.high)
            if voltage
            else None,
            network_id=resolved.network_id,
            buses=buses,
            branches=tuple(
                EnvelopeBranch(
                    parent=buses.index(b.parent),
                    child=buses.index(b.child),
                    r_pu=b.r_pu,
                    x_pu=b.x_pu,
                    tap=b.tap_ratio,
                    rating_mva=b.s_max_mva,
                )
                for b in resolved.branches
            ),
            slack=buses.index(resolved.pcc_bus_id),
            base_mva=resolved.base_mva,
            slack_voltage_pu=float((F(voltage.low) + F(voltage.high)) / 2)
            if voltage
            else 1.0,
            shunt_q_mvar=tuple(resolved.shunt_q_nominal_mvar_by_bus[b] for b in buses),
            voltage_min_pu=policy.voltage_min_pu,
            voltage_max_pu=policy.voltage_max_pu,
            pcc_min_mw=policy.pcc_min_mw,
            pcc_max_mw=policy.pcc_max_mw,
            pf_min=policy.pf_min,
        )
