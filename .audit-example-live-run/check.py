from pathlib import Path
import json
from oilfield_energy.service import SimulationRequest, run_simulation
from oilfield_energy.live_journal import LiveJournal, read_live
from oilfield_energy.bootstrap.adapters.cluster_live import ClusterLiveEvent

def main():
    folder = Path(__file__).parent
    journal = LiveJournal(folder)
    def progress(p):
        print(p.stage.value, getattr(p.rolling, 'start_minute', ''), flush=True)
    result = run_simulation(SimulationRequest(scenario_type='cluster_coordination', steps=4, patent2_case='low_energy', admm_max_iterations=1000), progress, live_callback=journal.publish)
    (folder / 'result.json').write_text(result.model_dump_json(), encoding='utf-8')
    events=[]; cursor=0; stream=None
    while True:
        batch=read_live(folder,cursor,stream)
        events.extend(batch['events']);cursor=batch['cursor'];stream=batch['stream']
        if not batch['more']:break
    for event in events: ClusterLiveEvent.model_validate(event)
    cycles=[e for e in events if e['kind']=='cluster_cycle']
    final=result.cluster_execution
    assert [e['step'] for e in cycles]==[s.model_dump(mode='json') for s in final.safety.steps]
    assert [e['window'] for e in events if e['kind']=='cluster_window'][-1]==final.rolling_updates[-1].model_dump(mode='json')
    for stage in final.stages:
        if stage.stage=='minute':
            for r in stage.regions:
                assert [e['pcc'][r.region][0] for e in cycles]==list(r.p_mw)
                assert [e['pcc'][r.region][1] for e in cycles]==list(r.q_mvar)
    print('VERIFIED',len(cycles),'commits; status',final.status,'replayed',result.patent2_evidence.replayed_minutes,flush=True)
if __name__=='__main__': main()
