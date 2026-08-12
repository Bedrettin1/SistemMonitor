# -*- coding: utf-8 -*-
"""SistemMonitor README icin ekran goruntusu yakalayici.

Kullanım:
    python tools/capture_screenshots.py

Ana pencere, telefon/QR baglanti dialogu ve (varsa) sistem tepsisi
baglam menüsünü PySide6 ile render edip assets/ altina PNG olarak kaydeder.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KAYNAK = os.path.join(ROOT, "kaynak")
ASSETS = os.path.join(ROOT, "assets")
sys.path.insert(0, KAYNAK)

import sistem_monitor as sm
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtCore import Qt


def _grab_widget(w, path, padding=0):
    w.show()
    w.raise_()
    w.activateWindow()
    QApplication.processEvents()
    w.repaint()
    QApplication.processEvents()
    pix = w.grab() if padding <= 0 else w.grab(
        w.rect().adjusted(-padding, -padding, padding, padding)
    )
    ok = pix.save(path, "PNG")
    print(f"  -> {os.path.relpath(path, ROOT)}  ({pix.width()}x{pix.height()}) saved={ok}")


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 9))
    sm.set_lang("tr")

    # 1) Ana pencere
    w = sm.Monitor()
    try:
        w.resize(1000, 640)
    except Exception:
        pass
    w.showNormal()
    w.raise_()
    # birkac tick ile canli degerleri doldur
    for _ in range(4):
        try:
            w._tick()
        except Exception as e:
            print("  _tick uyari:", e)
        QApplication.processEvents()
        time.sleep(0.6)
    _grab_widget(w, os.path.join(ASSETS, "screenshot-main.png"))

    # 2) Telefon / QR baglanti dialogu
    try:
        d = sm.PhoneConnectDialog(
            w, ip="192.168.1.24", port=8080,
            pair="ABC123", expiry=time.time() + 60, tls=False,
        )
        _grab_widget(d, os.path.join(ASSETS, "screenshot-phone.png"))
        d.close()
    except Exception as e:
        print("  phone dialog yakalanamadi:", e)

    # 3) Sistem tepsisi baglam menusu (varsa)
    try:
        tray = getattr(w, "_tray", None)
        if tray is not None and isinstance(tray, QSystemTrayIcon):
            menu = tray.contextMenu()
            if menu is not None:
                menu.popup(w.pos())
                QApplication.processEvents()
                time.sleep(0.3)
                _grab_widget(menu, os.path.join(ASSETS, "screenshot-tray.png"))
                menu.hide()
    except Exception as e:
        print("  tray menu yakalanamadi:", e)

    w.close()
    print("Tamamlandi.")


if __name__ == "__main__":
    main()
