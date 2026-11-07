"""Dev helper: render the app offscreen and save screenshots. Usage: python tests_gui_shot.py <project> <outdir>"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from overdub import engine  # noqa
from overdub.__main__ import setup_app
from overdub.main_window import MainWindow

proj, out = sys.argv[1], sys.argv[2]
app = QApplication(sys.argv[:1]); setup_app(app)
w = MainWindow(); w.resize(1400, 860); w.show()
w.grab().save(f"{out}/welcome.png")
w.load_project_folder(proj)
def s1():
    w.state.sel = (int(2.5 * 48000), int(4 * 48000)); w.state.changed.emit()
    QTest.keyClick(w, Qt.Key_Space)
    QTimer.singleShot(700, s2)
def s2():
    w.grab().save(f"{out}/playing.png")
    QTest.keyClick(w, Qt.Key_Space)
    w.show_master_fx(); w.grab().save(f"{out}/master.png")
    print("input:", w.in_device_name, w._input_names())
    w.close(); app.quit()
QTimer.singleShot(1500, s1)
app.exec()
