from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from analysis_service import AnalysisService
from vehicle_analysis import VehicleCounter


class CountingTests(unittest.TestCase):
    def test_counts_once_despite_recrossing_and_jitter(self):
        counter = VehicleCounter()
        for frame, y in enumerate([.4, .42, .49, .505, .52, .6, .4, .7]):
            counter.observe(1, 2, .9, .3, y, frame / 30)
        self.assertEqual(counter.summary()['crossings']['car']['down'], 1)
        self.assertEqual(counter.summary()['crossing_total'], 1)
        self.assertEqual(counter.summary()['unique_total'], 1)

    def test_unique_tracks_do_not_equal_line_crossings(self):
        counter = VehicleCounter()
        for t in range(4):
            counter.observe(1, 7, .8, .3, .2, t)
        self.assertEqual(counter.summary()['unique_tracks']['truck'], 1)
        self.assertEqual(counter.summary()['crossing_total'], 0)

    def test_type_votes_and_vertical_directions(self):
        counter = VehicleCounter('vertical', .5)
        counter.observe(1, 2, .4, .3, .2, 0)
        counter.observe(1, 7, .9, .35, .2, 1)
        event = counter.observe(1, 7, .9, .7, .2, 2)
        self.assertEqual(event['vehicle_type'], 'truck')
        self.assertEqual(event['direction'], 'right')
        for t, x in enumerate([.8, .75, .2]):
            counter.observe(2, 5, .9, x, .4, t)
        self.assertEqual(counter.summary()['crossings']['bus']['left'], 1)

    def test_analysis_rejects_arbitrary_paths_and_bad_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'recordings'
            output.mkdir()
            (output / 'test.mkv').write_bytes(b'x' * 5000)
            service = AnalysisService(root, output, threading.Event(), lambda: 'ffmpeg')
            with self.assertRaises(ValueError):
                service.source('recording:../../private.mkv', [])
            with patch.object(service, 'available', return_value=True):
                with self.assertRaises(ValueError):
                    service.enqueue({'source': 'recording:test.mkv', 'position': float('nan')}, [])
                job = service.enqueue({'source': 'recording:test.mkv'}, [])
                service.cancel(job['id'])
                self.assertEqual(service.states()[0]['status'], 'cancelled')
            active = [{'file': 'test.mkv', 'status': 'recording'}]
            self.assertEqual(service.sources(active), [])


if __name__ == '__main__':
    unittest.main()
