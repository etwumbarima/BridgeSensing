from pathlib import Path
import unittest

from streamlit.testing.v1 import AppTest


class StreamlitUITests(unittest.TestCase):
    def test_directory_selection_and_all_pages_render(self):
        app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')).run(timeout=40)
        self.assertEqual([e.message for e in app.exception],[])
        self.assertGreater(len(app.checkbox),1)
        app.checkbox[1].check().run(timeout=30)
        self.assertEqual(len(app.session_state['selected_ids']),1)
        for page in ('Live conditions','Recordings','Video analysis','Cameras'):
            app.sidebar.radio[0].set_value(page).run(timeout=30)
            self.assertEqual([e.message for e in app.exception],[],page)
        self.assertEqual(len(app.session_state['selected_ids']),1)


if __name__=='__main__': unittest.main()
