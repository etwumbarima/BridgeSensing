"""Streamlit-independent services. No worker thread calls Streamlit APIs."""
import atexit
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time

from analysis_service import AnalysisService
from live_detection import LiveDetectionService

ROOT = Path(__file__).resolve().parent
STORAGE = Path(os.environ.get('FL511_DATA_DIR', str(ROOT / '.runtime'))).resolve()


def radar_url(camera=None, station=None):
    if station:
        if station not in {'KTLH','KEVX','KJAX','KTBW','KMLB','KAMX','KBYX'}:
            raise ValueError('Unknown radar station.')
        return 'https://radar.weather.gov/station/' + station
    located = camera and isinstance(camera.get('longitude'),(int,float)) and isinstance(camera.get('latitude'),(int,float))
    settings = {'agenda':{'id':'national','center':[camera['longitude'],camera['latitude']] if located else [-83.5,28.2],
                          'zoom':9 if located else 6,'layer':'bref_qcd','transparent':True,'alertsOverlay':True}}
    from urllib.parse import quote
    return 'https://radar.weather.gov/?settings=v1_' + quote(base64.b64encode(json.dumps(settings).encode()).decode())


class CameraPreview:
    """Low-memory server-side motion preview; no browser CORS or localhost proxy."""
    def __init__(self, recorder):
        self.recorder=recorder
        self.lock=threading.RLock()
        self.stop_event=threading.Event()
        self.frame=None
        self.camera_id=None
        self.message='Preview is stopped.'
        self.process=None
        self.thread=None
        self.heartbeat=time.time()

    def start(self,camera):
        self.stop()
        with self.lock:
            self.stop_event=threading.Event()
            self.frame=None
            self.camera_id=camera['id']
            self.heartbeat=time.time()
            self.message='Connecting to Florida 511…'
            self.thread=threading.Thread(target=self.run,args=(camera,self.stop_event),daemon=True)
            self.thread.start()

    def run(self,camera,stop):
        process=None
        try:
            url=self.recorder.resolve_stream(camera)
            if stop.is_set():
                return
            process=subprocess.Popen([self.recorder.ffmpeg(),'-hide_banner','-loglevel','error','-nostdin',
                '-rw_timeout','15000000','-user_agent',self.recorder.HEADERS['User-Agent'],
                '-headers','Referer: https://fl511.com/\r\nOrigin: https://fl511.com\r\n','-re','-i',url,
                '-an','-vf','fps=2,scale=768:-2','-vcodec','mjpeg','-f','image2pipe','-flush_packets','1','pipe:1'],
                stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            with self.lock:
                self.process=process
            buffer=b''
            while not stop.is_set() and time.time()-self.heartbeat<120:
                chunk=process.stdout.read1(65536)
                if not chunk:
                    break
                buffer+=chunk
                while True:
                    start=buffer.find(b'\xff\xd8')
                    end=buffer.find(b'\xff\xd9',max(0,start))
                    if start<0 or end<0:
                        break
                    with self.lock:
                        self.frame=buffer[start:end+2]
                        self.message='Live motion preview · up to 2 FPS'
                    buffer=buffer[end+2:]
                if len(buffer)>8*1024*1024:
                    raise ValueError('Camera returned an invalid video frame.')
            with self.lock:
                self.message='Preview stopped. Start it again to reconnect.'
        except Exception:
            with self.lock:
                self.message='Could not connect to this live feed. Retry or select another camera.'
        finally:
            if process:
                if process.poll() is None:
                    process.terminate()
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait()
                process.stdout.close()

    def snapshot(self):
        with self.lock:
            self.heartbeat=time.time()
            return self.frame,self.message

    def stop(self):
        self.stop_event.set()
        with self.lock:
            process=self.process
        if process and process.poll() is None:
            process.terminate()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=30)
        with self.lock:
            self.process=None
            self.camera_id=None


class Workspace:
    def __init__(self,key):
        if not re.fullmatch(r'[a-f0-9]{32}',key):
            raise ValueError('Invalid workspace key.')
        self.key=key
        self.directory=STORAGE/key
        self.directory.mkdir(parents=True,exist_ok=True)
        # Recorder globals are isolated per workspace; no visitor can select another visitor's files.
        spec=importlib.util.spec_from_file_location('recorder_'+key,ROOT/'recorder.py')
        self.recorder=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.recorder)
        self.recorder.DATA=self.directory/'data'
        self.recorder.OUTPUT=self.directory/'recordings'
        self.recorder.init()
        seed=ROOT/'assets'/'cameras.json'
        if seed.exists() and not (self.recorder.DATA/'cameras.json').exists():
            shutil.copyfile(seed,self.recorder.DATA/'cameras.json')
        self.stop=self.recorder.STOP
        self.analysis=AnalysisService(ROOT,self.recorder.OUTPUT,self.stop,self.recorder.ffmpeg,self.directory)
        self.analysis.start(self.recorder.get_jobs)
        self.live=LiveDetectionService(ROOT,self.stop,self.directory)
        self.preview=CameraPreview(self.recorder)
        self.live_id=None
        self.last_live_id=None
        self.sync_lock=threading.Lock()
        self.scheduler_thread=threading.Thread(target=self.recorder.scheduler,daemon=True)
        self.scheduler_thread.start()

    def catalog(self):
        return self.recorder.load_catalog()

    def camera(self,camera_id):
        camera=next((c for c in self.catalog()['cameras'] if c['id']==str(camera_id)),None)
        if not camera:
            raise ValueError('Camera is not in the current Florida 511 directory.')
        return camera

    def jobs(self):
        jobs=self.recorder.get_jobs()
        for job in jobs:
            job['camera']=json.loads(job['camera'])
            stem=Path(job['file']).stem if job['file'] else None
            job['files']=list(self.recorder.OUTPUT.glob(stem+'*.mkv')) if stem else []
        return jobs

    def cancel(self,job_id):
        with self.recorder.LOCK:
            if self.recorder.status(job_id) not in ('queued','recording'):
                raise ValueError('This capture has already ended.')
            self.recorder.update(job_id,status='cancelled',message='Cancelled; captured footage retained.')
            process=self.recorder.PROCESSES.get(job_id)
        if process:
            self.recorder.stop_process(process)

    def upload(self,name,data):
        if len(data)>200*1024*1024:
            raise ValueError('Upload limit is 200 MB.')
        suffix=Path(name).suffix.lower()
        if suffix not in ('.mp4','.mkv','.mov','.avi'):
            raise ValueError('Use MP4, MKV, MOV or AVI.')
        clean=re.sub(r'[^A-Za-z0-9._-]','_',Path(name).stem)[:70]
        file=self.analysis.uploads/(clean+'_'+hashlib.sha256(data).hexdigest()[:12]+suffix)
        if not file.exists(): file.write_bytes(data)
        return 'upload:'+file.name

    def stop_live(self):
        if self.live_id:
            self.live.end(self.live_id)
            self.last_live_id=self.live_id
            self.live_id=None

    def close(self):
        self.stop.set()
        self.preview.stop()
        self.stop_live()
        for process in list(self.recorder.PROCESSES.values()):
            self.recorder.stop_process(process)
        self.scheduler_thread.join(timeout=5)
        if getattr(self.analysis,'thread',None): self.analysis.thread.join(timeout=20)


class Manager:
    def __init__(self):
        self.lock=threading.RLock()
        self.workspaces={}
        atexit.register(self.close)

    def workspace(self,key):
        with self.lock:
            if key not in self.workspaces:
                if len(self.workspaces)>=16:
                    raise ValueError('This app instance has reached its workspace limit. Reuse an existing workspace or reboot the app.')
                self.workspaces[key]=Workspace(key)
            return self.workspaces[key]

    def inference_busy(self):
        for workspace in self.workspaces.values():
            active=workspace.live.active
            if active and active[1].poll() is None:
                return True
            if any(s['status'] in ('queued','running') for s in workspace.analysis.states()):
                return True
        return False

    def start_live(self,workspace,camera_id,config):
        with self.lock:
            workspace.stop_live()
            if self.inference_busy():
                raise ValueError('Inference is busy. Stop the current live session or wait for video analysis to finish.')
            camera=workspace.camera(camera_id)
            if not camera['available'] or not workspace.recorder.ffmpeg():
                raise ValueError('This camera has no available video, or FFmpeg is missing.')
            workspace.preview.stop()
            result=workspace.live.start(camera,config)
            workspace.live_id=result['id']
            return result['id']

    def enqueue_analysis(self,workspace,config):
        with self.lock:
            if self.inference_busy():
                raise ValueError('Inference is busy. Stop live counting or wait for the active analysis to finish.')
            return workspace.analysis.enqueue(config,workspace.jobs())

    def schedule(self,workspace,camera_ids,start,end):
        with self.lock:
            now=time.time()
            ids=list(dict.fromkeys(camera_ids))
            if not 1<=len(ids)<=4 or not(now-60<=start<end and end>now and end-start<=86400):
                raise ValueError('Select 1–4 cameras and a future window of at most 24 hours.')
            cameras=[workspace.camera(camera_id) for camera_id in ids]
            if not workspace.recorder.ffmpeg() or any(not c['available'] for c in cameras):
                raise ValueError('FFmpeg and video-enabled cameras are required.')
            intervals=[(j['start'],j['end']) for w in self.workspaces.values() for j in w.jobs()
                       if j['status'] in ('queued','recording') and j['start']<end and j['end']>start]
            intervals += [(start,end)]*len(ids)
            n=0
            for _,delta in sorted([(s,1) for s,e in intervals]+[(e,-1) for s,e in intervals]):
                n+=delta
                if n>4:
                    raise ValueError('This window exceeds the server limit of four overlapping captures.')
            with workspace.recorder.connect() as db:
                for camera in cameras:
                    db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',(secrets.token_hex(6),json.dumps(camera),start,end,'queued','Waiting for scheduled start',None))
            return len(ids)

    def close(self):
        for workspace in list(self.workspaces.values()):
            workspace.close()
