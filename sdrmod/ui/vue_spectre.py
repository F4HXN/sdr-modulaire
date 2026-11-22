"""Spectre + waterfall (pyqtgraph), clic pour s'accorder, molette pour avancer d'un pas."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QRectF, Qt, Signal

pg.setConfigOptions(imageAxisOrder="row-major", antialias=False)

LIGNES_WATERFALL = 300
COLONNES_MAX = 2048


class VueSpectre(pg.GraphicsLayoutWidget):
    clic_frequence = Signal(float)   # Hz absolus
    molette = Signal(int)            # +1 / -1

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setBackground("#0b0f14")
        self.niveaux = (-110.0, -40.0)
        self._geometrie = None
        self._buf = np.full((LIGNES_WATERFALL, 16), -150.0, dtype=np.float32)
        self._rect = None

        self.p_spec = self.addPlot(row=0, col=0)
        self.p_wf = self.addPlot(row=1, col=0)
        self.ci.layout.setRowStretchFactor(0, 2)
        self.ci.layout.setRowStretchFactor(1, 3)
        self.ci.layout.setSpacing(0)

        for p in (self.p_spec, self.p_wf):
            p.setMouseEnabled(x=False, y=False)
            p.hideButtons()
            p.setMenuEnabled(False)
        self.p_spec.showGrid(x=True, y=True, alpha=0.25)
        self.p_spec.setLabel("left", "dBFS")
        self.p_spec.hideAxis("bottom")
        self.p_wf.hideAxis("left")
        self.p_wf.setLabel("bottom", "MHz")
        self.p_wf.setXLink(self.p_spec)
        self.p_spec.getAxis("left").setWidth(48)
        self.p_wf.getAxis("left").setWidth(48)
        self.p_wf.showAxis("left")
        self.p_wf.getAxis("left").setStyle(showValues=False)
        self.p_wf.getAxis("left").setPen(pg.mkPen(None))

        self.courbe = self.p_spec.plot(pen=pg.mkPen("#4fc3f7", width=1))
        self.img = pg.ImageItem()
        self.img.setLookupTable(pg.colormap.get("turbo").getLookupTable(nPts=256))
        self.p_wf.addItem(self.img)
        self.p_wf.setYRange(0, LIGNES_WATERFALL, padding=0)

        self.bandes, self.lignes = [], []
        for p in (self.p_spec, self.p_wf):
            bande = pg.LinearRegionItem(movable=False, brush=pg.mkBrush(255, 255, 255, 40),
                                        pen=pg.mkPen(255, 255, 255, 60))
            ligne = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#ff5252", width=1))
            p.addItem(bande)
            p.addItem(ligne)
            self.bandes.append(bande)
            self.lignes.append(ligne)

        self._glisse = False
        self.viewport().installEventFilter(self)
        self.regler_niveaux(*self.niveaux)

    # ------------------------------------------------------------ API
    def regler_niveaux(self, mini: float, maxi: float) -> None:
        self.niveaux = (mini, maxi)
        self.p_spec.setYRange(mini - 5, maxi + 10, padding=0)
        self.img.setLevels(self.niveaux)

    def regler_vfo(self, f_hz: float, bas: float, haut: float) -> None:
        for ligne in self.lignes:
            ligne.setValue(f_hz / 1e6)
        for bande in self.bandes:
            bande.setRegion(((f_hz + bas) / 1e6, (f_hz + haut) / 1e6))

    def dernier_spectre(self) -> np.ndarray | None:
        return getattr(self, "_dernier", None)

    def maj(self, donnees) -> None:
        db, centre, taux = donnees        # taux : largeur affichée (toute la bande, ou la portion zoomée)
        n = len(db)
        geometrie = (n, centre, taux)
        if geometrie != self._geometrie:
            if self._geometrie is not None and self._geometrie[2] != taux:
                self._buf[:] = -150.0     # autre largeur : l'ancien waterfall ne correspond plus
            self._geometrie = geometrie
            self.x = (centre + (np.arange(n) - n / 2) * taux / n) / 1e6
            x0, larg = (centre - taux / 2) / 1e6, taux / 1e6
            self.p_spec.setXRange(x0, x0 + larg, padding=0)
            self._rect = QRectF(x0, 0, larg, LIGNES_WATERFALL)
        self._dernier = db
        self.courbe.setData(self.x, db)

        # réduction pour le waterfall (max par groupe de colonnes)
        ligne = db
        if n > COLONNES_MAX:
            k = -(-n // COLONNES_MAX)        # n n'est plus forcément une puissance de 2 avec le zoom
            ligne = np.pad(db, (0, k * COLONNES_MAX - n), constant_values=-150.0).reshape(COLONNES_MAX, k).max(axis=1)
        if self._buf.shape[1] != len(ligne):
            self._buf = np.full((LIGNES_WATERFALL, len(ligne)), -150.0, dtype=np.float32)
        self._buf[:-1] = self._buf[1:]
        self._buf[-1] = ligne
        self.img.setImage(self._buf, autoLevels=False, levels=self.niveaux)
        if self._rect is not None:
            # le cadre se pose après l'image : il dépend de sa taille (sinon waterfall étiré au démarrage)
            self.img.setRect(self._rect)
            self._rect = None

    # ------------------------------------------------------------ souris
    def _accorder(self, pos_vue) -> bool:
        """Émet la fréquence sous le pointeur ; False si hors des graphes."""
        pos = self.mapToScene(pos_vue.toPoint())
        for p in (self.p_spec, self.p_wf):
            if p.vb.sceneBoundingRect().contains(pos):
                self.clic_frequence.emit(p.vb.mapSceneToView(pos).x() * 1e6)
                return True
        return False

    def eventFilter(self, obj, ev) -> bool:
        t = ev.type()
        if t == QEvent.Wheel:
            d = ev.angleDelta().y()
            if d:
                self.molette.emit(1 if d > 0 else -1)
            return True
        # clic gauche = accord ; maintenir et glisser = accord continu
        if t == QEvent.MouseButtonPress and ev.button() == Qt.MouseButton.LeftButton:
            self._glisse = self._accorder(ev.position())
            return self._glisse
        if t == QEvent.MouseMove and self._glisse and ev.buttons() & Qt.MouseButton.LeftButton:
            self._accorder(ev.position())
            return True
        if t == QEvent.MouseButtonRelease and self._glisse:
            self._glisse = False
            return True
        return super().eventFilter(obj, ev)
