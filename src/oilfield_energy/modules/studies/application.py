"""Construct separate forecast and plant inputs from one validated recipe."""

from oilfield_energy.modules.control.contracts import PlantInputs

from .contracts import StudySpec
from .domain import generate_profiles


def generate_inputs(spec: StudySpec) -> tuple[PlantInputs, PlantInputs, PlantInputs]:
    return (
        generate_profiles(spec, resolution=15, forecast="day_ahead"),
        generate_profiles(spec, resolution=15, forecast="intraday"),
        generate_profiles(spec, resolution=1, forecast="plant"),
    )
