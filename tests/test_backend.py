import base64
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
from urllib.parse import parse_qs,urlsplit

import backend


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.storage=patch.object(backend,'STORAGE',Path(self.temp.name))
        self.storage.start()
        self.manager=backend.Manager()
        self.first=self.manager.workspace('a'*32)
        self.second=self.manager.workspace('b'*32)

    def tearDown(self):
        self.manager.close()
        self.storage.stop()
        self.temp.cleanup()

    def test_workspaces_isolate_files_jobs_and_upload_paths(self):
        upload=self.first.upload('../../my clip.mp4',b'video-fixture')
        self.assertTrue(self.first.analysis.source(upload,[]).is_file())
        with self.assertRaises(ValueError): self.second.analysis.source(upload,[])
        camera=next(c for c in self.first.catalog()['cameras'] if c['available'])
        future=time.time()+600
        with patch.object(self.first.recorder,'ffmpeg',return_value='ffmpeg'):
            self.manager.schedule(self.first,[camera['id']],future,future+60)
        self.assertEqual(len(self.first.jobs()),1)
        self.assertEqual(self.second.jobs(),[])
        self.assertEqual(self.first.recorder.DATA.parent,self.first.directory)
        self.assertNotEqual(self.first.recorder,self.second.recorder)

    def test_capacity_is_enforced_across_workspaces(self):
        ids=[c['id'] for c in self.first.catalog()['cameras'] if c['available']][:4]
        future=time.time()+600
        with patch.object(self.first.recorder,'ffmpeg',return_value='ffmpeg'),patch.object(self.second.recorder,'ffmpeg',return_value='ffmpeg'):
            self.manager.schedule(self.first,ids,future,future+60)
            with self.assertRaises(ValueError): self.manager.schedule(self.second,ids[:1],future,future+60)
            self.manager.schedule(self.second,ids[:1],future+60,future+120)

    def test_radar_uses_selected_camera_coordinates(self):
        url=backend.radar_url({'longitude':-81.87822,'latitude':30.306236})
        encoded=parse_qs(urlsplit(url).query)['settings'][0][3:]
        settings=json.loads(base64.b64decode(encoded))
        self.assertEqual(settings['agenda']['center'],[-81.87822,30.306236])
        self.assertEqual(settings['agenda']['zoom'],9)
        with self.assertRaises(ValueError): backend.radar_url(station='arbitrary')

    def test_unsafe_workspace_keys_and_source_paths_are_rejected(self):
        with self.assertRaises(ValueError): self.manager.workspace('../../private')
        with self.assertRaises(ValueError): self.first.analysis.source('upload:../../private.mp4',[])

    def test_capture_failure_does_not_leave_job_recording(self):
        camera=next(c for c in self.first.catalog()['cameras'] if c['available'])
        future=time.time()+600
        with patch.object(self.first.recorder,'ffmpeg',return_value='ffmpeg'):
            self.manager.schedule(self.first,[camera['id']],future,future+60)
        self.first.stop.set()
        self.first.scheduler_thread.join(timeout=5)
        with self.first.recorder.connect() as db:
            job=dict(db.execute('SELECT * FROM jobs').fetchone())
        fake_stop=Mock()
        fake_stop.is_set.return_value=False
        with patch.object(self.first.recorder,'STOP',fake_stop),patch.object(self.first.recorder,'resolve_stream',side_effect=RuntimeError('feed offline')):
            self.first.recorder.record(job)
        result=self.first.jobs()[0]
        self.assertEqual(result['status'],'failed')
        self.assertNotIn(camera['name'],result['file'])
        self.assertIn('camera_'+camera['id'],result['file'])


if __name__=='__main__': unittest.main()
