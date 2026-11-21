"""Lancement : python3 -m sdrmod [--simulation] [--debug]"""

import argparse
import logging
import sys

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


def appliquer_theme_sombre(app: QApplication) -> None:
    p = QPalette()
    for role, couleur in ((QPalette.Window, "#1b2026"), (QPalette.WindowText, "#d8dee6"),
                          (QPalette.Base, "#12161b"), (QPalette.AlternateBase, "#1f252c"),
                          (QPalette.Text, "#d8dee6"), (QPalette.Button, "#252c34"),
                          (QPalette.ButtonText, "#d8dee6"), (QPalette.Highlight, "#2f7dd1"),
                          (QPalette.HighlightedText, "#ffffff"), (QPalette.ToolTipBase, "#252c34"),
                          (QPalette.ToolTipText, "#d8dee6")):
        p.setColor(role, QColor(couleur))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor("#5a6470"))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#5a6470"))
    app.setPalette(p)


def main() -> int:
    ap = argparse.ArgumentParser(prog="sdrmod", description="Récepteur SDR modulaire")
    ap.add_argument("--simulation", action="store_true", help="utiliser la source simulée (sans matériel)")
    ap.add_argument("--debug", action="store_true", help="journal détaillé")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s : %(message)s")

    app = QApplication(sys.argv)
    app.setApplicationName("SDR Modulaire")
    app.setStyle("Fusion")
    appliquer_theme_sombre(app)

    from .ui.fenetre import FenetrePrincipale
    fenetre = FenetrePrincipale(source="simulation" if args.simulation else None)
    fenetre.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
