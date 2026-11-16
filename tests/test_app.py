from pathlib import Path
import unittest

from streamlit.testing.v1 import AppTest


class StreamlitUITests(unittest.TestCase):
    def test_directory_selection_and_all_pages_render(self):
        app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')).run(timeout=40)
        self.assertEqual([e.message for e in app.exception],[])
        self.assertGreater(len(app.checkbox),1)
        next(box for box in app.checkbox if (box.key or '').startswith('camera_')).check().run(timeout=30)
        self.assertEqual(len(app.session_state['selected_ids']),1)
        for page in ('Live conditions','Recordings','Video analysis','Cameras'):
            app.sidebar.radio[0].set_value(page).run(timeout=30)
            self.assertEqual([e.message for e in app.exception],[],page)
        self.assertEqual(len(app.session_state['selected_ids']),1)

    def test_rest_facility_filter(self):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py')).run(timeout=40)
        facility_filter = next(box for box in app.checkbox if box.label.startswith('Rest areas,'))
        facility_filter.check().run(timeout=30)
        self.assertFalse(list(app.exception))
        cameras = [box.label.casefold().replace('-', ' ') for box in app.checkbox
                   if (box.key or '').startswith('camera_')]
        self.assertTrue(cameras)
        self.assertTrue(all(any(term in name for term in ('rest area', 'service plaza', 'welcome center'))
                            for name in cameras))


if __name__=='__main__': unittest.main()
