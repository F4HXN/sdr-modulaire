"""Afficheur de fréquence façon transceiver : molette sur un chiffre, clic haut/bas."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter
from PySide6.QtWidgets import QInputDialog, QSizePolicy, QWidget


class AfficheurFrequence(QWidget):
    frequence_demandee = Signal(float)

    NB_CHIFFRES = 10        # jusqu'à 9,999 999 999 GHz

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._f = 0
        self.hors_plage = False
        self._survol: int | None = None
        self._zones: list[tuple[QRectF, int]] = []
        self.police = QFont("DejaVu Sans Mono", 26, QFont.Bold)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        fm = QFontMetricsF(self.police)
        self.setFixedSize(int(fm.horizontalAdvance("0") * 13.6 + 24), int(fm.height() + 12))
        self.setToolTip("Molette sur un chiffre pour le modifier · clic haut/bas : ± · double-clic : saisie")

    def frequence(self) -> float:
        return float(self._f)

    def regler(self, f: float) -> None:
        self._f = max(0, int(round(f)))
        self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#101418"))
        p.setFont(self.police)
        fm = QFontMetricsF(self.police)
        texte = f"{self._f:0{self.NB_CHIFFRES}d}"
        premier_significatif = next((i for i, c in enumerate(texte) if c != "0"), len(texte) - 1)
        x = 12.0
        y_base = (self.height() + fm.ascent() - fm.descent()) / 2
        self._zones = []
        for i, c in enumerate(texte):
            puissance = self.NB_CHIFFRES - 1 - i
            larg = fm.horizontalAdvance(c)
            zone = QRectF(x, 0, larg, self.height())
            self._zones.append((zone, puissance))
            if puissance == self._survol:
                p.fillRect(zone.adjusted(0, 4, 0, -4), QColor("#2a3540"))
            vif, sombre = (("#ff5252", "#4a1a1a") if self.hors_plage else ("#ffb300", "#4a3a10"))
            couleur = QColor(vif) if i >= premier_significatif else QColor(sombre)
            p.setPen(couleur)
            p.drawText(x, y_base, c)
            x += larg
            if puissance in (9, 6, 3):
                p.setPen(couleur)
                p.drawText(x - 2, y_base, ".")
                x += fm.horizontalAdvance(".") * 0.6
        p.setPen(QColor("#8899aa"))
        p.setFont(QFont("DejaVu Sans", 9))
        p.drawText(x + 4, y_base, "Hz")

    def _puissance(self, pos) -> int | None:
        for zone, puissance in self._zones:
            if zone.contains(pos):
                return puissance
        return None

    def _modifier(self, delta: int) -> None:
        nouveau = max(0, min(self._f + delta, 10 ** self.NB_CHIFFRES - 1))
        if nouveau != self._f:
            self.regler(nouveau)
            self.frequence_demandee.emit(float(nouveau))

    def wheelEvent(self, ev) -> None:
        p = self._puissance(ev.position())
        if p is not None and ev.angleDelta().y():
            self._modifier((1 if ev.angleDelta().y() > 0 else -1) * 10 ** p)
        ev.accept()

    def mouseMoveEvent(self, ev) -> None:
        p = self._puissance(ev.position())
        if p != self._survol:
            self._survol = p
            self.update()

    def leaveEvent(self, _ev) -> None:
        self._survol = None
        self.update()

    def mousePressEvent(self, ev) -> None:
        if ev.button() != Qt.LeftButton:
            return
        p = self._puissance(ev.position())
        if p is not None:
            self._modifier((1 if ev.position().y() < self.height() / 2 else -1) * 10 ** p)

    def mouseDoubleClickEvent(self, _ev) -> None:
        mhz, ok = QInputDialog.getDouble(self, "Fréquence", "Fréquence (MHz) :",
                                         self._f / 1e6, 0.0, 9999.0, 6)
        if ok:
            self.regler(mhz * 1e6)
            self.frequence_demandee.emit(self._f)
