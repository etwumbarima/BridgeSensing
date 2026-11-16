"""Convert INTELLIVIZ detections to compressed OTAnalytics track data."""

import bz2
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from collections import Counter

from vehicle_analysis import CLASSES


def suggested_start(job):
    if job.get('source', '').startswith('recording:'):
        match = re.search(r'_(\d{8}T\d{6}Z)_', job['source_name'])
        if match:
            return datetime.strptime(match[1], '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).isoformat()
    return ''


def export_ottrk(path, job, start_text):
    try:
        start = datetime.fromisoformat(start_text.strip())
        if start.utcoffset() is None:
            raise ValueError
        epoch = start.timestamp()
    except (ValueError, OverflowError, OSError) as error:
        raise ValueError('Enter the video start date and time with a timezone, such as 2026-11-09T13:54:25Z.') from error

    fps, frames = float(job['fps']), int(job['frames'])
    width, height = int(job['width']), int(job['height'])
    if not math.isfinite(fps) or min(fps, frames, width, height) <= 0:
        raise ValueError('The analysis has invalid video metadata.')

    source = Path(job['source_name'])
    metadata = {
        'otdet_version': '1.2',
        'ottrk_version': '1.1',
        'video': {'filename': source.stem, 'filetype': source.suffix, 'width': width,
                  'height': height, 'actual_fps': fps, 'recorded_fps': fps,
                  'number_of_frames': frames, 'recorded_start_date': epoch,
                  'expected_duration': frames / fps},
        'detection': {'model': {'name': 'YOLO26', 'classes': CLASSES}, 'normalized_bbox': False},
        'tracking': {'tracking_run_id': job['id'], 'frame_group': 0,
                     'first_tracked_video_start': epoch, 'last_tracked_video_end': epoch + (frames - 1) / fps,
                     'tracker': {'name': 'ByteTrack'}},
    }
    with path.open(newline='', encoding='utf-8') as csv_file:
        reader = csv.DictReader(csv_file)
        remaining = Counter(row['track_id'] for row in reader)
        csv_file.seek(0)
        compressor = bz2.BZ2Compressor()
        parts = [compressor.compress(b'{"metadata":' + json.dumps(metadata).encode()
                                     + b',"data":{"detections":[')]
        seen = set()
        for index, row in enumerate(csv.DictReader(csv_file)):
            track = row['track_id']
            frame = int(row['frame'])
            x1, y1, x2, y2 = (float(row[key]) for key in ('x1', 'y1', 'x2', 'y2'))
            remaining[track] -= 1
            detection = {'class': row['vehicle_type'], 'confidence': float(row['confidence']),
                         'x': (x1 + x2) / 2, 'y': (y1 + y2) / 2, 'w': x2 - x1, 'h': y2 - y1,
                         'frame': frame + 1, 'occurrence': epoch + frame / fps,
                         'track-id': int(track), 'input_file_path': source.name,
                         'interpolated-detection': False, 'first': track not in seen,
                         'finished': remaining[track] == 0}
            seen.add(track)
            encoded = json.dumps(detection, allow_nan=False, separators=(',', ':')).encode()
            block = compressor.compress((b',' if index else b'') + encoded)
            if block:
                parts.append(block)
        parts.append(compressor.compress(b']}}'))
        parts.append(compressor.flush())
    return b''.join(parts)
