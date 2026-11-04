"""Background YOLO/ByteTrack analysis and auditable line-crossing counts."""
import argparse
from collections import Counter, defaultdict
import csv
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import time

CLASSES = {2: 'car', 3: 'motorcycle', 5: 'bus', 7: 'truck'}


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, allow_nan=False), encoding='utf-8')
    replace_file(temporary, path)


def replace_file(source, target):
    # Windows readers/OneDrive may briefly hold the destination without delete sharing.
    for attempt in range(20):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(.025)


class VehicleCounter:
    """One crossing per track, with a dead band to suppress center jitter."""
    def __init__(self, axis='horizontal', position=.5, band=.015):
        self.axis, self.position, self.band = axis, position, band
        self.votes = defaultdict(Counter)
        self.sides = {}
        self.hits = Counter()
        self.counted = set()
        self.events = []

    def observe(self, track_id, class_id, confidence, x, y, timestamp):
        if class_id not in CLASSES:
            return
        self.hits[track_id] += 1
        self.votes[track_id][CLASSES[class_id]] += confidence
        distance = (y if self.axis == 'horizontal' else x) - self.position
        side = 1 if distance > self.band else -1 if distance < -self.band else 0
        if not side:
            return
        previous = self.sides.get(track_id)
        self.sides[track_id] = side
        if previous and previous != side and track_id not in self.counted and self.hits[track_id] >= 3:
            direction = ('down' if side > 0 else 'up') if self.axis == 'horizontal' else ('right' if side > 0 else 'left')
            event = {'track_id': track_id, 'vehicle_type': self.votes[track_id].most_common(1)[0][0],
                     'direction': direction, 'video_seconds': round(timestamp, 3)}
            self.events.append(event)
            self.counted.add(track_id)
            return event

    def summary(self):
        directions = ('up', 'down') if self.axis == 'horizontal' else ('left', 'right')
        crossings = {name: {d: 0 for d in directions} for name in CLASSES.values()}
        for event in self.events:
            crossings[event['vehicle_type']][event['direction']] += 1
        unique = Counter(v.most_common(1)[0][0] for key, v in self.votes.items() if self.hits[key] >= 3)
        return {'crossings': crossings, 'crossing_total': len(self.events),
                'unique_tracks': {name: unique[name] for name in CLASSES.values()},
                'unique_total': sum(unique.values())}


def analyze(source, model_path, directory, config):
    import cv2
    import torch
    from ultralytics import YOLO

    torch.set_num_threads(min(4, os.cpu_count() or 1))
    state_path = directory / 'state.json'
    state = json.loads(state_path.read_text('utf-8'))
    started = time.time()
    cap = cv2.VideoCapture(str(source))
    writer = None
    try:
        if not cap.isOpened():
            raise RuntimeError('Could not open the selected video.')
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        if not math.isfinite(fps) or fps <= 0:
            raise RuntimeError('Source has no valid frame rate; timing cannot be measured reliably.')
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        limit = int(config['max_seconds'] * fps) if config.get('max_seconds') else total
        target = min(total, limit) if total > 0 else limit
        counter = VehicleCounter(config['axis'], config['position'])
        model = YOLO(str(model_path))
        raw_video = directory / 'annotated_raw.mp4'
        writer = cv2.VideoWriter(str(raw_video), cv2.VideoWriter_fourcc(*'mp4v'), fps, (width, height))
        if not writer.isOpened():
            raise RuntimeError('Could not create annotated video.')
        state.update(status='running', message='Detecting and tracking vehicles…', frames=0,
                     total_frames=target, fps=fps, width=width, height=height)
        write_json(state_path, state)
        frame_number = 0
        with (directory / 'detections.csv').open('w', newline='', encoding='utf-8') as handle:
            fields = ['frame', 'video_seconds', 'track_id', 'vehicle_type', 'confidence', 'x1', 'y1', 'x2', 'y2']
            detections = csv.DictWriter(handle, fieldnames=fields)
            detections.writeheader()
            while not target or frame_number < target:
                if (directory / 'cancel.flag').exists():
                    state.update(status='cancelled', message='Analysis cancelled. Start a new analysis to generate complete exports.')
                    write_json(state_path, state)
                    return
                ok, frame = cap.read()
                if not ok:
                    break
                result = model.track(frame, persist=True, tracker='bytetrack.yaml', conf=config['confidence'],
                                     classes=list(CLASSES), imgsz=640, verbose=False, device='cpu')[0]
                timestamp = frame_number / fps
                if result.boxes is not None and result.boxes.id is not None:
                    for box, track_id, class_id, conf in zip(result.boxes.xyxy.cpu().tolist(), result.boxes.id.int().cpu().tolist(),
                                                            result.boxes.cls.int().cpu().tolist(), result.boxes.conf.cpu().tolist()):
                        x1, y1, x2, y2 = box
                        counter.observe(track_id, class_id, conf, (x1+x2)/2/width, (y1+y2)/2/height, timestamp)
                        detections.writerow(dict(zip(fields, [frame_number, round(timestamp, 3), track_id, CLASSES[class_id],
                                                              round(conf, 4), *[round(v, 2) for v in box]])))
                annotated = result.plot()
                position = int(config['position'] * (height if config['axis'] == 'horizontal' else width))
                endpoints = ((0, position), (width-1, position)) if config['axis'] == 'horizontal' else ((position, 0), (position, height-1))
                cv2.line(annotated, *endpoints, (60, 230, 230), 2)
                summary = counter.summary()
                cv2.rectangle(annotated, (0, 0), (min(width, 510), 52), (20, 45, 38), -1)
                cv2.putText(annotated, f"Line crossings: {summary['crossing_total']} | Tracks: {summary['unique_total']}",
                            (10, 22), cv2.FONT_HERSHEY_SIMPLEX, .5, (255, 255, 255), 1)
                cv2.putText(annotated, 'Car / motorcycle / bus / truck | ByteTrack IDs',
                            (10, 43), cv2.FONT_HERSHEY_SIMPLEX, .4, (220, 240, 220), 1)
                writer.write(annotated)
                frame_number += 1
                if frame_number == 1 or frame_number % 15 == 0:
                    try:
                        cv2.imwrite(str(directory / 'latest.tmp.jpg'), annotated)
                        replace_file(directory / 'latest.tmp.jpg', directory / 'latest.jpg')
                    except PermissionError:
                        pass  # Preview readers must not interrupt an otherwise valid analysis.
                    state.update(frames=frame_number, **summary, elapsed=round(time.time()-started, 1))
                    write_json(state_path, state)
        writer.release()
        writer = None
        if not frame_number:
            raise RuntimeError('Source contained no decodable video frames.')
        if target and frame_number < target - max(3, int(fps)):
            raise RuntimeError('Video ended before the expected frame count. Partial detections are retained but results are incomplete.')
        summary = counter.summary()
        summary.update(source=source.name, model=model_path.name, tracker='ByteTrack', config=config,
                       frames=frame_number, video_seconds=round(frame_number/fps, 3), source_fps=fps,
                       counts_definition='Each track counted once at its first line crossing; types use confidence-weighted votes up to crossing.',
                       limitations='Track IDs can fragment during occlusion, camera movement, or reconnects. Unique tracks are not ground-truth distinct vehicles. Screen directions are not compass or road directions.')
        write_json(directory / 'summary.json', summary)
        with (directory / 'crossings.csv').open('w', newline='', encoding='utf-8') as handle:
            events = csv.DictWriter(handle, fieldnames=['track_id', 'vehicle_type', 'direction', 'video_seconds'])
            events.writeheader()
            events.writerows(counter.events)
        with (directory / 'counts.csv').open('w', newline='', encoding='utf-8') as handle:
            rows = csv.writer(handle)
            rows.writerow(['vehicle_type', 'direction', 'line_crossings', 'unique_tracks_in_clip'])
            for vehicle_type, directions in summary['crossings'].items():
                for direction, count in directions.items():
                    rows.writerow([vehicle_type, direction, count, summary['unique_tracks'][vehicle_type]])
        state.update(status='running', frames=frame_number, **counter.summary(), message='Encoding annotated video for browser playback…')
        write_json(state_path, state)
        executable = os.environ.get('FL511_FFMPEG') or shutil.which('ffmpeg')
        if not executable:
            raise RuntimeError('FFmpeg is required to encode annotated video.')
        result = subprocess.run([executable, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(raw_video),
                                 '-an', '-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast', '-crf', '23',
                                 '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(directory / 'annotated.mp4')],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError('Annotated video encoding failed; CSV data remains in the analysis folder.')
        raw_video.unlink()
        cancelled = (directory / 'cancel.flag').exists()
        state.update(status='cancelled' if cancelled else 'completed',
                     message='Cancelled.' if cancelled else 'Vehicle analysis complete.', elapsed=round(time.time()-started, 1))
        write_json(state_path, state)
    except Exception as error:
        state.update(status='failed', message=f'{type(error).__name__}: {error}')
        write_json(state_path, state)
    finally:
        cap.release()
        if writer:
            writer.release()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--directory', required=True)
    args = parser.parse_args()
    directory = Path(args.directory)
    config = json.loads((directory / 'state.json').read_text('utf-8'))['config']
    analyze(Path(args.source), Path(args.model), directory, config)
