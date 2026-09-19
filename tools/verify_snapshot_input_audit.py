"""Item 12: synthetic input transformations and archived fixed-state replay."""

import argparse
import csv
import hashlib
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import oilfield_energy
from oilfield_energy.bootstrap.field_dataset import create_field_dataset_workflow

AT = datetime.fromisoformat("2026-09-13T12:00:00+08:00")
CASES = (
    "baseline",
    "units",
    "directions",
    "timezone",
    "row_order",
    "future",
    "bad_latest",
    "stale",
    "skew",
    "duplicate",
    "offline_nonzero",
    "offline_zero",
    "missing_point",
    "device_limit",
    "hash_mismatch",
)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def run_case(source: Path, output: Path, name: str) -> dict:
    dataset = output / name / "input"
    shutil.copytree(source, dataset)
    mapping = read(dataset / "point_mapping.json")
    points = {p["point_id"]: p for p in mapping["payload"]["points"]}
    with (dataset / "measurements.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if name == "units":
        for row in rows:
            point = points[row["point"]]
            if point["unit"] in {"kW", "kvar", "kV"}:
                point["unit"] = {"kW": "W", "kvar": "var", "kV": "V"}[point["unit"]]
                row["value"] = str(float(row["value"]) * 1000)
    elif name == "directions":
        inverse = {
            "injection": "withdrawal",
            "withdrawal": "injection",
            "consumption": "supply",
            "supply": "consumption",
        }
        for row in rows:
            point = points[row["point"]]
            if point["quantity"] in {"p", "q"}:
                point["positive_direction"] = inverse[point["positive_direction"]]
                row["value"] = str(-float(row["value"]))
    elif name == "timezone":
        for row in rows:
            row["time"] = datetime.fromisoformat(row["time"]).astimezone(timezone.utc).isoformat()
    elif name == "row_order":
        rows.reverse()
    elif name == "future":
        rows.append(
            dict(rows[0], time=(AT + timedelta(seconds=1)).isoformat(), value="9999", quality="BAD")
        )
    elif name == "bad_latest":
        rows.append(dict(rows[0], time=(AT - timedelta(seconds=1)).isoformat()))
        rows[0]["quality"] = "BAD"
    elif name in {"stale", "skew"}:
        rows[0]["time"] = (AT - timedelta(seconds=61 if name == "stale" else 6)).isoformat()
    elif name == "duplicate":
        rows.append(dict(rows[0]))
    elif name.startswith("offline_"):
        for row in rows:
            if row["point"] == "WT1.in_service" or (
                name == "offline_zero" and row["point"] in {"WT1.p", "WT1.q"}
            ):
                row["value"] = "0"
    elif name == "missing_point":
        rows.pop(0)
    elif name == "device_limit":
        devices = read(dataset / "devices.json")
        devices["payload"]["devices"][0]["p_max_mw"] = 0.5
        write(dataset / "devices.json", devices)
    write(dataset / "point_mapping.json", mapping)
    with (dataset / "measurements.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["point", "time", "value", "quality"])
        writer.writeheader()
        writer.writerows(rows)
    manifest = read(dataset / "manifest.json")
    for ref in manifest["files"]:
        ref["sha256"] = hashlib.sha256((dataset / ref["path"]).read_bytes()).hexdigest()
    write(dataset / "manifest.json", manifest)
    if name == "hash_mismatch":
        with (dataset / "measurements.csv").open("a", encoding="utf-8") as stream:
            stream.write("\n")
    result = create_field_dataset_workflow().execute(
        dataset / "manifest.json", output / name / "run", operation="run", at=AT, demo=True
    )
    path = result.output / "power_flow.json"
    report = read(path) if path.exists() else read(result.output / "dataset_run.json")
    return {"status": result.status, "exit_code": result.exit_code, "report": report}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    results = {name: run_case(args.source, args.output, name) for name in CASES}
    base = results["baseline"]["report"]
    metrics = (
        "pcc_import_mw",
        "pcc_reactive_mvar",
        "loss_mw",
        "reactive_loss_mvar",
        "power_factor",
    )
    checks = {}
    for name in ("units", "directions", "timezone", "row_order", "future"):
        report = results[name]["report"]
        checks[name + "_equivalent"] = (
            results[name]["status"] == results["baseline"]["status"]
            and all(
                report["operating_point"][key] == base["operating_point"][key]
                for key in ("point_id", "source", "quality_valid", "coherent")
            )
            and all(
                set(report["operating_point"][key]) == set(base["operating_point"][key])
                and all(
                    abs(value - base["operating_point"][key][bus]) <= 1e-10
                    for bus, value in report["operating_point"][key].items()
                )
                for key in ("p_demand_mw_by_bus", "q_demand_mvar_by_bus")
            )
            and all(abs(report["flow"][key] - base["flow"][key]) <= 1e-10 for key in metrics)
        )
    for name in (
        "bad_latest",
        "stale",
        "skew",
        "duplicate",
        "offline_nonzero",
        "missing_point",
        "hash_mismatch",
    ):
        checks[name + "_rejected"] = (
            results[name]["status"] == "invalid_input" and results[name]["exit_code"] == 2
        )
    limited = results["device_limit"]["report"]
    checks["limit_preserved_without_clipping"] = (
        results["device_limit"]["status"] == "violation"
        and limited["snapshot"]["devices"][0]["p_mw"] == 1.0
        and any(v["code"] == "P_MAX" for v in limited["device_violations"])
    )
    off = results["offline_zero"]["report"]
    checks["offline_injection_removed"] = (
        off["snapshot"]["devices"][0]["in_service"] is False
        and abs(
            sum(off["operating_point"]["p_demand_mw_by_bus"].values())
            - sum(base["operating_point"]["p_demand_mw_by_bus"].values())
            - 1.0
        )
        <= 1e-10
    )
    for name in ("baseline", "offline_zero", "device_limit"):
        report = results[name]["report"]
        flow, point = report["flow"], report["operating_point"]
        checks[name + "_pq_balance"] = (
            abs(flow["pcc_import_mw"] - sum(point["p_demand_mw_by_bus"].values()) - flow["loss_mw"])
            < 1e-8
            and abs(
                flow["pcc_reactive_mvar"]
                - sum(point["q_demand_mvar_by_bus"].values())
                - flow["reactive_loss_mvar"]
            )
            < 1e-8
        )
    # Destroy only the private copied source, then replay from the result archive.
    (args.output / "baseline/input/measurements.csv").write_text(
        "invalid copied source", encoding="utf-8"
    )
    replay = create_field_dataset_workflow().execute(
        args.output / "baseline/run/dataset/manifest.json",
        args.output / "replay",
        operation="run",
        at=AT,
        demo=True,
    )
    checks["archive_replay_independent"] = (
        read(replay.output / "power_flow.json")["flow"] == base["flow"]
    )
    summary = {
        "package": str(Path(oilfield_energy.__file__).resolve()),
        "checks": checks,
        "passed": all(checks.values()),
        "cases": {
            name: {
                "status": r["status"],
                "exit_code": r["exit_code"],
                "reasons": r["report"].get("reasons", []),
            }
            for name, r in results.items()
        },
        "baseline_metrics": {key: base["flow"][key] for key in metrics},
    }
    write(args.output / "summary.json", summary)
    print(json.dumps(summary, indent=2))
    raise SystemExit(0 if summary["passed"] else 1)


if __name__ == "__main__":
    main()
