"""Recorded-video pavement review and annotation."""

import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile

import numpy as np
import streamlit as st
from PIL import Image, ImageDraw

from vehicle_analysis import replace_file


CONDITIONS = ['Dry', 'Wet', 'Possible standing water', 'Unknown']


def parse_area(text):
    try:
        points = np.asarray([[float(value.strip()) for value in pair.split(',')]
                             for pair in text.split(';')])
        if (points.ndim != 2 or points.shape[1] != 2 or not 3 <= len(points) <= 12
                or not np.isfinite(points).all() or np.any(points < 0) or np.any(points > 100)):
            raise ValueError
        import cv2
        polygon = points.astype(np.float32)
        if not cv2.isContourConvex(polygon) or abs(cv2.contourArea(polygon)) < 1:
            raise ValueError
    except ValueError as error:
        raise ValueError('Enter 3–12 corners in boundary order to outline a convex road area. Use x,y percentages from 0 to 100.') from error
    return points.tolist()


@st.cache_data(ttl=300, max_entries=6, show_spinner=False)
def clip_media(path, revision, ffmpeg, start, duration):
    command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-ss', str(start), '-i', path]
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    still = subprocess.run(command + ['-frames:v', '1', '-vf', 'scale=960:-2',
                                     '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1'],
                           capture_output=True, timeout=30, creationflags=flags)
    if still.returncode or not still.stdout:
        raise ValueError('No frame was available at that start time. Choose an earlier time.')
    clip = subprocess.run(command + ['-t', str(duration), '-an', '-vf', 'scale=960:-2',
                                     '-c:v', 'libx264', '-preset', 'veryfast', '-threads', '2',
                                     '-pix_fmt', 'yuv420p', '-movflags', 'frag_keyframe+empty_moov',
                                     '-f', 'mp4', 'pipe:1'],
                          capture_output=True, timeout=60, creationflags=flags)
    if clip.returncode or len(clip.stdout) < 1000:
        raise ValueError('Could not encode this review clip.')
    return still.stdout, clip.stdout


@st.cache_data(ttl=300, max_entries=6, show_spinner=False)
def pavement_frames(clip, ffmpeg):
    result = subprocess.run(
        [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-threads', '1',
         '-i', 'pipe:0', '-vf', 'fps=2,scale=320:180', '-an', '-pix_fmt', 'gray',
         '-f', 'rawvideo', '-threads', '1', 'pipe:1'],
        input=clip, capture_output=True, timeout=30,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('Could not sample the video.')
    return np.frombuffer(result.stdout, dtype=np.uint8).reshape(-1, 180, 320)


def suggest_condition(frames, points):
    """Screen for persistent reflection patterns inside the reviewed pavement."""
    import cv2

    if len(frames) < 6:
        return 'Unknown', 'Too few video frames for a consistent reading.'
    height, width = frames.shape[1:]
    mask = np.zeros((height, width), dtype=np.uint8)
    corners = np.int32(np.rint(np.asarray(points) * [width - 1, height - 1] / 100))
    cv2.fillConvexPoly(mask, corners, 1)
    mask = cv2.erode(mask, np.ones((5, 5), dtype=np.uint8)).astype(bool)
    if mask.sum() < 500:
        return 'Unknown', 'The outlined area is too small to assess.'

    median = np.median(frames, axis=0).astype(np.uint8)
    stable = (np.mean(np.abs(frames.astype(np.int16) - median) <= 24, axis=0) >= .8) & mask
    if stable.sum() < .65 * mask.sum() or np.median(median[stable]) < 48:
        return 'Unknown', 'Darkness or movement obscures too much of the outlined area.'
    if np.mean(median[stable] >= 248) > .10:
        return 'Unknown', 'Strong glare obscures the pavement.'

    background = cv2.GaussianBlur(median, (0, 0), 5)
    highlights = stable & (median >= 125) & (median.astype(np.int16) - background >= 15)
    broad = cv2.morphologyEx(highlights.astype(np.uint8), cv2.MORPH_OPEN,
                             cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    _, components, stats, _ = cv2.connectedComponentsWithStats(broad)
    patches = [index for index, (_, _, w, h, area) in enumerate(stats[1:], 1)
               if area >= 24 and max(w, h) <= 4 * min(w, h)]
    coverage = np.count_nonzero(np.isin(components, patches)) / stable.sum()
    if coverage >= .018 and any(stats[index, cv2.CC_STAT_AREA] >= .012 * stable.sum() for index in patches):
        return 'Possible standing water', 'A broad, persistent bright patch could be a reflection. Check its edges and look for splash.'
    if coverage >= .006 and len(patches) >= 2:
        return 'Wet', 'Several persistent bright patches suggest a reflective surface. Check for sheen or spray.'
    if coverage < .002 and np.mean(highlights[stable]) < .035:
        return 'Dry', 'Little persistent reflection was detected in the outline. Check for subtle sheen or spray.'
    return 'Unknown', 'The reflection pattern is ambiguous.'


def marked_frame(still, points):
    image = Image.open(io.BytesIO(still)).convert('RGB')
    draw = ImageDraw.Draw(image, 'RGBA')
    if points:
        draw.polygon([(x * image.width / 100, y * image.height / 100) for x, y in points],
                     fill=(0, 175, 155, 45), outline='#00ffe0', width=3)
    for percent in range(10, 100, 10):
        x, y = image.width * percent / 100, image.height * percent / 100
        draw.line((x, 0, x, image.height), fill=(255, 255, 0, 100))
        draw.line((0, y, image.width, y), fill=(255, 255, 0, 100))
        draw.text((x + 2, 4), str(percent), fill='yellow', stroke_width=1, stroke_fill='black')
        draw.text((4, y + 2), str(percent), fill='yellow', stroke_width=1, stroke_fill='black')
    return image


def render_road_conditions(workspace):
    st.caption('Review visible pavement in recorded video and save human labels for later analysis.')
    folder = workspace.directory / 'road_conditions'
    labels = [json.loads(path.read_text(encoding='utf-8')) for path in sorted(folder.glob('*.json'))]
    if labels:
        with st.expander('Saved clip labels'):
            st.dataframe([{'Video': item['source_name'], 'Start (s)': item['start_seconds'],
                           'Clip length (s)': item['requested_duration_seconds'],
                           'Condition': item['condition']} for item in labels], hide_index=True)
        st.download_button('Export saved labels', json.dumps(labels, indent=2),
                           file_name='road-condition-labels.json', mime='application/json', on_click='ignore')
    sources = {item['id']: item['name'] for item in workspace.analysis.sources(workspace.jobs())}
    if not sources:
        st.info('Complete a camera recording or add an uploaded video in Video analysis.')
        return
    source = st.selectbox('Recorded video', list(sources), format_func=sources.get)
    path = workspace.analysis.source(source, workspace.jobs())
    stat = path.stat()
    revision = (stat.st_size, stat.st_mtime_ns)
    saved = {item['id']: item for item in labels}
    with st.form('road_clip_controls'):
        left, right = st.columns(2)
        start = left.number_input('Start time in seconds', min_value=0.0, step=1.0)
        duration = right.selectbox('Clip length in seconds', [5, 10, 15], index=1)
        if st.form_submit_button('Load review clip'):
            st.session_state.road_clip = (source, revision, start, duration)
    loaded = st.session_state.get('road_clip')
    if not loaded or loaded[:2] != (source, revision):
        return
    _, _, start, duration = loaded
    clip_id = hashlib.sha256(json.dumps(loaded).encode()).hexdigest()
    ffmpeg = workspace.analysis.ffmpeg()
    if not ffmpeg:
        st.error('FFmpeg is required to review clips.')
        return
    try:
        with st.spinner('Loading review clip…'):
            still, clip = clip_media(str(path), revision, ffmpeg, start, duration)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        st.error(f'Could not load the clip. {error}')
        return
    existing = saved.get(clip_id, {})
    previous_points = existing.get('road_area_percent', [])
    default_area = '; '.join(f'{x:g},{y:g}' for x, y in previous_points)
    st.caption(f'Reviewing {duration} seconds starting at {start:g} s, or through the end of the recording.')
    text = st.text_input('Road outline corners', value=default_area, key=f'road_area_{clip_id}',
                         placeholder='35,30; 55,30; 75,85; 15,85',
                         help='Use the grid to enter x,y percentages from the top-left. Separate corners with semicolons and follow the road boundary in order.')
    points = []
    if text.strip():
        try:
            points = parse_area(text)
        except ValueError as error:
            st.warning(str(error))
    left, right = st.columns(2)
    left.image(marked_frame(still, points), caption='First frame with the marked pavement area')
    right.video(clip)
    label_key = f'road_label_{clip_id}'
    if points:
        try:
            suggestion, reason = suggest_condition(pavement_frames(clip, ffmpeg), points)
            st.write(f'**Suggested condition · {suggestion}**')
            st.caption(f'{reason} This visual estimate can confuse reflections, lane markings, and shadows.')
            if st.button('Use suggestion', key=f'road_suggestion_{clip_id}'):
                st.session_state[label_key] = suggestion
        except (ValueError, OSError, subprocess.TimeoutExpired):
            st.info('Automatic suggestion unavailable for this clip. You can still label it manually.')
    if label_key not in st.session_state:
        st.session_state[label_key] = existing.get('condition')
    condition = st.selectbox('Road condition', CONDITIONS, key=label_key,
                             index=None,
                             placeholder='Choose a condition')
    if st.button('Save road condition', type='primary', disabled=not points or not condition):
        item = {'id': clip_id, 'source': source, 'source_name': sources[source],
                'source_size_bytes': revision[0], 'source_modified_ns': revision[1],
                'start_seconds': start, 'requested_duration_seconds': duration,
                'road_area_percent': points, 'condition': condition}
        try:
            folder.mkdir(exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=folder,
                                             suffix='.tmp', delete=False) as handle:
                json.dump(item, handle, allow_nan=False)
            temporary = Path(handle.name)
            try:
                replace_file(temporary, folder / f'{clip_id}.json')
            finally:
                temporary.unlink(missing_ok=True)
        except OSError as error:
            st.error(f'Could not save the label. {error}')
        else:
            st.session_state.road_saved = clip_id
            st.rerun()
    if st.session_state.pop('road_saved', None) == clip_id:
        st.success('Road condition saved.')
    elif existing:
        st.caption(f"Saved label: {existing['condition']}")
