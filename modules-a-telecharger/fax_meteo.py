# SPDX-License-Identifier: MIT
"""Fax météo (WEFAX) pour SDR Modulaire : les cartes météo des stations en ondes courtes.

Repris du décodeur de l'application IC-7300 Scope (wefax.py, même auteur), avec la même liste de stations.
Standard des stations (DWD Pinneberg, Northwood, Athènes, Boston…) : 120 lignes par minute, IOC 576
(1 810 points par ligne), modulation de fréquence ±400 Hz autour de la fréquence annoncée (noir en
dessous, blanc au-dessus : 1 500 / 2 300 Hz en USB avec la radio 1,9 kHz plus bas). Une carte commence
par le signal de départ (noir et blanc alternés à 300 Hz pendant 5 s), puis une trentaine de secondes
de phasage (une impulsion par ligne qui marque le bord gauche), et finit par le signal d'arrêt (450 Hz).

Le module extrait lui-même le canal de la station dans les IQ (NCO, décimation à 12 kHz, passe-bas à
1 kHz, discriminateur) : le mode du VFO n'a pas d'importance, il suffit que la station soit dans la bande
reçue (clé qui reçoit les ondes courtes, échantillonnage direct ou convertisseur). Le décodeur attend le
signal de départ, cale le bord gauche et la pente (petit écart d'horloge avec l'émetteur) sur les lignes
de phasage, trace la carte ligne par ligne et l'enregistre en PNG au signal d'arrêt.
Même algorithme dans SDR Web (web/fax-meteo.js).
"""

from __future__ import annotations

import queue
import threading
import time

import numpy as np

from sdrmod.dsp import NCO, Decimateur, plan_decimation
from sdrmod.modules import Module

TAUX_FAX = 12_000
ECART_HZ = 400.0                 # noir : −400 Hz, blanc : +400 Hz autour de la fréquence annoncée
LPM = 120
IOC = 576
LARGEUR = round(np.pi * IOC)     # 1 810 points par ligne (π × 576, arrondi)
LIGNE = TAUX_FAX * 60 // LPM     # 6 000 échantillons par ligne
LIGNES_MAX = 1600                # au-delà, la carte est close (13 min)
LIGNES_PHASAGE = 50
DEPART_HZ, ARRET_HZ = 300.0, 450.0
TONALITE_DB = 23                 # alternance de départ / d'arrêt au-dessus du reste du spectre
N_FFT = 16_384                   # analyse des tonalités : 1 s complétée de zéros (0,73 Hz par case)
DECALAGE_USB = 1900              # en USB, la radio se règle 1,9 kHz sous la fréquence annoncée

# Stations (120 lignes/min, IOC 576) : nom, fréquence annoncée en Hz — même liste que IC-7300 Scope.
STATIONS = [
    ("DWD Pinneberg (Allemagne)", 3_855_000), ("DWD Pinneberg (Allemagne)", 7_880_000),
    ("DWD Pinneberg (Allemagne)", 13_882_500),
    ("Northwood (Royaume-Uni)", 2_618_500), ("Northwood (Royaume-Uni)", 4_610_000),
    ("Northwood (Royaume-Uni)", 8_040_000), ("Northwood (Royaume-Uni)", 11_086_500),
    ("Athènes (Grèce)", 4_481_000), ("Athènes (Grèce)", 8_105_000),
    ("Boston NMF (États-Unis)", 4_235_000), ("Boston NMF (États-Unis)", 6_340_500),
    ("Boston NMF (États-Unis)", 9_110_000), ("Boston NMF (États-Unis)", 12_750_000),
]


def passe_bas(coupure: float, taux: float, n: int) -> np.ndarray:
    """Sinus cardinal fenêtré (Hamming), gain 1."""
    k = np.arange(n) - (n - 1) / 2
    h = np.sinc(2 * coupure / taux * k) * np.hamming(n)
    return h / h.sum()


class DecodeurFax:
    """Canal complexe à 12 kHz centré sur la fréquence annoncée → cartes (niveaux de gris uint8)."""

    def __init__(self) -> None:
        self.fir = passe_bas(1000, TAUX_FAX, 63)
        self.etat_fir = np.zeros(len(self.fir) - 1, complex)
        self.prec = None
        self.verrou = threading.Lock()
        self.inverse = False            # bande latérale inférieure : noir et blanc échangés
        self.terminees: list[tuple[np.ndarray, bool]] = []     # (carte, partielle)
        self.hann = np.hanning(TAUX_FAX)
        self.version = 0
        self.frequence = None           # dernière fréquence moyenne mesurée (Hz, par rapport à l'annoncée)
        self.reinitialiser()

    def reinitialiser(self) -> None:
        with self.verrou:
            self.valeurs = np.zeros(0, np.float32)  # 0 noir … 1 blanc
            self.verifie = 0
            self.n_depart = 0
            self.n_arret = 0
            self.en_cours = False
            self.lignes: list[np.ndarray] = []
            self.debut = 0                # échantillon du début de la 1re ligne
            self.ligne = float(LIGNE)     # durée réelle d'une ligne (pente)
            self.cale = False
            self.attente_fin_depart = False

    def statut(self) -> str:
        if self.attente_fin_depart:
            return "Signal de départ reçu…"
        if not self.en_cours:
            return "En attente d'une carte (signal de départ à 300 Hz)…"
        etat = "phasage" if not self.cale and len(self.lignes) < LIGNES_PHASAGE else (
            "calée" if self.cale else "sans phasage")
        return f"Carte en cours · ligne {len(self.lignes)} · {etat}"

    def demarrer_maintenant(self) -> None:
        """Commencer une carte tout de suite (signal de départ manqué)."""
        with self.verrou:
            self._commencer(len(self.valeurs), phasage=False)

    def terminer_maintenant(self) -> None:
        with self.verrou:
            if self.en_cours:
                self._terminer(partielle=True)

    def regler_bord_gauche(self, fraction: float) -> None:
        """Le point cliqué (fraction de la largeur) devient le bord gauche."""
        with self.verrou:
            if not self.en_cours:
                return
            self.debut += int(round(fraction * self.ligne)) % int(self.ligne)
            self.cale = True
            self._tout_retracer()

    @property
    def pente_ppm(self) -> float:
        return (self.ligne / LIGNE - 1) * 1e6

    def regler_pente(self, ppm: float) -> None:
        """Corrige la pente : +100 ppm allonge chaque ligne de 0,6 échantillon."""
        with self.verrou:
            self.ligne = LIGNE * (1 + ppm * 1e-6)
            if self.en_cours:
                self._tout_retracer()

    # ------------------------------------------------------------- démodulation : 0 (noir) à 1 (blanc)
    def traiter(self, z: np.ndarray) -> None:
        x = np.concatenate([self.etat_fir, z])
        y = np.convolve(x, self.fir, mode="valid")
        self.etat_fir = x[len(x) - len(self.etat_fir):]
        if not len(y):
            return
        y = np.concatenate([[y[0] if self.prec is None else self.prec], y])
        self.prec = y[-1]
        f = np.angle(y[1:] * np.conj(y[:-1])) * TAUX_FAX / (2 * np.pi)
        self.frequence = float(np.median(f))
        v = np.clip((f + ECART_HZ) / (2 * ECART_HZ), 0, 1)
        if self.inverse:
            v = 1 - v
        with self.verrou:
            self.valeurs = np.concatenate([self.valeurs, v.astype(np.float32)])
            self._tonalites()
            if self.en_cours:
                self._lignes()
            else:
                self._rogner()

    def _rogner(self) -> None:
        """Hors carte, on ne garde que les dernières secondes."""
        trop = len(self.valeurs) - 3 * TAUX_FAX
        if trop > 0:
            self.valeurs = self.valeurs[trop:]
            self.verifie = max(0, self.verifie - trop)

    # ------------------------------------------------------------- signaux de départ et d'arrêt
    def niveau_tonalite(self, fin: int, hz: float) -> float:
        """Force de l'alternance à `hz` dans la dernière seconde, au-dessus du reste (dB)."""
        part = self.valeurs[fin - TAUX_FAX:fin]
        spectre = np.abs(np.fft.rfft((part - part.mean()) * self.hann, N_FFT))
        par_hz = N_FFT / TAUX_FAX
        k = int(round(hz * par_hz))
        pic = spectre[k - 4:k + 5].max()
        # Une vraie alternance a son fondamental à `hz` : c'est la raie la plus forte de 20 à 1 500 Hz.
        # (Sinon une carte à motif régulier, des barres par exemple, a un harmonique qui tombe à 450 Hz.)
        if pic < spectre[int(20 * par_hz):int(1500 * par_hz)].max():
            return 0.0
        plancher = np.median(spectre[int(100 * par_hz):int(1500 * par_hz)]) + 1e-9
        return 20 * np.log10(pic / plancher + 1e-9)

    def _tonalites(self) -> None:
        pas = TAUX_FAX // 4
        while self.verifie + pas <= len(self.valeurs):
            self.verifie += pas
            fin = self.verifie
            if fin < TAUX_FAX:
                continue
            if not self.en_cours:
                if self.niveau_tonalite(fin, DEPART_HZ) > TONALITE_DB:
                    self.n_depart += 1
                    if self.n_depart >= 4:
                        self.attente_fin_depart = True
                elif self.attente_fin_depart:            # fin du départ : le phasage commence
                    self.attente_fin_depart = False
                    self.n_depart = 0
                    self._commencer(fin - pas, phasage=True)
                else:
                    self.n_depart = 0
            else:
                arret = self.niveau_tonalite(fin, ARRET_HZ) > TONALITE_DB
                self.n_arret = self.n_arret + 1 if arret else 0
                if self.n_arret >= 6:                  # la carte s'arrête avant l'alternance de fin
                    self._terminer(lignes_en_trop=round(2.5 * LPM / 60))
                    return

    # ------------------------------------------------------------- carte
    def _commencer(self, debut: int, phasage: bool) -> None:
        self.en_cours = True
        self.lignes = []
        self.debut = debut
        self.ligne = float(LIGNE)
        self.cale = not phasage           # sans phasage : on garde ce départ
        self.n_arret = 0
        self.version += 1

    def _rangee(self, k: int) -> np.ndarray:
        a = self.debut + k * self.ligne
        idx = a + np.arange(LARGEUR) * (self.ligne / LARGEUR)
        idx = np.clip(np.round(idx).astype(int), 0, len(self.valeurs) - 1)
        return (self.valeurs[idx] * 255).astype(np.uint8)

    def _tout_retracer(self) -> None:
        n = len(self.lignes)
        self.lignes = [self._rangee(k) for k in range(n) if self.debut + (k + 1) * self.ligne <= len(self.valeurs)]
        self.version += 1

    def _lignes(self) -> None:
        while self.debut + (len(self.lignes) + 1) * self.ligne <= len(self.valeurs):
            self.lignes.append(self._rangee(len(self.lignes)))
            self.version += 1
            if not self.cale and len(self.lignes) == LIGNES_PHASAGE:
                self._phasage()
            if len(self.lignes) >= LIGNES_MAX:
                self._terminer()
                return

    def _phasage(self) -> None:
        """Bord gauche et pente d'après l'impulsion de phasage de chaque ligne."""
        ligne = int(LIGNE)
        lignes = np.array([self.valeurs[self.debut + k * ligne:self.debut + (k + 1) * ligne]
                           for k in range(4, LIGNES_PHASAGE)])
        profil = lignes.mean(axis=0)
        large = ligne // 20               # l'impulsion fait 5 % de la ligne
        noyau = np.ones(large) / large
        boucler = lambda p: np.concatenate([p, p[:large]])
        lisse = np.convolve(boucler(profil), noyau, "valid")[:ligne]
        ecart = lisse - np.median(profil)
        signe = 1 if ecart.max() > -ecart.min() else -1
        contraste = signe * ecart
        centre = int(np.argmax(contraste))
        if contraste[centre] < 0.35:      # pas d'impulsion nette : pas de phasage
            self.cale = True
            return
        positions = []                    # position de l'impulsion ligne par ligne → pente
        for rangee in lignes:
            s = np.convolve(boucler(signe * (rangee - np.median(rangee))), noyau, "valid")[:ligne]
            pres = (np.arange(ligne) - centre + ligne // 2) % ligne - ligne // 2
            fenetre = np.abs(pres) < ligne // 10
            positions.append(centre + pres[fenetre][np.argmax(s[fenetre])])
        k = np.arange(len(positions))
        pente, premiere = np.polyfit(k, positions, 1)
        if abs(pente) > LIGNE * 0.002:    # pente invraisemblable (plus de 2 000 ppm)
            pente, premiere = 0.0, float(np.median(positions))
        self.ligne = LIGNE + pente
        # le début de l'impulsion devient le bord gauche ; `premiere` est sa position dans la 5e ligne
        self.debut += int(round(premiere - 4 * pente)) % ligne
        self.cale = True
        self._tout_retracer()

    def _terminer(self, lignes_en_trop: int = 0, partielle: bool = False) -> None:
        rangees = self.lignes[:len(self.lignes) - lignes_en_trop] if lignes_en_trop else self.lignes
        if len(rangees) > LIGNES_PHASAGE or (partielle and rangees):
            self.terminees.append((np.array(rangees), partielle))
        self.en_cours = False
        self.lignes = []
        self.valeurs = self.valeurs[-TAUX_FAX:]
        self.verifie = len(self.valeurs)
        self.n_arret = 0
        self.version += 1

    def image(self) -> np.ndarray | None:
        with self.verrou:
            return np.array(self.lignes) if self.lignes else None


# ======================================================================= module
try:
    from PySide6.QtCore import QRectF, Qt, QTimer, QUrl
    from PySide6.QtGui import QDesktopServices, QImage, QPainter
    from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel,
                                   QPushButton, QSizePolicy, QVBoxLayout, QWidget)
except ImportError:                                      # décodeur seul (essais sans Qt)
    QWidget = object


class VueCarte(QWidget):
    """Aperçu de la carte (proportions gardées) ; un clic place le bord gauche."""

    def __init__(self, sur_clic) -> None:
        super().__init__()
        self.qimage = None
        self.sur_clic = sur_clic
        self.setMinimumSize(260, 200)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.setToolTip("Cliquez sur le bord gauche réel de la carte pour la recaler")
        self.cadre = QRectF()

    def montrer(self, qimage) -> None:
        self.qimage = qimage
        self.update()

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.GlobalColor.black)
        if self.qimage is not None and self.qimage.height():
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            # toute la largeur ; en hauteur, les dernières lignes si la carte est plus haute que la vue
            echelle = self.width() / self.qimage.width()
            h = min(self.height(), self.qimage.height() * echelle)
            src_h = h / echelle
            src = QRectF(0, self.qimage.height() - src_h, self.qimage.width(), src_h)
            self.cadre = QRectF(0, 0, self.width(), h)
            p.drawImage(self.cadre, self.qimage, src)
        p.end()

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton and self.cadre.width():
            self.sur_clic(ev.position().x() / self.cadre.width())


class FaxMeteoModule(Module):
    nom = "Fax météo (WEFAX)"
    description = ("Cartes météo en ondes courtes (DWD Pinneberg, Northwood, Athènes, Boston…) : 120 lignes/min, "
                   "IOC 576, calage automatique sur le phasage, PNG enregistrés. Station choisie dans une liste.")

    def demarrer(self) -> None:
        self.decodeur = DecodeurFax()
        self.dossier = self.ctx.dossier_donnees("fax_meteo")
        self.choix = 1                                   # DWD 7 880 kHz
        self.vfo = self.ctx.frequence()
        self.blocs: queue.Queue = queue.Queue(maxsize=32)
        self.etat = {"instant": 0.0, "couvert": False, "perdus": 0, "niveau": None, "bruit": None, "calcul": 0.0}
        self._actif = True
        self._fil = threading.Thread(target=self._boucle, name="fax-meteo", daemon=True)
        self._fil.start()
        self.abonner("iq", self._sur_iq)
        self.abonner("vfo", self._sur_vfo)

    def frequence(self) -> float:
        """Fréquence annoncée décodée : celle de la station, ou VFO + 1,9 kHz (USB)."""
        return STATIONS[self.choix][1] if self.choix < len(STATIONS) else self.vfo + DECALAGE_USB

    def _sur_vfo(self, f: float, _mode: str) -> None:
        self.vfo = f

    def _sur_iq(self, iq: np.ndarray, centre: float, taux: int) -> None:
        try:
            self.blocs.put_nowait((iq, centre, taux))
        except queue.Full:
            self.etat["perdus"] += 1

    def _boucle(self) -> None:
        bande = None
        while self._actif:
            try:
                tache = self.blocs.get(timeout=0.5)
            except queue.Empty:
                continue
            if tache is None:
                break
            iq, centre, taux = tache
            debut = time.perf_counter()
            f = self.frequence()
            couvert = abs(f - centre) + 3_000 < 0.42 * taux
            self.etat.update(instant=time.monotonic(), couvert=couvert)
            if not couvert:
                bande = None
                continue
            if bande is None or bande[:2] != (centre, taux):
                d1, d2 = plan_decimation(taux, 48_000)
                inter = taux / d1
                nco = NCO(taux)
                dec1, dec2 = Decimateur(d1, taux, 0.42 * inter), Decimateur(d2, inter, 6_000)
                dec3 = Decimateur(4, 48_000, 2_500)
            nco.freq = f - centre
            bande = (centre, taux)
            z = dec3.traiter(dec2.traiter(dec1.traiter(nco.melanger(iq))))
            if not len(z):
                continue
            p = 10 * np.log10(float(np.mean(np.abs(z) ** 2)) + 1e-20)
            e = self.etat
            e["bruit"] = p if e["bruit"] is None else min(p, e["bruit"] + 0.02)
            e["niveau"] = p if e["niveau"] is None else 0.9 * e["niveau"] + 0.1 * p
            self.decodeur.traiter(z.astype(np.complex128))
            e["calcul"] = 0.9 * e["calcul"] + 0.1 * (time.perf_counter() - debut) / (len(iq) / taux)

    # ------------------------------------------------------------- interface
    def creer_widget(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(4)
        ligne = QHBoxLayout()
        self.c_station = QComboBox()
        for nom, f in STATIONS:
            self.c_station.addItem(f"{nom} · {f / 1000:g} kHz".replace(".", ","))
        self.c_station.addItem("Fréquence du VFO (USB, +1,9 kHz)")
        self.c_station.setCurrentIndex(self.choix)
        self.c_station.setToolTip("Station décodée : le module extrait son canal des IQ, quel que soit le mode du VFO")
        self.c_station.currentIndexChanged.connect(self._changer_station)
        accorder = QPushButton("Accorder")
        accorder.setToolTip("VFO en USB 1,9 kHz sous la fréquence annoncée, pour écouter la station")
        accorder.clicked.connect(self.accorder)
        ligne.addWidget(self.c_station, 1)
        ligne.addWidget(accorder)
        lay.addLayout(ligne)
        self.vue = VueCarte(self.decodeur.regler_bord_gauche)
        lay.addWidget(self.vue, 1)
        self.statut = QLabel()
        self.statut.setWordWrap(True)
        lay.addWidget(self.statut)
        self.diag = QLabel()
        self.diag.setWordWrap(True)
        self.diag.setStyleSheet("color: gray")
        self.diag.setToolTip("Niveau du canal au-dessus du bruit, fréquence moyenne mesurée (0 = fréquence annoncée ; "
                             "le blanc est à +400 Hz, le noir à −400 Hz), part du temps réel utilisée")
        lay.addWidget(self.diag)
        ligne = QHBoxLayout()
        self.b_depart = QPushButton("Démarrer")
        self.b_depart.setToolTip("Commence une carte maintenant (signal de départ manqué) ; cliquez ensuite sur son bord gauche")
        self.b_depart.clicked.connect(self.decodeur.demarrer_maintenant)
        self.b_fin = QPushButton("Terminer")
        self.b_fin.setToolTip("Clôt la carte en cours et l'enregistre")
        self.b_fin.clicked.connect(self.decodeur.terminer_maintenant)
        self.pente = QDoubleSpinBox()
        self.pente.setRange(-2000, 2000)
        self.pente.setSingleStep(10)
        self.pente.setDecimals(0)
        self.pente.setSuffix(" ppm")
        self.pente.setToolTip("Pente (écart d'horloge avec l'émetteur), réglée au phasage ; à corriger à la main si la carte penche")
        self.pente.editingFinished.connect(lambda: self.decodeur.regler_pente(self.pente.value()))
        self.c_inverse = QCheckBox("Inverser")
        self.c_inverse.setToolTip("Échange noir et blanc (station reçue en LSB)")
        self.c_inverse.toggled.connect(lambda v: setattr(self.decodeur, "inverse", v))
        for b in (self.b_depart, self.b_fin, QLabel("Pente"), self.pente, self.c_inverse):
            ligne.addWidget(b)
        lay.addLayout(ligne)
        ligne = QHBoxLayout()
        self.c_auto = QCheckBox("Enregistrer chaque carte")
        self.c_auto.setChecked(True)
        sauver = QPushButton("Enregistrer…")
        sauver.clicked.connect(self._enregistrer_sous)
        dossier = QPushButton("Dossier")
        dossier.setToolTip(str(self.dossier))
        dossier.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.dossier))))
        ligne.addWidget(self.c_auto, 1)
        ligne.addWidget(sauver)
        ligne.addWidget(dossier)
        lay.addLayout(ligne)
        aide = QLabel("Ondes courtes : clé qui reçoit la HF (RTL-SDR Blog V4, échantillonnage direct) ou convertisseur. "
                      "Horaires des cartes : programme de chaque station (DWD : dwd.de, « Seefunk »).")
        aide.setWordWrap(True)
        aide.setStyleSheet("color: gray")
        lay.addWidget(aide)
        self.version_vue = -1
        self.minuterie = QTimer(w, interval=500, timeout=self._rafraichir)
        self.minuterie.start()
        self._rafraichir()
        return w

    def _changer_station(self, i: int) -> None:
        self.choix = i
        self.decodeur.reinitialiser()

    def accorder(self) -> None:
        f = self.frequence()
        if self.choix >= len(STATIONS):
            return
        taux = self.ctx.taux()
        self.ctx.regler_mode("USB")
        self.ctx.regler_frequence(f - DECALAGE_USB + taux)       # force le recentrage de la clé…
        self.ctx.regler_frequence(f - DECALAGE_USB)              # … VFO 1,9 kHz sous la fréquence annoncée

    @staticmethod
    def _qimage(gris: np.ndarray):
        h, l = gris.shape
        return QImage(np.ascontiguousarray(gris).data, l, h, l, QImage.Format.Format_Grayscale8).copy()

    def _enregistrer(self, gris: np.ndarray, chemin, partielle: bool = False) -> None:
        self._qimage(gris).save(str(chemin), "PNG")

    def _nom(self, partielle: bool = False) -> str:
        f = self.frequence()
        return time.strftime("fax_%Y%m%d_%H%M%S") + f"_{f / 1000:g}kHz".replace(".", ",") + (
            "_partielle" if partielle else "") + ".png"

    def _rafraichir(self) -> None:
        d = self.decodeur
        if d.version != self.version_vue:
            self.version_vue = d.version
            img = d.image()
            self.vue.montrer(self._qimage(img) if img is not None else None)
        with d.verrou:
            terminees, d.terminees = d.terminees, []
        for gris, partielle in terminees:
            if self.c_auto.isChecked():
                self._enregistrer(gris, self.dossier / self._nom(partielle))
            self.vue.montrer(self._qimage(gris))
        e = self.etat
        if time.monotonic() - e["instant"] > 1.5:
            self.statut.setText("En attente des échantillons du récepteur.")
        elif not e["couvert"]:
            self.statut.setText(f"{self.frequence() / 1000:g} kHz n'est pas dans la bande reçue : cliquez sur « Accorder » "
                                "(clé qui reçoit la HF ou convertisseur).".replace(".", ","))
        else:
            self.statut.setText(d.statut())
        if not self.pente.hasFocus():
            self.pente.setValue(round(d.pente_ppm))
        self.b_fin.setEnabled(d.en_cours)
        if e["niveau"] is not None and e["couvert"]:
            fr = f" · fréquence {d.frequence:+.0f} Hz" if d.frequence is not None else ""
            perdus = f" · {e['perdus']} blocs perdus" if e["perdus"] else ""
            self.diag.setText(f"niveau {e['niveau'] - e['bruit']:.0f} dB{fr} · calcul {100 * e['calcul']:.0f} %{perdus}")

    def _enregistrer_sous(self) -> None:
        img = self.decodeur.image()
        if img is None:
            return
        chemin, _ = QFileDialog.getSaveFileName(self.vue, "Enregistrer la carte", str(self.dossier / self._nom()),
                                                "Images PNG (*.png)")
        if chemin:
            self._enregistrer(img, chemin)

    def arreter(self) -> None:
        self._actif = False
        try:
            self.blocs.put_nowait(None)
        except queue.Full:
            pass
        if hasattr(self, "minuterie"):
            self.minuterie.stop()
