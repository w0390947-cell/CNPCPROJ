"""Bounded coordinate/realize/diagnose feedback on one immutable planning window.

Numerical payloads are opaque here. Callbacks must use the same input snapshot
and must not advance the plant, publish commands or mutate the supplied maps.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType
from typing import Generic, TypeVar, cast

from oilfield_energy.modules.dispatch.api import (
    cluster_power_factor_tracking_margin,
    equivalent_coordination_calibration,
    tighten_cluster_power_factor_reference,
)
from oilfield_energy.modules.dispatch.contracts import (
    CoordinationNetworkCalibration,
    CoordinationRecoveryPolicy,
    NetworkCandidatePolicy,
    NetworkCandidateSelection,
    NetworkCoordinationPolicy,
    PCCTrackingLimits,
    RegionalNetworkCandidate,
)
from oilfield_energy.modules.power_flow.contracts import ClusterPowerFactorPolicy

from .candidates import NetworkCandidatePool
from .contracts import (
    CoordinationRecoveryReport,
    CoordinationRecoveryRound,
    RecoveryStopReason,
    RegionalCalibrationFeedback,
)

T = TypeVar("T")
CalibrationMap = Mapping[str, CoordinationNetworkCalibration]


@dataclass(frozen=True)
class RecoveryTrial(Generic[T]):
    payload: T
    evidence: CoordinationRecoveryRound


@dataclass(frozen=True)
class RecoveryResult(Generic[T]):
    payload: T | None
    report: CoordinationRecoveryReport


@dataclass
class _ReferenceState:
    calibration: CalibrationMap | None
    margin_mvar: tuple[float, ...]


def run_reference_recovery(
    *,
    names: tuple[str, ...],
    time_minutes: tuple[float, ...],
    initial_calibration: CalibrationMap | None,
    policy: CoordinationRecoveryPolicy | None,
    attempt: Callable[
        [CalibrationMap | None, int, tuple[float, ...]], RecoveryTrial[T]
    ],
    diagnose: Callable[[T, int], tuple[RegionalCalibrationFeedback, ...]] | None,
    power_factor_policy: ClusterPowerFactorPolicy | None = None,
    tracking_limits: PCCTrackingLimits | None = None,
    network_candidate_policy: NetworkCandidatePolicy | None = None,
    import_limit_mw: float | None = None,
    attempt_candidates: Callable[
        [CalibrationMap, int, tuple[float, ...], tuple[RegionalNetworkCandidate, ...]],
        RecoveryTrial[T],
    ]
    | None = None,
    network_coordination_policy: NetworkCoordinationPolicy | None = None,
    attempt_joint: Callable[[T, int, tuple[float, ...]], RecoveryTrial[T]]
    | None = None,
) -> RecoveryResult[T]:
    """Retry only reference-related rejection, with complete current AC witnesses.

    Candidate feedback can progress even when calibration repeats. Failed
    diagnostics, no new feedback, exhausted budgets or a nonconverged coordinator
    stop without adoption. Neither calibration nor finite candidate search is a
    globally valid feasibility cut or a completeness guarantee.
    """
    if (
        not names
        or len(set(names)) != len(names)
        or any(not n.strip() for n in names)
        or not time_minutes
        or any(not isfinite(t) or t < 0 for t in time_minutes)
        or any(b <= a for a, b in zip(time_minutes, time_minutes[1:]))
    ):
        raise ValueError("recovery requires unique regions and a planning time axis")
    current: CalibrationMap | None = (
        MappingProxyType[str, CoordinationNetworkCalibration](dict(initial_calibration))
        if initial_calibration is not None
        else None
    )
    if current is not None and (
        set(current) != set(names)
        or any(len(v.p_loss_mw) != len(time_minutes) for v in current.values())
    ):
        raise ValueError("initial calibration does not cover the recovery window")
    seen: list[CalibrationMap] = [current] if current is not None else []
    state: _ReferenceState = _ReferenceState(current, (0.0,) * len(time_minutes))
    robust_margin = (
        cluster_power_factor_tracking_margin(
            target=power_factor_policy.target,
            region_count=len(names),
            tracking=tracking_limits,
        )
        if power_factor_policy is not None and tracking_limits is not None
        else None
    )
    if power_factor_policy is not None and robust_margin is None:
        raise ValueError("PF recovery requires the actual regional tracking limits")
    history: list[CoordinationRecoveryRound] = []
    payload: T | None = None
    pool: NetworkCandidatePool | None = None
    if network_coordination_policy is not None and (
        attempt_joint is None or power_factor_policy is None or tracking_limits is None
    ):
        raise ValueError(
            "network physical recovery requires a joint port and adoption policies"
        )
    if network_candidate_policy is not None:
        if (
            policy is None
            or tracking_limits is None
            or power_factor_policy is None
            or import_limit_mw is None
            or not isfinite(import_limit_mw)
            or import_limit_mw <= 0
            or attempt_candidates is None
        ):
            raise ValueError(
                "network candidate recovery requires its complete policy and numerical port"
            )
        pool = NetworkCandidatePool(
            names=names,
            time_minutes=time_minutes,
            policy=network_candidate_policy,
            recovery=policy,
            tracking=tracking_limits,
            power_factor=power_factor_policy,
            import_limit_mw=import_limit_mw,
        )

    def finish(stop: RecoveryStopReason, reason: str = "") -> RecoveryResult[T]:
        final_payload: T | None = payload
        joint_margin: tuple[float, ...] = state.margin_mvar
        # A complete coordinated window is required. Communication failure,
        # malformed evidence and unexpected exceptions never trigger this port.
        if (
            network_coordination_policy is not None
            and attempt_joint is not None
            and final_payload is not None
            and history
            and stop
            in {
                "stagnated",
                "budget_exhausted",
                "feedback_unavailable",
                "joint_rejected",
                "regional_unresolved",
            }
            and history[-1].outcome
            in {
                "regional_rejected",
                "regional_unresolved",
                "joint_rejected",
                "power_factor_rejected",
            }
        ):
            for _ in range(network_coordination_policy.max_joint_attempts):
                number = len(history) + 1
                try:
                    trial = attempt_joint(final_payload, number, joint_margin)
                    row = trial.evidence
                    if (
                        row.round != number
                        or row.joint_network is None
                        or row.reference_method != "joint-network"
                    ):
                        raise ValueError(
                            "joint port omitted its independent physical evidence"
                        )
                    if row.joint_network.time_minutes != time_minutes:
                        raise ValueError(
                            "joint restoration time axis differs from the frozen window"
                        )
                    final_payload = trial.payload
                    history.append(
                        row.model_copy(
                            update={"power_factor_margin_mvar": joint_margin}
                        )
                    )
                    if row.outcome == "passed":
                        stop, reason = "accepted", ""
                        break
                    stop, reason = "regional_unresolved", row.reason
                    if row.outcome != "power_factor_rejected" or robust_margin is None:
                        break
                    updated: tuple[float, ...] = tuple(
                        float(robust_margin) for _ in time_minutes
                    )
                    if joint_margin == updated:
                        break
                    joint_margin = updated
                except (ValueError, RuntimeError, FloatingPointError) as exc:
                    stop, reason = "error", f"联合网络恢复失败：{exc}"
                    break
        return RecoveryResult(
            final_payload,
            CoordinationRecoveryReport(
                version="coordination-recovery-v3"
                if network_coordination_policy is not None
                else "coordination-recovery-v2"
                if pool is not None
                else "coordination-recovery-v1",
                policy=policy,
                power_factor_policy=power_factor_policy,
                network_candidate_policy=network_candidate_policy,
                network_coordination_policy=network_coordination_policy,
                accepted=stop == "accepted",
                stop_reason=stop,
                reason=reason,
                rounds=tuple(history),
            ),
        )

    maximum = policy.max_recoordinations if policy is not None else 0
    selected: NetworkCandidateSelection
    for number in range(1, maximum + 2):
        used_candidates = (
            pool.take_pending() if isinstance(pool, NetworkCandidatePool) else ()
        )
        try:
            trial: RecoveryTrial[T]
            if used_candidates:
                assert attempt_candidates is not None and state.calibration is not None
                trial = attempt_candidates(
                    state.calibration, number, state.margin_mvar, used_candidates
                )
            else:
                trial = attempt(state.calibration, number, state.margin_mvar)
            record = trial.evidence
            if record.round != number or record.feedback:
                raise ValueError("unexpected recovery round or premature feedback")
            if (
                record.outcome in {"passed", "power_factor_rejected"}
                and power_factor_policy is not None
            ):
                pf = record.cluster_power_factor
                if (
                    pf is None
                    or pf.policy != power_factor_policy
                    or tuple(p.time_minutes for p in pf.points) != time_minutes
                    or (record.outcome == "passed" and pf.status != "passed")
                    or (
                        record.outcome == "power_factor_rejected"
                        and (
                            pf.status != "violated"
                            or any(
                                p.p_mw <= 0 or p.status == "unknown" for p in pf.points
                            )
                        )
                    )
                ):
                    raise ValueError(
                        "missing or inconsistent joint power factor evidence"
                    )
            if record.p_target_mw or record.q_target_mvar:
                if (
                    set(record.p_target_mw) != set(names)
                    or set(record.q_target_mvar) != set(names)
                    or any(
                        len(v) != len(time_minutes)
                        for v in (
                            *record.p_target_mw.values(),
                            *record.q_target_mvar.values(),
                        )
                    )
                ):
                    raise ValueError(
                        "coordination targets do not cover the recovery window"
                    )
            payload = trial.payload
            history.append(
                record.model_copy(
                    update={
                        "calibration_used": dict(state.calibration)
                        if state.calibration is not None
                        else None,
                        "network_candidates_used": used_candidates,
                        "power_factor_margin_mvar": state.margin_mvar
                        if power_factor_policy is not None
                        else (),
                    }
                )
            )
        except (ValueError, RuntimeError, FloatingPointError) as exc:
            history.append(
                CoordinationRecoveryRound(
                    round=number,
                    outcome="error",
                    reference_method="network-socp-admm" if network_coordination_policy is not None else "aggregate-admm",
                    reason=str(exc),
                    calibration_used=dict(state.calibration)
                    if state.calibration is not None
                    else None,
                    network_candidates_used=used_candidates,
                    power_factor_margin_mvar=state.margin_mvar
                    if power_factor_policy is not None
                    else (),
                )
            )
            return finish("error", str(exc))
        if record.outcome == "passed":
            return finish("accepted")
        if record.outcome == "power_factor_rejected":
            if policy is None or power_factor_policy is None or tracking_limits is None:
                return finish("disabled", "未配置集群功率因数参考恢复策略")
            assert isinstance(power_factor_policy, ClusterPowerFactorPolicy)
            if number > maximum:
                return finish(
                    "budget_exhausted", "跨层恢复轮次已耗尽，集群功率因数仍不合格"
                )
            assert record.cluster_power_factor is not None
            points = record.cluster_power_factor.points
            tightened = tighten_cluster_power_factor_reference(
                target=power_factor_policy.target,
                region_count=len(names),
                tracking=tracking_limits,
                current_mvar=state.margin_mvar,
                realized_p_mw=tuple(p.p_mw for p in points),
                realized_q_mvar=tuple(p.q_mvar for p in points),
                violated=tuple(p.status == "violated" for p in points),
            )
            if state.margin_mvar == tightened:
                return finish(
                    "stagnated", "已使用完整跟踪误差余量，联合功率因数仍不合格"
                )
            state.margin_mvar = tightened
            if used_candidates:
                assert isinstance(pool, NetworkCandidatePool)
                selected = pool.select(record, state.margin_mvar)
                history[-1] = history[-1].model_copy(
                    update={"candidate_selection": selected}
                )
                if not selected.candidates:
                    return finish(
                        "budget_exhausted"
                        if not selected.search_complete
                        else "stagnated",
                        "候选集中未找到满足新功率因数余量的未尝试组合，不代表物理问题无解",
                    )
                state.calibration = MappingProxyType[
                    str, CoordinationNetworkCalibration
                ]({c.region: c.calibration for c in selected.candidates})
            # The feasible reference domain changed. A calibration seen under
            # the old PF domain is not a cycle in this new domain.
            seen = [state.calibration] if state.calibration is not None else []
            continue
        if record.outcome != "regional_rejected":
            return finish(record.outcome, record.reason)
        if policy is None or diagnose is None:
            return finish("disabled", "未配置跨层恢复策略或区域诊断端口")
        if number > maximum:
            return finish("budget_exhausted", "跨层恢复轮次已耗尽，未取得可采用计划")
        try:
            feedback = cast(
                tuple[RegionalCalibrationFeedback, ...], diagnose(trial.payload, number)
            )
            history[-1] = history[-1].model_copy(update={"feedback": feedback})
            if (
                len(feedback) != len(names)
                or {f.evidence.region for f in feedback} != set(names)
                or any(f.reference_round != number for f in feedback)
            ):
                raise ValueError("feedback region identities or reference round differ")
            if any(f.calibration is None for f in feedback):
                return finish(
                    "feedback_unavailable", "区域诊断未取得完整有效的物理校准"
                )
            if any(f.evidence.time_minutes != time_minutes for f in feedback):
                raise ValueError("feedback time axis differs from the current window")
            refreshed = {
                f.evidence.region: f.calibration
                for f in feedback
                if f.calibration is not None
            }
            if isinstance(pool, NetworkCandidatePool):
                pool.add(feedback)
                selected = pool.select(record, state.margin_mvar)
                history[-1] = history[-1].model_copy(
                    update={"candidate_selection": selected}
                )
                if selected.candidates:
                    state.calibration = MappingProxyType[
                        str, CoordinationNetworkCalibration
                    ]({c.region: c.calibration for c in selected.candidates})
                    continue
                if not selected.search_complete:
                    return finish(
                        "budget_exhausted", "区域候选组合搜索预算耗尽，未找到联合候选"
                    )
            if any(
                all(
                    equivalent_coordination_calibration(old[n], refreshed[n], policy)
                    for n in names
                )
                for old in seen
            ):
                return finish(
                    "stagnated",
                    "有效校准未改变或发生循环；未取得新的联合候选，不能推断物理问题无解"
                    if pool is not None
                    else "有效校准未改变或发生循环，停止重复生成参考",
                )
            state.calibration = MappingProxyType[str, CoordinationNetworkCalibration](
                refreshed
            )
            seen.append(state.calibration)
        except (ValueError, RuntimeError, FloatingPointError) as exc:
            return finish("error", f"区域反馈无效：{exc}")
    raise AssertionError("bounded recovery loop must terminate explicitly")
