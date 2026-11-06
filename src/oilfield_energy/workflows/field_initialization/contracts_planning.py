"""Dated forecasts and bounded terminal assumptions, independent of transport."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StrictBool, StrictFloat, model_validator

from .contracts_values import Bounds, Name, Nonnegative, Value


class ForecastLoad(Value):
    bus_id: Name
    p_forecast_mw: Nonnegative
    q_forecast_mvar: StrictFloat
    p_bounds_mw: Bounds
    q_bounds_mvar: Bounds

    @model_validator(mode="after")
    def inside(self) -> "ForecastLoad":
        if self.p_bounds_mw.low < 0 or not (
            self.p_bounds_mw.low <= self.p_forecast_mw <= self.p_bounds_mw.high
            and self.q_bounds_mvar.low
            <= self.q_forecast_mvar
            <= self.q_bounds_mvar.high
        ):
            raise ValueError("forecast outside declared load bounds")
        return self


class ForecastRenewable(Value):
    resource_id: Name
    available_forecast_mw: Nonnegative
    minimum_available_mw: Nonnegative
    bound_method_ref: Name

    @model_validator(mode="after")
    def inside(self) -> "ForecastRenewable":
        if self.minimum_available_mw > self.available_forecast_mw:
            raise ValueError("renewable lower bound exceeds forecast")
        return self


class ForecastPoint(Value):
    at: AwareDatetime
    bus_loads: tuple[ForecastLoad, ...]
    renewables: tuple[ForecastRenewable, ...]


class Tariff(Value):
    start_at: AwareDatetime
    end_at: AwareDatetime
    import_price_cny_per_mwh: Nonnegative


class Rates(Value):
    curtailment_cny_per_mwh: Nonnegative
    storage_throughput_cny_per_mwh: Nonnegative
    loss_cny_per_mwh: Nonnegative
    source_ref: Name


class EnergyTask(Value):
    task_id: Name
    deadline_at: AwareDatetime
    energy_min_mwh: Nonnegative
    energy_max_mwh: Nonnegative
    required: StrictBool
    source_ref: Name

    @model_validator(mode="after")
    def bounds(self) -> "EnergyTask":
        if self.energy_min_mwh > self.energy_max_mwh:
            raise ValueError("task energy range reversed")
        return self


class PlanningData(Value):
    input_revision: Name
    issued_at: AwareDatetime
    effective_from: AwareDatetime
    valid_until: AwareDatetime
    source_ref: Name
    forecast_interpolation: Literal["linear", "hold"]
    forecast_points: Annotated[
        tuple[ForecastPoint, ...], Field(min_length=2, max_length=1441)
    ]
    tariff_intervals: Annotated[
        tuple[Tariff, ...], Field(min_length=1, max_length=1440)
    ]
    cost_rates: Rates
    energy_task: EnergyTask | None
    terminal_boundary_ref: Name
    # An explicit declaration: never inferred from a last point or a missing file.
    terminal_boundary_policy: Literal["last_bounds_until_valid_until"]


class PlanningInputs(PlanningData):
    context_id: None
