"""Screen recorded tracks for candidate braking within a calibrated road patch."""

import csv
import math
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np


def road_projection(points, width, length):
    corners = np.asarray(points, dtype=np.float32)
    if (not all(math.isfinite(size) and size > 0 for size in (width, length))
            or corners.shape != (4, 2) or not np.isfinite(corners).all()
            or np.any(corners < 0) or np.any(corners > 1)
            or not cv2.isContourConvex(corners) or abs(cv2.contourArea(corners)) < .0001):
        raise ValueError('Enter four corners of a road rectangle in order, using image percentages.')
    return corners, cv2.getPerspectiveTransform(
        corners, np.float32([[0, 0], [width, 0], [width, length], [0, length]]))


def find_braking(path, image_width, image_height, fps, calibration, threshold):
    corners, projection = calibration
    tracks = defaultdict(list)
    with path.open(newline='', encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            pixel = ((float(row['x1']) + float(row['x2'])) / 2, float(row['y2']))
            point = (pixel[0] / image_width, pixel[1] / image_height)
            if cv2.pointPolygonTest(corners, point, False) >= 0:
                tracks[int(row['track_id'])].append(
                    (int(row['frame']), *pixel, row['vehicle_type']))

    events = []
    if fps < 5:
        return events, tracks
    for track_id, samples in tracks.items():
        frames = np.asarray([sample[0] for sample in samples])
        points = np.float32([[x / image_width, y / image_height] for _, x, y, _ in samples])
        longitudinal = cv2.perspectiveTransform(points[None], projection)[0, :, 1]
        times = frames / fps
        last = None
        half = max(2, round(fps / 2))
        for index in range(half, len(samples) - half, max(1, round(fps / 10))):
            start, end = index - half, index + half
            if np.max(np.diff(times[start:end + 1])) > max(.21, 1.5 / fps):
                continue
            before = slice(start, index + 1)
            after = slice(index, end + 1)
            first = np.polyfit(times[before] - times[index], longitudinal[before], 1)[0]
            second = np.polyfit(times[after] - times[index], longitudinal[after], 1)[0]
            if abs(first) < 3 or first * second < 0:
                continue
            deceleration = (abs(first) - abs(second)) / ((times[end] - times[start]) / 2)
            if deceleration < threshold:
                continue
            event = {'track_id': track_id, 'vehicle_type': samples[index][3],
                     'seconds': round(float(times[index]), 2),
                     'deceleration_mps2': round(float(deceleration), 2)}
            if last is None or times[index] - last > 1:
                events.append(event)
            elif event['deceleration_mps2'] > events[-1]['deceleration_mps2']:
                events[-1] = event
            last = times[index]
    return sorted(events, key=lambda event: event['seconds']), tracks


def review_clip(video, ffmpeg, seconds, samples):
    """Render only the selected event, with a growing trail for its track."""
    cap = cv2.VideoCapture(str(video))
    try:
        if not cap.isOpened():
            raise ValueError('The analyzed video could not be opened.')
        fps = cap.get(cv2.CAP_PROP_FPS)
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError('The analyzed video has no valid frame rate.')
        first = max(0, round((seconds - 3) * fps))
        end = round((seconds + 3) * fps)
        points = {frame: (round(x), round(y)) for frame, x, y, _ in samples if first <= frame < end}
        cap.set(cv2.CAP_PROP_POS_FRAMES, first)
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / 'trajectory.mp4'
            output = Path(directory) / 'driving-event.mp4'
            writer = None
            trail = []
            try:
                for index in range(first, end):
                    ok, frame = cap.read()
                    if not ok:
                        break
                    if writer is None:
                        height, width = frame.shape[:2]
                        writer = cv2.VideoWriter(str(raw), cv2.VideoWriter_fourcc(*'mp4v'), fps, (width, height))
                        if not writer.isOpened():
                            raise ValueError('Could not create the review clip.')
                    point = points.get(index)
                    if point:
                        trail.append(point)
                    if len(trail) > 1:
                        cv2.polylines(frame, [np.int32(trail)], False, (245, 45, 255), 4, cv2.LINE_AA)
                    if point:
                        cv2.circle(frame, point, 8, (245, 45, 255), -1, cv2.LINE_AA)
                    writer.write(frame)
            finally:
                if writer is not None:
                    writer.release()
            if writer is None:
                raise ValueError('No video frames were available for this clip.')
            try:
                result = subprocess.run(
                    [ffmpeg, '-hide_banner', '-loglevel', 'error', '-i', str(raw), '-an',
                     '-c:v', 'libx264', '-threads', '2', '-pix_fmt', 'yuv420p', '-movflags',
                     '+faststart', str(output)], capture_output=True, timeout=90,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except subprocess.TimeoutExpired as error:
                raise ValueError('Review clip encoding timed out.') from error
            if result.returncode:
                raise ValueError('FFmpeg could not encode the review clip.')
            return output.read_bytes()
    finally:
        cap.release()
