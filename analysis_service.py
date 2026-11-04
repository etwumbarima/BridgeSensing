"""Persistent, sequential analysis jobs isolated from the recorder process."""
import importlib.util
import json
import math
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time

from vehicle_analysis import write_json


class AnalysisService:
    def __init__(self, root, output, stop, ffmpeg, data_directory=None):
        self.root, self.output, self.stop, self.ffmpeg = root, output, stop, ffmpeg
        self.data_directory = data_directory or root
        self.uploads = self.data_directory / 'uploads'
        self.uploads.mkdir(exist_ok=True)
        self.directory = self.data_directory / 'analyses'
        self.directory.mkdir(exist_ok=True)
        self.model = root / 'models' / 'yolo26n.pt'
        self.lock = threading.RLock()
        self.process = None

    def sources(self, jobs):
        active_stems = [Path(j['file']).stem for j in jobs if j.get('file') and j['status'] in ('queued', 'recording')]
        recordings = [{'id': 'recording:' + p.name, 'name': p.name, 'size': p.stat().st_size}
                      for p in sorted(self.output.glob('*.mkv')) if p.stat().st_size > 4096 and not any(p.stem.startswith(stem) for stem in active_stems)]
        recordings.extend({'id':'upload:'+p.name,'name':p.name,'size':p.stat().st_size} for p in sorted(self.uploads.iterdir()) if p.suffix.lower() in ('.mp4','.mkv','.mov','.avi') and p.is_file())
        return recordings

    def source(self, source_id, jobs):
        if source_id not in {s['id'] for s in self.sources(jobs)}:
            raise ValueError('Select a finished recording or an uploaded video.')
        return (self.uploads if source_id.startswith('upload:') else self.output) / source_id.split(':', 1)[1]

    def states(self):
        result = []
        for path in self.directory.glob('*/state.json'):
            try:
                state = json.loads(path.read_text('utf-8'))
                state['has_preview'] = (path.parent / 'latest.jpg').is_file()
                result.append(state)
            except (OSError, ValueError):
                pass
        return sorted(result, key=lambda s: s['created'], reverse=True)

    def available(self):
        return self.model.is_file() and all(importlib.util.find_spec(m) is not None for m in ('ultralytics', 'cv2', 'torch'))

    def enqueue(self, body, jobs):
        if not self.available() or not self.ffmpeg():
            raise ValueError('Vehicle analysis requires the local YOLO model, ultralytics, OpenCV, Torch, and FFmpeg.')
        source = self.source(body['source'], jobs)
        config = {'axis': body.get('axis', 'horizontal'), 'position': float(body.get('position', .5)),
                  'confidence': float(body.get('confidence', .3)), 'max_seconds': float(body.get('max_seconds', 0))}
        if config['axis'] not in ('horizontal', 'vertical'):
            raise ValueError('Invalid counting line orientation.')
        if not all(math.isfinite(config[k]) for k in ('position', 'confidence', 'max_seconds')):
            raise ValueError('Analysis settings must be finite numbers.')
        if not .05 <= config['position'] <= .95 or not .1 <= config['confidence'] <= .9 or not 0 <= config['max_seconds'] <= 86400:
            raise ValueError('Invalid line position, confidence, or analysis duration.')
        with self.lock:
            if sum(s['status'] in ('queued', 'running') for s in self.states()) >= 8:
                raise ValueError('Up to eight analysis jobs may be queued. Wait or cancel an existing job.')
            job_id = secrets.token_hex(8)
            path = self.directory / job_id
            path.mkdir()
            state = {'id': job_id, 'source': body['source'], 'source_name': source.name, 'created': time.time(),
                     'config': config, 'status': 'queued', 'message': 'Waiting for analysis worker', 'frames': 0}
            write_json(path / 'state.json', state)
        return state

    def job_path(self, job_id):
        if not re.fullmatch(r'[a-f0-9]{16}', job_id):
            raise ValueError('Invalid analysis ID.')
        path = self.directory / job_id
        if not (path / 'state.json').is_file():
            raise ValueError('Analysis not found.')
        return path

    def cancel(self, job_id):
        with self.lock:
            path = self.job_path(job_id)
            state = json.loads((path / 'state.json').read_text('utf-8'))
            if state['status'] not in ('running', 'queued'):
                raise ValueError('Analysis has already ended.')
            (path / 'cancel.flag').touch()
            if state['status'] == 'queued':
                state.update(status='cancelled', message='Cancelled before processing.')
                write_json(path / 'state.json', state)

    def start(self, get_jobs):
        for state in self.states():
            if state['status'] == 'running':
                state.update(status='interrupted', message='Recorder restarted during analysis. Start a new analysis to retry.')
                write_json(self.directory / state['id'] / 'state.json', state)
        self.thread=threading.Thread(target=self.run, args=(get_jobs,), daemon=True)
        self.thread.start()

    def run(self, get_jobs):
        while not self.stop.wait(1):
            with self.lock:
                queued = sorted((s for s in self.states() if s['status'] == 'queued'), key=lambda s: s['created'])
                if not queued:
                    continue
                state = queued[0]
                directory = self.job_path(state['id'])
                try:
                    source = self.source(state['source'], get_jobs())
                    state.update(status='running', message='Loading YOLO model…')
                    write_json(directory / 'state.json', state)
                    self.process = subprocess.Popen([sys.executable, str(self.root / 'vehicle_analysis.py'), '--source', str(source),
                            '--model', str(self.model), '--directory', str(directory)], cwd=self.root,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                except Exception as error:
                    state.update(status='failed', message=str(error))
                    write_json(directory / 'state.json', state)
                    continue
            while self.process.poll() is None:
                if self.stop.wait(.5):
                    self.process.terminate()
                    self.process.wait(timeout=15)
                    break
            with self.lock:
                latest = json.loads((directory / 'state.json').read_text('utf-8'))
                if latest['status'] == 'running':
                    latest.update(status='interrupted' if self.stop.is_set() else 'failed',
                                  message='Analysis worker stopped before completing. Start a new analysis to retry.')
                    write_json(directory / 'state.json', latest)
                self.process = None

    def thumbnail(self, source_id, jobs):
        source = self.source(source_id, jobs)
        if not self.ffmpeg():
            raise ValueError('FFmpeg is missing.')
        result = subprocess.run([self.ffmpeg(), '-hide_banner', '-loglevel', 'error', '-i', str(source),
                                 '-frames:v', '1', '-vf', 'scale=960:-2', '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1'],
                                capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode or not result.stdout:
            raise ValueError('Could not preview this video.')
        return result.stdout
