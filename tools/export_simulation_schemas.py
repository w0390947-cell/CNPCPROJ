"""Generate simulation schemas from the installed package's authority types."""

import argparse
import json
from pathlib import Path

from oilfield_energy.modules.control.contracts import PlantInputs
from oilfield_energy.modules.dispatch.contracts import AdoptedSchedule
from oilfield_energy.modules.studies.contracts import ScenarioCatalog, StudySpec


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for name, model in (
        ("recipe", StudySpec),
        ("plant", PlantInputs),
        ("scenarios", ScenarioCatalog),
        ("adopted_schedule", AdoptedSchedule),
    ):
        (args.output / (name + ".schema.json")).write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
