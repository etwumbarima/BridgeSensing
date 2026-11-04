"""Local Florida 511 camera catalog and persistent scheduled recorder. No pip packages."""
import argparse
import concurrent.futures
import datetime as dt
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
OUTPUT = ROOT / 'recordings'
BASE = 'https://fl511.com'
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': BASE + '/', 'Origin': BASE}
LOCK = threading.RLock()
STOP = threading.Event()
PROCESSES = {}
ACTIVE = set()
TOKEN = secrets.token_urlsafe(32)
MAX_ACTIVE = 4
ANALYSIS = None
LIVE_DETECTION = None
LIVE_PREVIEW_SLOTS = threading.BoundedSemaphore(2)


class Database(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect():
    db = sqlite3.connect(DATA / 'recorder.db', timeout=30, factory=Database)
    db.row_factory = sqlite3.Row
    return db


def init():
    DATA.mkdir(exist_ok=True)
    OUTPUT.mkdir(exist_ok=True)
    with connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, camera TEXT, start REAL, end REAL, status TEXT, message TEXT, file TEXT)')
        db.execute("UPDATE jobs SET status='interrupted', message='Recorder stopped during capture; any partial file is retained.' WHERE status='recording'")


def fetch(url, payload=None, raw=False):
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={**HEADERS, **({'Content-Type': 'application/json'} if payload is not None else {})})
    with urllib.request.urlopen(req, timeout=25) as response:
        body = response.read()
    return body if raw else json.loads(body)


def load_catalog():
    path = DATA / 'cameras.json'
    return json.loads(path.read_text('utf-8')) if path.exists() else {'updated': None, 'cameras': []}


def camera_coordinates(row):
    # Florida 511 returns WGS84 camera points in longitude/latitude order.
    geography = (row.get('latLng') or {}).get('geography') or {}
    point = re.fullmatch(r'POINT\s*\(\s*([-+\d.eE]+)\s+([-+\d.eE]+)\s*\)', geography.get('wellKnownText', ''))
    if point:
        longitude, latitude = map(float, point.groups())
        if -180 <= longitude <= 180 and -90 <= latitude <= 90:
            return {'latitude': latitude, 'longitude': longitude}
    return {'latitude': None, 'longitude': None}


def sync_catalog():
    cameras = []
    start = 0
    total = None
    while total is None or start < total:
        query = {'start': start, 'length': 100, 'search': {'value': ''},
                 'columns': [{'data': 'sortOrder', 'name': 'sortOrder', 's': True}],
                 'order': [{'column': 0, 'dir': 'asc'}]}
        for retry in range(4):
            try:
                response = fetch(BASE + '/List/GetData/Cameras?' + urllib.parse.urlencode({'query': json.dumps(query), 'lang': 'en'}))
                break
            except Exception:
                if retry == 3:
                    raise ValueError(f'Florida 511 catalog request failed at location {start}. Retry sync; cached catalog retained.') from None
                time.sleep(2 ** retry)
        rows = response['data']
        total = int(response['recordsTotal'])
        if not rows:
            raise ValueError('Florida 511 returned an incomplete catalog; previous catalog retained.')
        for row in rows:
            for image in row.get('images', []):
                disabled = any(image.get(k) for k in ('videoDisabled', 'disabled', 'blocked'))
                cameras.append({'id': str(image['id']), 'siteId': str(row['id']),
                    **camera_coordinates(row),
                    'name': row.get('location') or image.get('description') or str(image['id']),
                    'view': image.get('description') or '', 'roadway': row.get('roadway') or '',
                    'county': row.get('county') or '', 'region': row.get('region') or '',
                    'direction': row.get('direction') or '', 'videoUrl': image.get('videoUrl'),
                    'auth': bool(image.get('isVideoAuthRequired')),
                    'available': bool(image.get('videoUrl')) and not disabled,
                    'snapshot': urllib.parse.urljoin(BASE, image.get('imageUrl') or '/map/Cctv/' + str(image['id']))})
        start += len(rows)
        time.sleep(.4)
    catalog = {'updated': time.time(), 'locations': total, 'cameras': list({c['id']: c for c in cameras}.values())}
    temp = DATA / 'cameras.tmp'
    temp.write_text(json.dumps(catalog), 'utf-8')
    temp.replace(DATA / 'cameras.json')
    return catalog


def resolve_stream(camera):
    url = camera.get('videoUrl')
    if camera.get('auth'):
        result = fetch(BASE + '/Camera/GetVideoUrl?' + urllib.parse.urlencode({'imageId': camera['id']}))
        if isinstance(result, str):
            url = result
        else:
            suffix = fetch('https://divas.cloud/VDS-API/SecureTokenUri/GetSecureTokenUriBySourceId', result)
            url = suffix if suffix.startswith('https://') else url + suffix
    if not url or urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
        raise ValueError('No supported video stream is available.')
    return url


def ffmpeg():
    return os.environ.get('FL511_FFMPEG') or shutil.which('ffmpeg')


def update(job_id, **values):
    with connect() as db:
        db.execute('UPDATE jobs SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?', [*values.values(), job_id])


def status(job_id):
    with connect() as db:
        row = db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
        return row['status'] if row else None


def get_jobs():
    with connect() as db:
        return [dict(row) for row in db.execute('SELECT * FROM jobs ORDER BY start DESC')]


def record(job):
    job_id = job['id']
    camera = json.loads(job['camera'])
    filename = 'camera_' + re.sub(r'[^a-zA-Z0-9_-]', '_', str(camera['id']))[:12] + '_' + dt.datetime.fromtimestamp(job['start'], dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + job_id + '.mkv'
    output = OUTPUT / filename
    try:
        with LOCK:
            if status(job_id) != 'queued' or STOP.is_set():
                return
            update(job_id, status='recording', message='Connecting to live streamâ€¦', file=filename)
        attempt = 0
        had_gap = time.time() > job['start'] + 5
        while time.time() < job['end'] - 1:
            if STOP.is_set() or status(job_id) == 'cancelled':
                return
            attempt += 1
            part = output if attempt == 1 else output.with_stem(output.stem + '_part' + str(attempt))
            try:
                url = resolve_stream(camera)
                duration = job['end'] - time.time()
                if duration <= 0:
                    break
                args = [ffmpeg(), '-hide_banner', '-loglevel', 'error', '-nostats', '-y',
                        '-rw_timeout', '15000000', '-user_agent', HEADERS['User-Agent'],
                        '-headers', 'Referer: https://fl511.com/\r\nOrigin: https://fl511.com\r\n',
                        '-i', url, '-t', str(duration), '-map', '0:v:0', '-an', '-c:v', 'copy', str(part)]
                with LOCK:
                    if STOP.is_set() or status(job_id) == 'cancelled':
                        return
                    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    PROCESSES[job_id] = process
                update(job_id, message='Capturing live video' + (f' â€¢ reconnect {attempt-1}' if attempt > 1 else ''))
                while process.poll() is None:
                    if STOP.is_set() or status(job_id) == 'cancelled' or time.time() >= job['end']:
                        stop_process(process)
                        break
                    time.sleep(.5)
                code = process.wait()
                if status(job_id) == 'cancelled' or STOP.is_set():
                    return
                if time.time() >= job['end'] - 2 and code == 0:
                    break
                had_gap = True
                update(job_id, message='Stream disconnected; refreshing access and retryingâ€¦')
            except Exception:
                had_gap = True
                update(job_id, message='Feed unavailable; refreshing access and retryingâ€¦')
            if attempt >= 6:
                break
            STOP.wait(min(5, max(0, job['end'] - time.time())))
        # A real video packet must exist; Matroska headers alone do not count.
        parts = []
        for part in OUTPUT.glob(output.stem + '*.mkv'):
            try:
                if part.stat().st_size > 4096:
                    parts.append(part)
            except OSError:
                continue
        with LOCK:
            if status(job_id) != 'cancelled':
                complete = bool(parts) and time.time() >= job['end'] - 2 and not had_gap
                update(job_id, status='completed' if complete else ('partial' if parts else 'failed'),
                       message=('Capture finished. Review footage for source interruptions.' if complete else
                                'Capture started late, ended early, or reconnected; available parts retained.' if parts else 'No video captured. Feed may be offline or access was rejected.'))
    except Exception:
        if status(job_id) != 'cancelled':
            update(job_id, status='failed', message='Capture worker failed. Any valid partial footage is retained; retry this camera.')
    finally:
        with LOCK:
            PROCESSES.pop(job_id, None)
            ACTIVE.discard(job_id)
            if STOP.is_set() and status(job_id) == 'recording':
                update(job_id, status='interrupted', message='Recorder stopped; partial files retained.')


def stop_process(process):
    if process.poll() is not None:
        return
    try:
        process.communicate(b'q\n', timeout=5)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        process.kill()
        process.wait()


def scheduler():
    while not STOP.wait(1):
        with LOCK, connect() as db:
            now = time.time()
            db.execute("UPDATE jobs SET status='missed', message='Scheduled window elapsed while recorder was unavailable.' WHERE status='queued' AND end<=?", (now,))
            rows = db.execute("SELECT * FROM jobs WHERE status='queued' AND start<=? AND end>? ORDER BY start", (now, now)).fetchall()
            for row in rows:
                if len(ACTIVE) >= MAX_ACTIVE:
                    break
                if row['id'] not in ACTIVE:
                    ACTIVE.add(row['id'])
                    threading.Thread(target=record, args=(dict(row),), daemon=True).start()


