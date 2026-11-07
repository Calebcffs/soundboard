import sys

from . import engine  # noqa: F401  (sets PipeWire env vars before PortAudio starts)
from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import QApplication

from .main_window import MainWindow

STYLE = """
QWidget { font-size: 12px; }
#transport { background: #121318; border-bottom: 1px solid #2a2d38; }
QToolButton { background: #2a2d38; border: 1px solid #353946; border-radius: 5px; padding: 3px 8px; color: #e8eaf0; }
QToolButton:hover { background: #343846; }
QToolButton:checked { background: #4c5bd4; border-color: #6875f0; }
#playBtn { font-size: 15px; }
#recBtn { color: #ff5252; font-size: 16px; }
#exportBtn { background: #2f6f4f; border-color: #3d8b63; font-weight: 600; }
#exportBtn:hover { background: #3a8560; }
#muteBtn, #soloBtn { padding: 0px; font-weight: 600; }
#muteBtn:checked { background: #c77d1a; border-color: #e59a35; }
#soloBtn:checked { background: #c9b11a; border-color: #e6cf35; color: #111; }
QGroupBox { border: 1px solid #2f3340; border-radius: 6px; margin-top: 14px; padding: 8px 8px 4px 8px; background: #1b1d24; }
QGroupBox::indicator { width: 13px; height: 13px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #e8eaf0; font-weight: 600; }
QSlider::groove:horizontal { height: 4px; background: #343846; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #5b6ee1; border-radius: 2px; }
QSlider::handle:horizontal { background: #e8eaf0; width: 12px; margin: -5px 0; border-radius: 6px; }
QPushButton { background: #2a2d38; border: 1px solid #353946; border-radius: 5px; padding: 5px 12px; }
QPushButton:hover { background: #343846; }
QComboBox { background: #2a2d38; border: 1px solid #353946; border-radius: 4px; padding: 2px 6px; }
QListWidget { background: #1b1d24; border: 1px solid #2f3340; border-radius: 6px; }
QSplitter::handle { background: #2a2d38; }
QScrollBar:horizontal { height: 12px; }
"""


def setup_app(app: QApplication):
    app.setApplicationName("Overdub")
    app.setDesktopFileName("overdub")
    app.setStyle("Fusion")
    pal = QPalette()
    for role, col in [(QPalette.Window, "#1a1b21"), (QPalette.WindowText, "#e8eaf0"),
                      (QPalette.Base, "#14151a"), (QPalette.AlternateBase, "#1d1f26"),
                      (QPalette.Text, "#e8eaf0"), (QPalette.Button, "#2a2d38"),
                      (QPalette.ButtonText, "#e8eaf0"), (QPalette.Highlight, "#4c5bd4"),
                      (QPalette.HighlightedText, "#ffffff"), (QPalette.ToolTipBase, "#2a2d38"),
                      (QPalette.ToolTipText, "#e8eaf0"), (QPalette.PlaceholderText, "#7b8091")]:
        pal.setColor(role, QColor(col))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor("#5a5f6e"))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#5a5f6e"))
    app.setPalette(pal)
    app.setStyleSheet(STYLE)


def main():
    app = QApplication(sys.argv)
    setup_app(app)
    w = MainWindow()
    w.show()
    if len(sys.argv) > 1:
        w.load_project_folder(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
