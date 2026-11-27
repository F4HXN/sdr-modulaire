"""Panneau des réglages classiques : source, démodulation, affichage."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QProgressBar, QPushButton, QSlider,
                               QSpinBox, QVBoxLayout, QWidget)

from ..config import TAILLES_FFT, TAUX_ECHANTILLONNAGE, Reglages


def texte_plages(plages) -> str:
    def mhz(f):
        return f"{f / 1e6:g}"
    return " · ".join(f"{mhz(a)}–{mhz(b)} MHz" for a, b in plages)


def _curseur(mini: int, maxi: int, valeur: int) -> QSlider:
    s = QSlider(Qt.Horizontal)
    s.setRange(mini, maxi)
    s.setValue(valeur)
    return s


class PanneauReglages(QWidget):
    taux_change = Signal(int)
    gain_change = Signal(object)        # float | None
    ajuster_gain = Signal()
    ppm_change = Signal(int)
    direct_change = Signal(int)
    convertisseur_change = Signal(float)   # Hz
    bias_t_change = Signal(bool)
    largeur_change = Signal(float)
    agc_change = Signal(bool)
    squelch_change = Signal(float)
    volume_change = Signal(float)
    fft_change = Signal(int)
    moyennage_change = Signal(float)
    zoom_change = Signal(float)
    niveaux_change = Signal(float, float)
    auto_echelle = Signal()

    def __init__(self, r: Reglages, parent=None) -> None:
        super().__init__(parent)
        self._gains: list[float] = []
        racine = QVBoxLayout(self)

        # --- Matériel détecté
        g = QGroupBox("Matériel")
        f = QFormLayout(g)
        self.m_modele = QLabel("— (démarre la source)")
        self.m_tuner = QLabel("—")
        self.m_plage = QLabel("—")
        self.m_gain = QLabel("—")
        self.m_taux = QLabel("—")
        for lbl in (self.m_modele, self.m_tuner, self.m_plage, self.m_gain, self.m_taux):
            lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f.addRow("Modèle", self.m_modele)
        f.addRow("Tuner", self.m_tuner)
        f.addRow("Couverture", self.m_plage)
        f.addRow("Gain", self.m_gain)
        f.addRow("Taux max", self.m_taux)
        racine.addWidget(g)

        # --- Source
        g = QGroupBox("Source")
        f = QFormLayout(g)
        self.taux = QComboBox()
        for t in TAUX_ECHANTILLONNAGE:
            self.taux.addItem(f"{t / 1e6:.3f} MS/s", t)
        self.taux.setCurrentIndex(max(0, self.taux.findData(r.taux)))
        self.taux.currentIndexChanged.connect(lambda _: self.taux_change.emit(self.taux.currentData()))
        f.addRow("Échantillonnage", self.taux)

        self.gain_auto = QCheckBox("Auto")
        self.gain_auto.setChecked(r.gain is None)
        self.gain = _curseur(0, 0, 0)
        self.gain_lbl = QLabel("—")
        self.gain_lbl.setMinimumWidth(48)
        ligne = QHBoxLayout()
        ligne.addWidget(self.gain_auto)
        ligne.addWidget(self.gain, 1)
        ligne.addWidget(self.gain_lbl)
        self.b_ajuster = QPushButton("Ajuster")
        self.b_ajuster.setToolTip("Cherche le gain le plus fort qui ne fait pas saturer la clé (quelques secondes), "
                                  "puis recale les niveaux du spectre")
        self.b_ajuster.clicked.connect(self.ajuster_gain)
        ligne.addWidget(self.b_ajuster)
        f.addRow("Gain RF", ligne)
        self.sat_lbl = QLabel("—")
        self.sat_lbl.setToolTip("Part des échantillons que la clé écrête (octet 0 ou 255). Au-delà de 0,05 %, "
                                "baissez le gain RF ou cliquez sur « Ajuster ».")
        f.addRow("Saturation", self.sat_lbl)
        self.gain_auto.toggled.connect(self._emettre_gain)
        self.gain.valueChanged.connect(self._emettre_gain)

        self.ppm = QSpinBox()
        self.ppm.setRange(-200, 200)
        self.ppm.setValue(r.ppm)
        self.ppm.valueChanged.connect(self.ppm_change)
        f.addRow("Correction (ppm)", self.ppm)

        self.direct = QComboBox()
        self.direct.addItems(["Désactivé", "Branche I", "Branche Q (HF, V3)"])
        self.direct.setCurrentIndex(r.echantillonnage_direct)
        self.direct.currentIndexChanged.connect(self.direct_change)
        f.addRow("Échant. direct", self.direct)

        self.convertisseur = QDoubleSpinBox()
        self.convertisseur.setRange(-2000.0, 2000.0)
        self.convertisseur.setDecimals(3)
        self.convertisseur.setSuffix(" MHz")
        self.convertisseur.setValue(r.convertisseur / 1e6)
        self.convertisseur.setToolTip("Décalage d'un convertisseur HF (Ham It Up : 125 MHz). 0 sans convertisseur.")
        self.convertisseur.editingFinished.connect(lambda: self.convertisseur_change.emit(self.convertisseur.value() * 1e6))
        f.addRow("Convertisseur", self.convertisseur)

        self.bias = QCheckBox("Alimenter l'antenne (bias-T)")
        self.bias.setChecked(r.bias_t)
        self.bias.toggled.connect(self.bias_t_change)
        f.addRow(self.bias)
        racine.addWidget(g)

        # --- Démodulation
        g = QGroupBox("Démodulation")
        f = QFormLayout(g)
        self.largeur = QDoubleSpinBox()
        self.largeur.setDecimals(0)
        self.largeur.setSuffix(" Hz")
        self.largeur.setKeyboardTracking(False)
        self.largeur.valueChanged.connect(self.largeur_change)
        f.addRow("Bande passante", self.largeur)

        self.agc = QCheckBox("AGC audio")
        self.agc.setChecked(r.agc)
        self.agc.toggled.connect(self.agc_change)
        f.addRow(self.agc)

        self.squelch = _curseur(-120, 0, int(r.squelch))
        self.squelch_lbl = QLabel()
        self.niveau = QProgressBar()
        self.niveau.setRange(-120, 0)
        self.niveau.setTextVisible(True)
        self.niveau.setFormat("%v dBFS")
        self.niveau.setMaximumHeight(16)
        ligne = QHBoxLayout()
        ligne.addWidget(self.squelch, 1)
        ligne.addWidget(self.squelch_lbl)
        f.addRow("Squelch", ligne)
        f.addRow("Niveau canal", self.niveau)
        self.squelch.valueChanged.connect(self._emettre_squelch)

        self.volume = _curseur(0, 100, int(r.volume * 100))
        self.volume.valueChanged.connect(lambda v: self.volume_change.emit(v / 100))
        f.addRow("Volume", self.volume)
        racine.addWidget(g)

        # --- Affichage
        g = QGroupBox("Affichage")
        f = QFormLayout(g)
        self.fft = QComboBox()
        for n in TAILLES_FFT:
            self.fft.addItem(str(n), n)
        self.fft.setCurrentIndex(max(0, self.fft.findData(r.taille_fft)))
        self.fft.currentIndexChanged.connect(lambda _: self.fft_change.emit(self.fft.currentData()))
        f.addRow("Taille FFT", self.fft)

        self.zoom = QComboBox()
        self.zoom.setToolTip("Largeur de fréquence affichée par le spectre et le waterfall, autour du VFO")
        self._zooms_pour(r.taux, r.zoom)
        self.zoom.currentIndexChanged.connect(lambda _: self.zoom_change.emit(float(self.zoom.currentData() or 0)))
        f.addRow("Largeur", self.zoom)

        self.moyennage = _curseur(0, 95, int(r.moyennage * 100))
        self.moyennage.valueChanged.connect(lambda v: self.moyennage_change.emit(v / 100))
        f.addRow("Moyenne", self.moyennage)

        self.n_min = _curseur(-150, 0, int(r.niveau_min))
        self.n_max = _curseur(-150, 0, int(r.niveau_max))
        self.n_lbl = QLabel()
        self.n_min.valueChanged.connect(self._emettre_niveaux)
        self.n_max.valueChanged.connect(self._emettre_niveaux)
        f.addRow("Plancher", self.n_min)
        f.addRow("Plafond", self.n_max)
        bouton = QPushButton("Échelle auto")
        bouton.clicked.connect(self.auto_echelle)
        ligne = QHBoxLayout()
        ligne.addWidget(self.n_lbl, 1)
        ligne.addWidget(bouton)
        f.addRow(ligne)
        racine.addWidget(g)
        racine.addStretch(1)

        self._maj_libelles()

    # ------------------------------------------------------------ mises à jour silencieuses
    def regler_gains(self, gains: list[float], actuel: float | None) -> None:
        self._gains = gains
        self.gain.blockSignals(True)
        self.gain.setRange(0, max(0, len(gains) - 1))
        if gains and actuel is not None:
            self.gain.setValue(min(range(len(gains)), key=lambda i: abs(gains[i] - actuel)))
        self.gain.blockSignals(False)
        self._maj_libelles()

    def appliquer_materiel(self, infos: dict) -> None:
        """Affiche le matériel détecté et grise ce qu'il ne sait pas faire."""
        modele = infos.get("modele", "?")
        if infos.get("fabricant"):
            modele = f"{infos['fabricant']} {modele}"
        if infos.get("numero_serie"):
            modele += f"  (S/N {infos['numero_serie']})"
        self.m_modele.setText(modele)
        self.m_tuner.setText(infos.get("tuner", "?"))
        self.m_tuner.setToolTip(infos.get("remarque", ""))
        self.m_plage.setText(texte_plages(infos.get("plages", [])) or "inconnue")
        gains = infos.get("gains") or []
        self.m_gain.setText(f"{min(gains):.1f} à {max(gains):.1f} dB ({len(gains)} paliers)" if gains else "—")
        taux_max = infos.get("taux_max")
        self.m_taux.setText(f"{taux_max / 1e6:.3f} MS/s (stable)" if taux_max else "—")

        # taux d'échantillonnage au-delà du maximum stable : grisés
        modele_combo = self.taux.model()
        for i in range(self.taux.count()):
            ok = not taux_max or self.taux.itemData(i) <= taux_max
            modele_combo.item(i).setEnabled(ok)

        for widget, cle, texte in ((self.bias, "bias_t", "Pas de bias-T sur cette clé"),
                                   (self.direct, "echantillonnage_direct",
                                    "Échantillonnage direct inutilisable avec ce tuner")):
            possible = infos.get(cle)
            widget.setEnabled(possible is not False)
            widget.setToolTip(texte if possible is False else
                              ("Présence non garantie sur ce matériel" if possible is None else ""))

    def regler_demod(self, mini: float, maxi: float, pas: float, valeur: float) -> None:
        self.largeur.blockSignals(True)
        self.largeur.setRange(mini, maxi)
        self.largeur.setSingleStep(pas)
        self.largeur.setValue(valeur)
        self.largeur.setEnabled(mini != maxi)
        self.largeur.blockSignals(False)

    def regler_niveaux(self, mini: float, maxi: float) -> None:
        for s, v in ((self.n_min, mini), (self.n_max, maxi)):
            s.blockSignals(True)
            s.setValue(int(v))
            s.blockSignals(False)
        self._maj_libelles()

    def choisir_gain(self, i: int) -> None:
        """Gain manuel sur le palier i (émet gain_change)."""
        self.gain_auto.blockSignals(True)
        self.gain_auto.setChecked(False)
        self.gain_auto.blockSignals(False)
        self.gain.blockSignals(True)
        self.gain.setValue(i)
        self.gain.blockSignals(False)
        self._emettre_gain()

    def afficher_saturation(self, taux: float | None) -> None:
        if taux is None:
            self.sat_lbl.setText("—")
            self.sat_lbl.setStyleSheet("")
            return
        self.sat_lbl.setText("aucune" if taux < 1e-4 else f"{100 * taux:.2f} %".replace(".", ","))
        self.sat_lbl.setStyleSheet("color: #ff5252; font-weight: bold" if taux > 5e-4 else "")

    def afficher_niveau(self, dbfs: float) -> None:
        self.niveau.setValue(int(max(-120, min(0, dbfs))))

    # ------------------------------------------------------------ interne
    def gain_actuel(self) -> float | None:
        if self.gain_auto.isChecked() or not self._gains:
            return None
        return self._gains[self.gain.value()]

    def _emettre_gain(self, *_):
        self.gain.setEnabled(not self.gain_auto.isChecked())
        self._maj_libelles()
        self.gain_change.emit(self.gain_actuel())

    def _emettre_squelch(self, v: int) -> None:
        self._maj_libelles()
        self.squelch_change.emit(float(v))

    def _emettre_niveaux(self, *_):
        if self.n_min.value() >= self.n_max.value() - 5:
            return
        self._maj_libelles()
        self.niveaux_change.emit(float(self.n_min.value()), float(self.n_max.value()))

    def _maj_libelles(self) -> None:
        g = self.gain_actuel()
        self.gain_lbl.setText("auto" if g is None else f"{g:.1f} dB".replace(".", ","))
        self.gain.setEnabled(not self.gain_auto.isChecked())
        s = self.squelch.value()
        self.squelch_lbl.setText("ouvert" if s <= -120 else f"{s} dBFS")
        self.n_lbl.setText(f"{self.n_min.value()} … {self.n_max.value()} dBFS")

    ZOOMS = (0.0, 1_000_000.0, 500_000.0, 200_000.0, 100_000.0, 50_000.0, 20_000.0)

    ZOOMS_AUDIO = [3_000, 4_000, 6_000, 12_000, 24_000]

    def zooms_audio(self, actuel: float) -> None:
        """Source audio : largeurs du spectre du son, comptées depuis 0 Hz."""
        self.zoom.blockSignals(True)
        self.zoom.clear()
        for z in self.ZOOMS_AUDIO:
            self.zoom.addItem(f"0–{z / 1e3:g} kHz (son)", float(z))
        self.zoom.setCurrentIndex(max(0, self.zoom.findData(float(actuel))))
        self.zoom.blockSignals(False)

    def _zooms_pour(self, taux: float, actuel: float) -> None:
        """Largeurs proposées : plus petites que la bande reçue."""
        self.zoom.blockSignals(True)
        self.zoom.clear()
        for z in self.ZOOMS:
            if z == 0 or z < taux:
                self.zoom.addItem("Toute la bande" if z == 0 else (f"{z / 1e6:g} MHz" if z >= 1e6 else f"{z / 1e3:g} kHz"), z)
        i = self.zoom.findData(actuel)
        self.zoom.setCurrentIndex(max(0, i))
        self.zoom.blockSignals(False)
        if i < 0 and actuel:
            self.zoom_change.emit(0.0)
