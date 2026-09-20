import pytest
from pydantic import ValidationError

from oilfield_energy.modules.dispatch.contracts import CoordinationIterationTrace


def trace_record():
    # Small explicit protocol fixture, not a computed dispatch or safety certificate.
    return dict(
        communication_tick=2,
        coordination_epoch=1,
        global_updated=True,
        time_hours=(0.0, 12.0),
        regions=(
            dict(
                region="SC",
                response_epoch=1,
                response_sent_tick=2,
                fresh=True,
                outage=False,
                fallback=False,
                proposal_p_mw=(2.0, 3.0),
                proposal_q_mvar=(0.5, 0.6),
                reference_p_mw=(1.8, 2.8),
                reference_q_mvar=(0.4, 0.5),
            ),
        ),
    )


@pytest.mark.parametrize(
    "change",
    [
        {"time_hours": (12.0, 0.0)},
        {"time_hours": (0.0,)},
        {"time_hours": (0.0, float("nan"))},
        {"regions": ()},
    ],
)
def test_invalid_time_and_region_axes_rejected(change):
    with pytest.raises(ValidationError):
        CoordinationIterationTrace.model_validate({**trace_record(), **change})


@pytest.mark.parametrize(
    "change",
    [
        {"response_epoch": 0},
        {"response_sent_tick": 3},
        {"proposal_p_mw": None},
        {"proposal_q_mvar": (1.0,)},
        {"reference_p_mw": (1.0, float("inf"))},
        {"fresh": False},
    ],
)
def test_incomplete_nonfinite_or_mixed_epoch_evidence_rejected(change):
    record = trace_record()
    record["regions"] = ({**record["regions"][0], **change},)
    with pytest.raises(ValidationError):
        CoordinationIterationTrace.model_validate(record)


def test_trace_owns_immutable_values():
    record = trace_record()
    trace = CoordinationIterationTrace.model_validate(record)
    record["regions"][0]["reference_p_mw"] = (9.0, 9.0)
    assert trace.regions[0].reference_p_mw == (1.8, 2.8)
    with pytest.raises(ValidationError):
        trace.regions[0].fresh = False


def test_duplicate_regions_rejected():
    record = trace_record()
    record["regions"] = record["regions"] * 2
    with pytest.raises(ValidationError, match="unique region"):
        CoordinationIterationTrace.model_validate(record)
