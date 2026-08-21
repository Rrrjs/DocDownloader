"""DocDownloader 独立 GUI 入口。"""

import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

from gui.icon_utils import ICON_PATH
from gui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("DocDownloader Workspace")
    app.setWindowIcon(QIcon(str(ICON_PATH)) if ICON_PATH.exists() else QIcon())
    app.setFont(QFont("Segoe UI", 10))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
