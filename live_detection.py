"""Live YOLO worker: keep the newest decoded frame rather than queueing latency."""
import argparse
import csv
import json
import os
from pathlib import Path
import subprocess
import threading
import time

from vehicle_analysis import CLASSES, VehicleCounter, write_json, replace_file


def run(directory):
    import cv2
    import numpy as np
    import torch
    from ultralytics import YOLO
    import recorder as server

    torch.set_num_threads(min(4, os.cpu_count() or 1))
    state_path = directory / 'state.json'
    state = json.loads(state_path.read_text('utf-8'))
    camera = state.pop('camera')
    config = state['config']
    counter = VehicleCounter(config['axis'], config['position'])
    model = None
    process = None
    stop_reader = threading.Event()
    frame_lock = threading.Lock()
    newest = [None, 0, 0.0]
    started = time.time()
    processed, received_total = 0, 0
    first_frame_at = None
    width, height = 640, 480

    def stopped():
        return (directory / 'stop.flag').exists() or time.time() - started > 3600

    try:
        state.update(status='loading', message='Loading YOLO and ByteTrack…')
        write_json(state_path, state)
        model = YOLO(str(server.ROOT / 'models' / 'yolo26n.pt'))
        with (directory / 'crossings.csv').open('w', newline='', encoding='utf-8') as handle:
            events = csv.DictWriter(handle, fieldnames=['track_id', 'vehicle_type', 'direction', 'video_seconds'])
            events.writeheader()
            for attempt in range(4):
                if stopped():
                    break
                if attempt:
                    # A new tracker after a gap avoids reusing IDs for unrelated objects.
                    state.update(status='reconnecting', message='Feed interrupted. Start a new counting session after reconnect; totals retained for review.')
                    break
                url = server.resolve_stream(camera)
                process = subprocess.Popen([server.ffmpeg(), '-hide_banner', '-loglevel', 'error', '-nostdin',
                    '-rw_timeout', '15000000', '-user_agent', server.HEADERS['User-Agent'],
                    '-headers', 'Referer: https://fl511.com/\r\nOrigin: https://fl511.com\r\n',
                    '-re', '-i', url, '-an', '-vf', 'scale=640:480:force_original_aspect_ratio=decrease,pad=640:480:(ow-iw)/2:(oh-ih)/2',
                    '-pix_fmt', 'bgr24', '-flush_packets', '1', '-f', 'rawvideo', 'pipe:1'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                reader_done = threading.Event()

                def read_frames():
                    size = width * height * 3
                    try:
                        while not stop_reader.is_set():
                            raw = bytearray()
                            while len(raw) < size:
                                chunk = process.stdout.read(size-len(raw))
                                if not chunk:
                                    return
                                raw.extend(chunk)
                            frame = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3).copy()
                            with frame_lock:
                                newest[0] = frame
                                newest[1] += 1
                                newest[2] = time.time()
                    finally:
                        reader_done.set()

                reader = threading.Thread(target=read_frames, daemon=True)
                reader.start()
                last_number = 0
                while not stopped():
                    with frame_lock:
                        frame, number, arrival = newest
                    if number == last_number:
                        if reader_done.is_set():
                            break
                        if time.time() - (arrival or started) > 40:
                            raise RuntimeError('Live feed timed out. Try reconnecting or another camera.')
                        time.sleep(.02)
                        continue
                    last_number = number
                    if first_frame_at is None:
                        first_frame_at = time.time()
                    result = model.track(frame, persist=True, tracker='bytetrack.yaml', conf=config['confidence'],
                                         classes=list(CLASSES), imgsz=640, verbose=False, device='cpu')[0]
                    current = {name: 0 for name in CLASSES.values()}
                    if result.boxes is not None:
                        for cls in result.boxes.cls.int().cpu().tolist():
                            current[CLASSES[cls]] += 1
                        if result.boxes.id is not None:
                            for box, tid, cls, confidence in zip(result.boxes.xyxy.cpu().tolist(), result.boxes.id.int().cpu().tolist(),
                                                               result.boxes.cls.int().cpu().tolist(), result.boxes.conf.cpu().tolist()):
                                x1,y1,x2,y2 = box
                                event = counter.observe(tid, cls, confidence, (x1+x2)/2/width, (y1+y2)/2/height, arrival-started)
                                if event:
                                    events.writerow(event)
                                    handle.flush()
                    image = result.plot()
                    point = int(config['position'] * (height if config['axis']=='horizontal' else width))
                    ends = ((0,point),(width-1,point)) if config['axis']=='horizontal' else ((point,0),(point,height-1))
                    cv2.line(image, *ends, (50,240,240), 2)
                    totals = counter.summary()
                    cv2.rectangle(image,(0,0),(width,28),(20,45,38),-1)
                    cv2.putText(image,f"LIVE | Crossings: {totals['crossing_total']} | In frame: {sum(current.values())}",
                                (8,20),cv2.FONT_HERSHEY_SIMPLEX,.5,(255,255,255),1)
                    success, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    if success:
                        try:
                            (directory / 'frame.tmp').write_bytes(encoded.tobytes())
                            replace_file(directory / 'frame.tmp', directory / 'frame.jpg')
                        except PermissionError:
                            pass  # Skip a preview update during a transient file lock; counting continues.
                    processed += 1
                    received_total = number
                    elapsed = time.time()-started
                    state.update(status='running', message='Live detection and counting', frames=processed,
                        received_frames=received_total, skipped_frames=max(0,received_total-processed),
                        processing_fps=round(processed/max(time.time()-first_frame_at,1),1), updated=time.time(),
                        frame_age_seconds=round(time.time()-arrival,2), elapsed=round(elapsed,1), in_frame=current, **totals)
                    write_json(state_path,state)
                if stopped():
                    state.update(status='stopped', message='Counting session ended. Totals retained.' if time.time()-started<3600 else 'One-hour session limit reached. Start a new observation period.')
                    break
            else:
                state.update(status='failed',message='Live feed unavailable.')
        if state['status'] in ('running','reconnecting','loading'):
            state.update(status='disconnected',message='Feed disconnected. Start a new session to resume counting; totals retained.')
    except Exception as error:
        state.update(status='failed',message=f'{type(error).__name__}: {error}')
    finally:
        stop_reader.set()
        if process:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()
        state.update(updated=time.time(), elapsed=round(time.time()-started,1))
        write_json(state_path,state)


class LiveDetectionService:
    def __init__(self, root, stop, data_directory=None):
        self.root, self.stop = root, stop
        self.folder = (data_directory or root) / 'live-sessions'
        self.folder.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.active = None
        self.heartbeat = 0
        threading.Thread(target=self.watch, daemon=True).start()

    def path(self, session):
        import re
        if not re.fullmatch(r'[a-f0-9]{16}',session):
            raise ValueError('Invalid live session.')
        path = self.folder / session
        if not (path/'state.json').exists():
            raise ValueError('Live session not found.')
        return path

    def start(self, camera, config):
        import importlib.util
        import math
        import secrets
        import sys
        if not (self.root/'models'/'yolo26n.pt').is_file() or any(importlib.util.find_spec(m) is None for m in ('torch','cv2','ultralytics','lap')):
            raise ValueError('YOLO model or tracking dependencies are missing.')
        config = {'axis':config.get('axis','horizontal'), 'position':float(config.get('position',.5)), 'confidence':float(config.get('confidence',.3))}
        if config['axis'] not in ('horizontal','vertical') or not math.isfinite(config['position']) or not .05<=config['position']<=.95 or not math.isfinite(config['confidence']) or not .1<=config['confidence']<=.9:
            raise ValueError('Invalid counting line or confidence.')
        with self.lock:
            if self.active and self.active[1].poll() is None:
                raise ValueError('A live counting session is already running. Stop it before starting another.')
            session = secrets.token_hex(8)
            directory = self.folder/session
            directory.mkdir()
            write_json(directory/'state.json', {'id':session,'camera_id':camera['id'],'camera_name':camera['name'],
                'camera':camera,'config':config,'created':time.time(),'status':'loading','message':'Starting live detection…','frames':0})
            process = subprocess.Popen([sys.executable,str(self.root/'live_detection.py'),'--directory',str(directory)],
                cwd=self.root,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.active=(session,process)
            self.heartbeat=time.time()
            return {'id':session}

    def state(self, session):
        path=self.path(session)
        with self.lock:
            if self.active and self.active[0]==session:
                self.heartbeat=time.time()
            value=json.loads((path/'state.json').read_text('utf-8'))
            value.pop('camera',None)
            value['has_frame']=(path/'frame.jpg').exists()
            if self.active and self.active[0]==session and self.active[1].poll() is not None and value['status'] in ('loading','running'):
                value.update(status='failed',message='Live worker stopped unexpectedly. Start a new session.')
            return value

    def end(self, session):
        path=self.path(session)
        (path/'stop.flag').touch()
        with self.lock:
            if self.active and self.active[0]==session:
                process=self.active[1]
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=10)
                state=json.loads((path/'state.json').read_text('utf-8'))
                if state['status'] in ('loading','running'):
                    state.pop('camera',None)
                    state.update(status='stopped',message='Live counting stopped. Totals retained.')
                    write_json(path/'state.json',state)

    def watch(self):
        while not self.stop.wait(5):
            with self.lock:
                active=self.active
                expired=time.time()-self.heartbeat>120
            if active and expired and active[1].poll() is None:
                self.end(active[0])
        if self.active and self.active[1].poll() is None:
            self.end(self.active[0])


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--directory',required=True)
    run(Path(parser.parse_args().directory))
