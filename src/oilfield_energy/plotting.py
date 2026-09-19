"""算例结果图。图中使用英文标签，避免跨平台中文字体缺失。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

os.environ.setdefault("MPLCONFIGDIR", str(Path.cwd() / "results" / ".matplotlib"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from .data import ProjectCase
from .model import OptimizationResult


def plot_microgrid(
    path: Path,
    case: ProjectCase,
    optimized: OptimizationResult,
    baseline: OptimizationResult,
    name: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    t = case.time_hours
    opt = optimized.microgrids[name]
    base = baseline.microgrids[name]
    fig, axes = plt.subplots(3, 1, figsize=(11, 9), sharex=True)
    axes[0].plot(t, opt["p_grid_mw"], label="PCC import (optimized)", linewidth=2)
    axes[0].plot(t, base["p_grid_mw"], label="PCC import (baseline)", linestyle="--")
    axes[0].axhline(case.assumptions.no_reverse_margin_mw, color="red", linestyle=":", label="no-reverse margin")
    axes[0].set_ylabel("MW")
    axes[0].legend(ncol=2)
    axes[0].grid(alpha=0.25)

    axes[1].plot(t, opt["wind_available_mw"], label="wind available", color="#68a357")
    axes[1].plot(t, opt["wind_used_mw"], label="wind used", color="#246b35")
    axes[1].plot(t, opt["pv_available_mw"], label="PV available", color="#f6b73c")
    axes[1].plot(t, opt["pv_used_mw"], label="PV used", color="#ce7e00")
    axes[1].set_ylabel("MW")
    axes[1].legend(ncol=4)
    axes[1].grid(alpha=0.25)

    net_storage = np.asarray(opt["storage_discharge_mw"]) - np.asarray(opt["storage_charge_mw"])
    axes[2].plot(t, net_storage, label="storage net discharge", color="#4864aa")
    ax_soc = axes[2].twinx()
    ax_soc.plot(t, np.asarray(opt["storage_energy_mwh"])[1:], label="stored energy", color="#9a4ea3")
    axes[2].set_ylabel("MW")
    ax_soc.set_ylabel("MWh")
    axes[2].set_xlabel("Hour")
    axes[2].grid(alpha=0.25)
    lines = axes[2].get_lines() + ax_soc.get_lines()
    axes[2].legend(lines, [line.get_label() for line in lines], loc="upper left")
    fig.suptitle(f"{name} dispatch")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def plot_cluster(
    path: Path,
    case: ProjectCase,
    optimized: OptimizationResult,
    baseline: OptimizationResult,
    names: Sequence[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    t = case.time_hours
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    axes[0].plot(t, optimized.cluster["total_import_mw"], label="optimized cluster import", linewidth=2)
    axes[0].plot(t, baseline.cluster["total_import_mw"], label="baseline cluster import", linestyle="--")
    axes[0].axhline(case.cluster_import_limit_mw, color="red", linestyle=":", label="cluster limit")
    axes[0].set_ylabel("MW")
    axes[0].legend()
    axes[0].grid(alpha=0.25)
    for name in names:
        axes[1].plot(t, optimized.microgrids[name]["p_grid_mw"], label=name)
    axes[1].set_xlabel("Hour")
    axes[1].set_ylabel("PCC import (MW)")
    axes[1].legend(ncol=len(names))
    axes[1].grid(alpha=0.25)
    fig.suptitle("Coordinated multi-microgrid dispatch")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
