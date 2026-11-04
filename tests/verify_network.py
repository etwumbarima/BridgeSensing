"""Optional network smoke test; not run by unit-test discovery.

Captures 20 seconds from public camera 1374 and analyzes its first five seconds.
Run explicitly with: python tests/verify_network.py
"""
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import Manager


def main():
    manager=Manager()
    workspace=manager.workspace(secrets.token_hex(16))
    try:
        start=time.time()+3
        manager.schedule(workspace,['1374'],start,start+20)
        deadline=time.time()+100
        while time.time()<deadline:
            job=workspace.jobs()[0]
            if job['status'] not in ('queued','recording'): break
            time.sleep(1)
        if job['status'] not in ('completed','partial') or not job['files']:
            raise RuntimeError('Live capture failed: '+job['message'])
        file=next(file for file in job['files'] if file.stat().st_size>4096)
        probe=subprocess.run([shutil.which('ffprobe'),'-v','error','-count_packets','-show_entries','stream=nb_read_packets:format=duration',
                              '-of','json',str(file)],capture_output=True,text=True,check=True)
        metadata=json.loads(probe.stdout)
        assert int(metadata['streams'][0]['nb_read_packets'])>0
        print('Scheduled live capture verified:',job['status'],flush=True)
        analysis=manager.enqueue_analysis(workspace,{'source':'recording:'+file.name,'max_seconds':5,'axis':'horizontal','position':.5,'confidence':.3})
        deadline=time.time()+180
        while time.time()<deadline:
            result=next(state for state in workspace.analysis.states() if state['id']==analysis['id'])
            if result['status'] not in ('queued','running'): break
            time.sleep(1)
        assert result['status']=='completed',result['message']
        directory=workspace.analysis.job_path(result['id'])
        for name in ('annotated.mp4','counts.csv','crossings.csv','detections.csv','summary.json'):
            assert (directory/name).is_file(),name
        summary=json.loads((directory/'summary.json').read_text('utf-8'))
        assert summary['frames']>0
        report={'recording_status':job['status'],'recording_metadata':metadata,'analysis_status':result['status'],
                'analysis_frames':summary['frames'],'crossing_total':summary['crossing_total']}
        (workspace.directory/'verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print('Offline YOLO analysis and all five exports verified:',summary['frames'],'frames',flush=True)
    finally:
        manager.close()


if __name__=='__main__': main()
