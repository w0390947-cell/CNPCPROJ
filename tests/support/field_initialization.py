"""Explicit analytic fixture, never a claim of a real site's approval or data."""

from datetime import datetime, timedelta, timezone

NOW = datetime(2026, 10, 15, 2, tzinfo=timezone.utc)


def request_document():
    at, end = NOW.isoformat(), (NOW + timedelta(hours=2)).isoformat()
    mapping = []

    def measured(kind, entity, quantity, value, scope="device-scope"):
        mid = f"{kind}:{entity}:{quantity}"
        unit = {"p": "MW", "q": "Mvar", "energy": "MWh", "voltage": "pu"}[quantity]
        mapping.append(
            dict(
                mapping_id=mid,
                point_id=mid,
                entity_kind=kind,
                entity_id=entity,
                quantity=quantity,
                access="read",
                raw_unit=unit,
                normalized_unit=unit,
                engineering_multiplier=1.0,
                direction_rule_ref="identity",
                quality_rule_ref="fixture-quality",
                timestamp_rule_ref="fixture-clock",
                scope_ref=scope,
                provenance_ref="analytic-test-fixture",
            )
        )
        return dict(
            value=value,
            observed_at=at,
            quality="good",
            source_kind="measured",
            source_ref=mid,
            value_semantics="instantaneous",
        )

    loads = [
        dict(
            bus_id=b,
            p_mw=measured("bus_load", b, "p", p, b),
            q_mvar=measured("bus_load", b, "q", 0.0, b),
            scope_ref=b,
        )
        for b, p in [("pcc", 0.0), ("load", 1.0)]
    ]
    states = [
        dict(
            resource_id=r,
            p_mw=measured("resource", r, "p", 0.0),
            q_mvar=measured("resource", r, "q", 0.0),
            online=True,
            remote_enabled=True,
            blocked=False,
            status_observed_at=at,
        )
        for r in ["wind", "storage"]
    ]
    states[1]["energy_mwh"] = measured("resource", "storage", "energy", 1.0)
    pcc = dict(
        bus_id="pcc",
        p_import_mw=measured("pcc", "pcc", "p", 1.0),
        q_import_mvar=measured("pcc", "pcc", "q", 0.0),
        voltage_pu=measured("pcc", "pcc", "voltage", 1.0),
    )
    common = dict(
        bus_id="load",
        q_min_mvar=0.0,
        q_max_mvar=0.0,
        s_max_mva=1.0,
        q_abs_over_p_max=None,
        p_control_enabled=True,
        q_control_enabled=False,
        p_change_limit_mw_per_cycle=0.5,
        provenance_ref="analytic-test-fixture",
    )
    devices = [
        dict(
            common,
            resource_id="wind",
            kind="wind",
            p_min_mw=0.0,
            p_max_mw=0.5,
            alpha_p=0.5,
        ),
        dict(
            common,
            resource_id="storage",
            kind="storage",
            p_min_mw=-0.5,
            p_max_mw=0.5,
            alpha_p=1.0,
            energy_min_mwh=0.2,
            energy_max_mwh=2.0,
            eta_charge=0.95,
            eta_discharge=0.95,
        ),
    ]
    network = dict(
        network_id="analytic-fixture",
        base_mva=10.0,
        pcc_bus_id="pcc",
        buses=[
            dict(bus_id=b, name=b, nominal_voltage_kv=10.0) for b in ["pcc", "load"]
        ],
        branches=[
            dict(
                branch_id="line",
                from_bus_id="pcc",
                to_bus_id="load",
                kind="line",
                r_pu=0.0,
                x_pu=0.001,
                s_max_mva=5.0,
                provenance="analytic-test-fixture",
            )
        ],
        operating_modes=[dict(mode_id="normal", description="analytic fixture")],
        default_operating_mode_id="normal",
        provenance=dict(
            source="TEST FIXTURE ONLY; no real field approval",
            dataset_version="test-v1",
            synthetic=False,
            approved_for_field_use=True,
            approved_by="fixture-validator",
            approved_at_utc="2026-10-14T00:00:00Z",
        ),
    )
    forecast_loads = [
        dict(
            bus_id=b,
            p_forecast_mw=p,
            q_forecast_mvar=0.0,
            p_bounds_mw=dict(low=p, high=p),
            q_bounds_mvar=dict(low=0.0, high=0.0),
        )
        for b, p in [("pcc", 0.0), ("load", 1.0)]
    ]
    plan = dict(
        context_id=None,
        input_revision="forecast-1",
        issued_at=(NOW - timedelta(minutes=15)).isoformat(),
        effective_from=at,
        valid_until=end,
        source_ref="analytic-forecast",
        forecast_interpolation="linear",
        forecast_points=[
            dict(
                at=t,
                bus_loads=forecast_loads,
                renewables=[
                    dict(
                        resource_id="wind",
                        available_forecast_mw=0.5,
                        minimum_available_mw=0.45,
                        bound_method_ref="fixture-bound",
                    )
                ],
            )
            for t in [at, end]
        ],
        tariff_intervals=[
            dict(start_at=at, end_at=end, import_price_cny_per_mwh=500.0)
        ],
        cost_rates=dict(
            curtailment_cny_per_mwh=10.0,
            storage_throughput_cny_per_mwh=10.0,
            loss_cny_per_mwh=10.0,
            source_ref="fixture-rates",
        ),
        energy_task=dict(
            task_id="task-1",
            deadline_at=end,
            energy_min_mwh=0.99,
            energy_max_mwh=1.01,
            required=True,
            source_ref="fixture-task",
        ),
        terminal_boundary_ref="fixture-terminal",
        terminal_boundary_policy="last_bounds_until_valid_until",
    )
    policy = dict(
        execution_step_seconds=60,
        planning_update_seconds=900,
        planning_horizon_seconds=1800,
        response_deadline_seconds=600,
        p_tracking_tolerance_mw=0.05,
        q_tracking_tolerance_mvar=0.05,
        pcc_model_tolerance_mw=0.01,
        pcc_model_tolerance_mvar=0.01,
        alpha_q=0.5,
        q_change_limit_mvar_per_cycle=0.9,
        settling_resolution=0.0001,
        voltage_min_pu=0.95,
        voltage_max_pu=1.05,
        pcc_min_mw=0.1,
        pcc_max_mw=5.0,
        pf_min=0.9,
        telemetry_max_age_seconds=30.0,
        snapshot_max_skew_seconds=1.0,
        future_clock_skew_seconds=0.0,
        compute_budget_seconds=30.0,
        dispatch_lead_seconds=1.0,
        transport_timeout_seconds=5.0,
        feedback_timeout_seconds=5.0,
        model_error_policy_ref="exact-normalized-snapshot-v1",
        availability_method_ref="fixture-availability",
        local_fallback_policy_ref="fixture-local",
    )
    return dict(
        interface_version="single-microgrid-field-v0.1-draft",
        operation="InitializeRun",
        message_id="message-1",
        correlation_id=None,
        microgrid_id="analytic-grid",
        run_id=None,
        created_at=at,
        payload=dict(
            mode="shadow",
            model_package=dict(
                network_model=network,
                operating_mode_id="normal",
                devices=devices,
                point_mapping=mapping,
                load_scopes=[
                    dict(
                        scope_ref=b,
                        bus_id=b,
                        members=[b],
                        semantics="gross_load",
                        provenance_ref="fixture",
                    )
                    for b in ["pcc", "load"]
                ],
                model_revision="model-1",
                mapping_revision="mapping-1",
                provenance_ref="fixture",
            ),
            initial_snapshot=dict(
                snapshot_id="snapshot-1",
                context_id=None,
                sequence=1,
                snapshot_at=at,
                received_at=at,
                model_revision="model-1",
                mapping_revision="mapping-1",
                operating_mode_id="normal",
                permission_revision="permission-1",
                bus_loads=loads,
                device_states=states,
                renewable_availability=[
                    dict(
                        resource_id="wind",
                        method_ref="fixture-availability",
                        available_p_mw=dict(
                            value=0.5,
                            observed_at=at,
                            quality="good",
                            source_kind="estimated",
                            source_ref="fixture-estimate",
                            value_semantics="instantaneous",
                        ),
                    )
                ],
                pcc=pcc,
                operating_state_ref="normal-state",
            ),
            planning_inputs=plan,
            control_policy=policy,
            authority=dict(
                owner_id="shadow-reader",
                epoch=1,
                valid_until=end,
                permission_revision="permission-1",
                write_enabled=False,
            ),
        ),
    )
