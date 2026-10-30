from pathlib import Path
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtGui import QImage, QPainter
from PySide6.QtCore import Qt
from PIL import Image
app=QApplication([])
Path('.local').mkdir(exist_ok=True)
image=QImage(1024,1024,QImage.Format.Format_ARGB32)
image.fill(Qt.GlobalColor.transparent)
painter=QPainter(image); QSvgRenderer('assets/icon.svg').render(painter); painter.end()
image.save('.local/icon.png')
Image.open('.local/icon.png').save('assets/icon.icns')
