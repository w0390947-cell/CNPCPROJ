from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi import HTTPException

from oilfield_energy.service import ScenarioType, SimulationRequest
from oilfield_energy.web_api import app, health, router, run_quick_simulation


class WebApiContractTests(unittest.TestCase):
    def test_health_reports_required_solver_capabilities(self) -> None:
        response = health()
        self.assertEqual(response.status, "ok")
        self.assertIn("SCIP", response.solvers)
        self.assertIn("CLARABEL", response.solvers)

    def test_openapi_exposes_health_and_quick_simulation(self) -> None:
        schema = app.openapi()
        self.assertIn("/api/health", schema["paths"])
        self.assertIn("/api/simulations/quick", schema["paths"])
        self.assertIn("/api/simulations", schema["paths"])
        self.assertIn("/api/simulations/{simulation_id}", schema["paths"])
        self.assertIn("/api/simulations/{simulation_id}/cancel", schema["paths"])
        self.assertIn("/api/simulations/{simulation_id}/result", schema["paths"])
        self.assertIn("/api/demo/catalog", schema["paths"])
        self.assertIn("/api/demo/telemetry", schema["paths"])
        self.assertIn("/api/demo/commands", schema["paths"])
        self.assertIn("/api/demo/fault", schema["paths"])
        self.assertIn("/api/demo/rearm", schema["paths"])
        self.assertIn("SimulationRequest", schema["components"]["schemas"])
        self.assertIn("SimulationResult", schema["components"]["schemas"])
        websocket_paths = {route.path for route in router.routes if hasattr(route, "path")}
        self.assertIn("/api/simulations/{simulation_id}/stream", websocket_paths)

    def test_quick_endpoint_runs_real_solver(self) -> None:
        response = run_quick_simulation(SimulationRequest(steps=4))
        self.assertTrue(response.executive_summary.overall_passed)
        self.assertEqual(len(response.timeseries), 4)

    def test_quick_endpoint_rejects_formal_size(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            run_quick_simulation(SimulationRequest(steps=96))
        self.assertEqual(caught.exception.status_code, 422)

    def test_quick_endpoint_rejects_non_single_scenario(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            run_quick_simulation(SimulationRequest(
                scenario_type=ScenarioType.GROUP_CONTROL,
                steps=4,
            ))
        self.assertEqual(caught.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
