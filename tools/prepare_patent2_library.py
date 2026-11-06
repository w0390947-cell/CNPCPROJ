"""Compute genuine demo records, or preserve an existing complete result unchanged.

Run without --result to compute both scenarios. Existing records require --replace.
"""
import argparse
from hashlib import sha256
from pathlib import Path

from oilfield_energy.bootstrap.adapters.patent2_library import library_path
from oilfield_energy.service import SimulationRequest, SimulationResult, run_simulation
from oilfield_energy.job_manager import atomic_write_json


def save(result: SimulationResult, *, replace: bool) -> Path:
    evidence=result.patent2_evidence
    if evidence is None or result.cluster_execution is None:
        raise ValueError('result has no patent2 evidence')
    if result.request.patent2_case != evidence.inputs.scenario:
        raise ValueError('scenario identity mismatch')
    if sha256(evidence.inputs.input_snapshot_json.encode()).hexdigest() != evidence.inputs.input_sha256:
        raise ValueError('input digest mismatch')
    path=library_path(evidence.inputs.scenario)
    if path.exists() and not replace:
        raise FileExistsError(f'{path} already exists; use --replace explicitly')
    path.parent.mkdir(parents=True,exist_ok=True)
    atomic_write_json(path,result.model_dump(mode='json'))
    return path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result',type=Path,action='append',help='existing complete result JSON; may be repeated')
    parser.add_argument('--scenario',choices=['normal','low_energy','both'],default='both')
    parser.add_argument('--replace',action='store_true')
    args=parser.parse_args()
    if args.result:
        for path in args.result:
            print(save(SimulationResult.model_validate_json(path.read_bytes()),replace=args.replace),flush=True)
    else:
        for scenario in (('normal','low_energy') if args.scenario=='both' else (args.scenario,)):
            if library_path(scenario).exists() and not args.replace:
                raise FileExistsError(f'{library_path(scenario)} already exists; use --replace explicitly')
            request=SimulationRequest(name=f'专利二 {scenario}',scenario_type='cluster_coordination',
                patent2_case=scenario,steps=4,admm_max_iterations=1000)
            result=run_simulation(request,lambda p:print(p.label,flush=True))
            print(save(result,replace=args.replace),flush=True)


if __name__=='__main__':
    main()
