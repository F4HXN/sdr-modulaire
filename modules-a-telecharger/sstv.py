# SPDX-License-Identifier: MIT
"""Décodeur SSTV (télévision à balayage lent) pour SDR Modulaire.

Décode les images reçues dans l'audio du récepteur : ISS sur 145,800 MHz en NFM (PD120),
ondes courtes en USB (14,230 / 21,340 / 28,680 MHz) ou en LSB (7,165 / 3,735 MHz). Une liste de
fréquences préréglées accorde le récepteur dans le bon mode ; « Agrandir » montre l'image en grand. Le mode est reconnu par son code VIS ;
on peut aussi forcer un mode et démarrer à la main si l'en-tête a été manqué.

Modes : Martin M1, M2 ; Scottie S1, S2, DX ; Robot 36, 72 ; PD50, PD90, PD120, PD160, PD180, PD240, PD290.

Principe : l'audio est ramené autour de 1 900 Hz, la fréquence instantanée en est tirée
(1 500 Hz = noir, 2 300 Hz = blanc, 1 200 Hz = synchro), puis chaque ligne est calée sur
son top de synchro : une droite ajustée sur les tops suit l'écart d'horloge de la clé,
ce qui évite l'image penchée. Les images reçues sont enregistrées en PNG dans
~/.config/sdr-modulaire/donnees/sstv/.

Module à télécharger : placez ce fichier dans ~/.config/sdr-modulaire/modules/.
Aucune dépendance en plus de SDR Modulaire.
"""

import threading
import time
from dataclasses import dataclass, field
from queue import Empty, Full, Queue

import numpy as np
from scipy import signal

from sdrmod.modules import Module

# Fréquences préréglées : (fréquence en Hz, mode du récepteur, libellé). Ondes courtes reprises d'IC-7300 Scope.
FREQUENCES = [
    (145_800_000, "NFM", "ISS (ARISS), PD120"),
    (144_500_000, "NFM", "2 m, appel SSTV (IARU Région 1)"),
    (50_510_000, "USB", "6 m"),
    (28_680_000, "USB", "10 m"),
    (21_340_000, "USB", "15 m"),
    (14_230_000, "USB", "20 m, la plus active"),
    (14_233_000, "USB", "20 m"),
    (7_165_000, "LSB", "40 m"),
    (7_171_000, "LSB", "40 m"),
    (3_735_000, "LSB", "80 m"),
]


def libelle_frequence(f: int, mode: str, nom: str) -> str:
    return f"{f / 1e6:.3f} MHz {mode} · {nom}".replace(".", ",", 1)


TAUX = 12_000                 # fréquence instantanée calculée à 48 kHz puis moyennée par 4
CENTRE = 1900.0               # milieu de la bande SSTV, ramené à 0 Hz avant le calcul


def ms(x: float) -> float:
    return x * TAUX / 1000


# ----------------------------------------------------------------------------- modes
@dataclass
class Mode:
    nom: str
    vis: int
    largeur: int
    hauteur: int
    ligne: float                  # durée d'une ligne transmise (ms)
    synchro: float                # durée du top de synchro (ms)
    pos_synchro: float            # début du top dans la ligne (ms)
    couleur: str                  # "RVB", "R36", "R72" ou "PD"
    balayages: list = field(default_factory=list)   # (canal, début ms, durée ms, pixels)
    debut: float = 0.0            # avance de la première ligne après le VIS (ms)
    palier: float = 1.5           # palier à 1 500 Hz juste après le top (ms)

    @property
    def lignes(self) -> int:      # lignes transmises (une ligne PD porte deux lignes d'image)
        return self.hauteur // 2 if self.couleur == "PD" else self.hauteur


def _martin(nom, vis, b):
    s, p = 4.862, 0.572
    g = s + p
    return Mode(nom, vis, 320, 256, g + 3 * (b + p), s, 0.0, "RVB",
                [("V", g, b, 320), ("B", g + b + p, b, 320), ("R", g + 2 * (b + p), b, 320)], palier=p)


def _scottie(nom, vis, b):
    p, s = 1.5, 9.0
    return Mode(nom, vis, 320, 256, 3 * b + 3 * p + s, s, p + 2 * b + p, "RVB",
                [("V", p, b, 320), ("B", 2 * p + b, b, 320), ("R", 3 * p + 2 * b + s, b, 320)], debut=s)


def _pd(nom, vis, largeur, hauteur, pixel):
    b, d = largeur * pixel, 20.0 + 2.08
    return Mode(nom, vis, largeur, hauteur, d + 4 * b, 20.0, 0.0, "PD",
                [("Y1", d, b, largeur), ("Cr", d + b, b, largeur), ("Cb", d + 2 * b, b, largeur),
                 ("Y2", d + 3 * b, b, largeur)], palier=2.08)


MODES = {m.vis: m for m in [
    _martin("Martin M1", 44, 146.432),
    _martin("Martin M2", 40, 73.216),
    _scottie("Scottie S1", 60, 138.24),
    _scottie("Scottie S2", 56, 88.064),
    _scottie("Scottie DX", 76, 345.6),
    Mode("Robot 36", 8, 320, 240, 150.0, 9.0, 0.0, "R36",
         [("Y", 12.0, 88.0, 320), ("sep", 100.0, 4.5, 1), ("C", 106.0, 44.0, 160)], palier=3.0),
    Mode("Robot 72", 12, 320, 240, 300.0, 9.0, 0.0, "R72",
         [("Y", 12.0, 138.0, 320), ("Cr", 156.0, 69.0, 160), ("Cb", 231.0, 69.0, 160)], palier=3.0),
    _pd("PD50", 93, 320, 256, 0.286),
    _pd("PD90", 99, 320, 256, 0.532),
    _pd("PD120", 95, 640, 496, 0.19),
    _pd("PD160", 98, 512, 400, 0.382),
    _pd("PD180", 96, 640, 496, 0.286),
    _pd("PD240", 97, 640, 496, 0.382),
    _pd("PD290", 94, 800, 616, 0.286),
]}
PAR_NOM = {m.nom: m for m in MODES.values()}


def vers_rvb(y, cb, cr) -> np.ndarray:
    """YCbCr (0–255) → RVB 8 bits, une ligne."""
    y, cb, cr = (np.asarray(v, dtype=np.float32) for v in (y, cb, cr))
    r = y + 1.402 * (cr - 128)
    g = y - 0.344136 * (cb - 128) - 0.714136 * (cr - 128)
    b = y + 1.772 * (cb - 128)
    return np.clip(np.stack([r, g, b], axis=-1), 0, 255).astype(np.uint8)


# ----------------------------------------------------------------------------- tampon
class Tampon:
    """Fréquence instantanée à 12 kHz, repérée par un indice absolu d'échantillon."""

    def __init__(self) -> None:
        self.donnees = np.zeros(1 << 16, np.float32)
        self.n = 0
        self.base = 0                      # indice absolu de donnees[0]

    @property
    def fin(self) -> int:
        return self.base + self.n

    def ajouter(self, x: np.ndarray) -> None:
        if self.n + len(x) > len(self.donnees):
            plus = np.zeros(max(len(self.donnees) * 2, self.n + len(x)), np.float32)
            plus[:self.n] = self.donnees[:self.n]
            self.donnees = plus
        self.donnees[self.n:self.n + len(x)] = x
        self.n += len(x)

    def morceau(self, a: int, b: int) -> np.ndarray:
        return self.donnees[a - self.base:b - self.base]

    def garder_depuis(self, a: int) -> None:
        a = max(a, self.base)
        k = a - self.base
        if k > 0:
            self.donnees[:self.n - k] = self.donnees[k:self.n]
            self.n -= k
            self.base = a


def moyennes(seg: np.ndarray, bords: np.ndarray) -> np.ndarray:
    """Moyenne de seg entre des bords fractionnaires successifs (indices dans seg)."""
    cumul = np.concatenate([[0.0], np.cumsum(seg, dtype=np.float64)])
    c = np.interp(bords, np.arange(len(cumul)), cumul)
    return np.diff(c) / np.maximum(np.diff(bords), 1e-9)


# ----------------------------------------------------------------------------- décodeur
class DecodeurSSTV:
    """Audio 48 kHz → images. Pur numpy : appelé dans le fil de traitement du module."""

    def __init__(self, taux_audio: int = 48_000) -> None:
        self.taux_audio = taux_audio
        self.facteur = max(1, round(taux_audio / TAUX))
        self.h = signal.firwin(63, 1100, fs=taux_audio).astype(np.float64)
        self.zi = np.zeros(len(self.h) - 1, complex)
        self.phase = 0.0
        self.dernier = 1 + 0j
        self.reste = np.zeros(0, np.float32)
        self.tampon = Tampon()
        self.verrou = threading.Lock()
        self.image: np.ndarray | None = None
        self.mode: Mode | None = None
        self.etat = "veille"
        self.message = "En attente d'un signal SSTV (en-tête VIS)…"
        self.version = 0                    # change à chaque ligne décodée
        self.terminees: list = []           # (image, nom du mode, date) prêtes à enregistrer
        self.tonalite = 0.0
        self.vis_depuis = 0

    # --------------------------------------------------------------- démodulation FM
    def frequences(self, audio: np.ndarray) -> np.ndarray:
        n = len(audio)
        w = 2 * np.pi * CENTRE / self.taux_audio
        ph = self.phase + w * np.arange(n)
        self.phase = float((self.phase + w * n) % (2 * np.pi))
        z, self.zi = signal.lfilter(self.h, 1.0, audio.astype(np.float64) * np.exp(-1j * ph), zi=self.zi)
        zz = np.concatenate([[self.dernier], z])
        self.dernier = z[-1] if n else self.dernier
        f = CENTRE + np.angle(zz[1:] * np.conj(zz[:-1])) * self.taux_audio / (2 * np.pi)
        f = np.concatenate([self.reste, np.clip(f, 900, 2700).astype(np.float32)])
        k = len(f) // self.facteur * self.facteur
        self.reste = f[k:]
        return f[:k].reshape(-1, self.facteur).mean(axis=1)

    def traiter(self, audio: np.ndarray) -> None:
        f = self.frequences(audio)
        if not len(f):
            return
        self.tampon.ajouter(f)
        self.tonalite = float(np.median(f))
        if self.etat == "veille":
            self._chercher_vis()
        else:
            self._decoder_lignes()

    # --------------------------------------------------------------- en-tête VIS
    def _chercher_vis(self) -> None:
        t = self.tampon
        premier = max(t.base + int(ms(260)), self.vis_depuis)
        dernier = t.fin - int(ms(300)) - 1             # le VIS entier doit être reçu
        if dernier - premier < 1:
            t.garder_depuis(t.fin - int(ms(1500)))
            return
        a = premier - int(ms(260))
        seg = t.morceau(a, t.fin)
        cumul = np.concatenate([[0.0], np.cumsum(seg, dtype=np.float64)])
        s = np.arange(premier, dernier) - a

        def moy(d, f):
            i, j = (s + int(ms(d))), (s + int(ms(f)))
            return (cumul[j] - cumul[i]) / (j - i)

        meneur, depart, arret = moy(-250, -10), moy(3, 27), moy(273, 297)
        bits = np.stack([moy(30 + 30 * k + 4, 60 + 30 * k - 4) for k in range(8)])
        valide = ((np.abs(meneur - 1900) < 100) & (np.abs(depart - 1200) < 100) & (np.abs(arret - 1200) < 100)
                  & np.all((bits > 1000) & (bits < 1400), axis=0))
        self.vis_depuis = dernier
        if not valide.any():
            t.garder_depuis(t.fin - int(ms(1500)))
            return
        i0 = int(np.argmax(valide))
        if i0 + int(ms(15)) >= len(s):                 # la grappe de positions valides n'est pas complète
            self.vis_depuis = premier + i0
            return
        fenetre = slice(i0, i0 + int(ms(15)))
        ecart = (np.abs(depart - 1200) + np.abs(arret - 1200)
                 + np.minimum(np.abs(bits - 1100), np.abs(bits - 1300)).sum(axis=0))
        ecart = np.where(valide, ecart, np.inf)
        meilleur = i0 + int(np.argmin(ecart[fenetre]))
        b = (bits[:, meilleur] < 1200).astype(int)     # 1 100 Hz = 1, 1 300 Hz = 0, poids faible d'abord
        code = int(sum(int(v) << k for k, v in enumerate(b[:7])))
        fin_vis = premier + meilleur + int(round(ms(300)))
        self.vis_depuis = fin_vis
        if b.sum() % 2:
            self.message = f"En-tête VIS reçu, mais parité fausse (code {code}) : ignoré."
        elif code not in MODES:
            self.message = f"Mode SSTV non pris en charge (code VIS {code})."
        else:
            self.demarrer(MODES[code], fin_vis)

    # --------------------------------------------------------------- image
    def demarrer(self, mode: Mode, debut: int | None = None) -> None:
        """Début d'image : après un VIS (debut connu) ou à la main (synchro cherchée sur une ligne)."""
        self.mode = mode
        self.manuel = debut is None
        debut = self.tampon.fin if debut is None else debut
        self.depart = debut + ms(mode.debut)          # début de la première ligne
        self.points: list[tuple[int, float]] = []      # (ligne, position du top)
        self.ligne = 0
        self.manques = 0
        self.cb = np.full(mode.largeur, 128.0)
        self.cr = np.full(mode.largeur, 128.0)
        self.y_prec = None
        with self.verrou:
            self.image = np.zeros((mode.hauteur, mode.largeur, 3), np.uint8)
            self.version += 1
        self.etat = "image"
        self.message = f"{mode.nom} : réception…"

    def arreter(self, raison: str = "") -> None:
        if self.etat == "image" and self.mode and self.ligne > self.mode.lignes // 3:
            self._terminer(partielle=True)
        self.etat = "veille"
        self.vis_depuis = self.tampon.fin
        self.message = raison or "En attente d'un signal SSTV (en-tête VIS)…"

    def _terminer(self, partielle: bool = False) -> None:
        with self.verrou:
            if self.image is not None:
                self.terminees.append((self.image.copy(), self.mode.nom, time.localtime(), partielle))

    def _prevision(self, ligne: int) -> float:
        """Position attendue du top de la ligne : droite ajustée sur les tops trouvés."""
        m = self.mode
        pente = ms(m.ligne)
        if not self.points:
            return self.depart + ms(m.pos_synchro) + ligne * pente
        l = np.array([p[0] for p in self.points], float)
        x = np.array([p[1] for p in self.points], float)
        if len(l) >= 6 and l[-1] > l[0]:
            p, o = np.polyfit(l, x, 1)
            if abs(p / pente - 1) < 0.003:            # écart d'horloge plausible (< 3 000 ppm)
                residus = np.abs(x - (o + p * l))
                bons = residus < max(np.median(residus) * 3, ms(0.5))
                if bons.sum() >= 4:
                    p, o = np.polyfit(l[bons], x[bons], 1)
                    if abs(p / pente - 1) < 0.003:
                        return o + p * ligne
        return float(np.median(x - pente * l)) + ligne * pente

    def _decoder_lignes(self) -> None:
        m, t = self.mode, self.tampon
        dl, ds = ms(m.ligne), ms(m.synchro)
        while self.etat == "image":
            if self.ligne >= m.lignes:
                self._terminer()
                self.etat = "fini"
                self.arreter(f"Image {m.nom} reçue.")
                return
            large = self.manuel and not self.points
            demi = dl / 2 if large else max(ms(3), ds * 0.6)
            prevu = self._prevision(self.ligne) + (dl / 2 if large else 0)
            debut_ligne = prevu - ms(m.pos_synchro)
            if t.fin < max(prevu + demi + ds, debut_ligne + dl + demi) + ms(2):
                return                                   # ligne pas encore reçue en entier
            # Top cherché par gabarit : 1 200 Hz pendant le top puis 1 500 Hz pendant le palier.
            # Le palier compte : le bit d'arrêt du VIS, à 1 200 Hz, touche le premier top, et
            # des pixels clairs après le palier ne doivent pas tirer le top vers la droite.
            a = int(prevu - demi)
            n, p = int(round(ds)), max(3, int(round(ms(m.palier))))
            seg = t.morceau(a, int(prevu + demi) + n + p + 1)
            c1 = np.concatenate([[0.0], np.cumsum(np.abs(seg - 1200.0), dtype=np.float64)])
            c2 = np.concatenate([[0.0], np.cumsum(np.abs(seg - 1500.0), dtype=np.float64)])
            debuts = np.arange(len(seg) - n - p + 1)
            ecart = (c1[debuts + n] - c1[debuts]) / n + (c2[debuts + n + p] - c2[debuts + n]) / p
            k = int(np.argmin(ecart))
            if ecart[k] < 220:
                self.points.append((self.ligne, a + k))
                self.points = self.points[-400:]
                self.manques = 0
            else:
                self.manques += 1
            if self.manques * m.ligne > 10_000:
                self.arreter(f"{m.nom} : signal perdu à la ligne {self.ligne}.")
                return
            self._ligne(self.ligne, self._prevision(self.ligne) - ms(m.pos_synchro))
            self.ligne += 1
            t.garder_depuis(int(self._prevision(self.ligne) - ms(m.pos_synchro) - dl))
            self.message = f"{m.nom} : ligne {self.ligne}/{m.lignes} ({100 * self.ligne // m.lignes} %)"

    def _ligne(self, ligne: int, debut: float) -> None:
        m, t = self.mode, self.tampon
        canaux = {}
        for nom, d, duree, px in m.balayages:
            bords = debut + ms(d) + ms(duree) * np.arange(px + 1) / px
            a = int(np.floor(bords[0]))
            seg = t.morceau(a, int(np.ceil(bords[-1])) + 2)
            f = moyennes(seg, bords - a)
            canaux[nom] = f if nom == "sep" else np.clip((f - 1500) / 800 * 255, 0, 255)
        etire = lambda c: np.interp(np.linspace(0, len(c) - 1, m.largeur), np.arange(len(c)), c)
        with self.verrou:
            img = self.image
            if m.couleur == "RVB":
                img[ligne] = np.stack([canaux["R"], canaux["V"], canaux["B"]], axis=-1).astype(np.uint8)
            elif m.couleur == "PD":
                img[2 * ligne] = vers_rvb(canaux["Y1"], canaux["Cb"], canaux["Cr"])
                img[2 * ligne + 1] = vers_rvb(canaux["Y2"], canaux["Cb"], canaux["Cr"])
            elif m.couleur == "R72":
                img[ligne] = vers_rvb(canaux["Y"], etire(canaux["Cb"]), etire(canaux["Cr"]))
            else:                                        # Robot 36 : Cr et Cb une ligne sur deux
                paire = canaux["sep"][0] > 1900 if abs(canaux["sep"][0] - 1900) > 200 else ligne % 2 == 1
                if paire:
                    self.cb = etire(canaux["C"])
                else:
                    self.cr = etire(canaux["C"])
                img[ligne] = vers_rvb(canaux["Y"], self.cb, self.cr)
                if paire and self.y_prec is not None and ligne > 0:
                    img[ligne - 1] = vers_rvb(self.y_prec, self.cb, self.cr)
                self.y_prec = canaux["Y"]
            self.version += 1


# ----------------------------------------------------------------------------- module
try:
    from PySide6.QtCore import QRectF, Qt, QTimer, QUrl
    from PySide6.QtGui import QDesktopServices, QImage, QPainter
    from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QPushButton,
                                   QSizePolicy, QVBoxLayout, QWidget)
except ImportError:                                      # décodeur seul (essais sans Qt)
    QWidget = object


class VueImage(QWidget):
    """Affiche un aperçu compact de l’image, proportions gardées."""

    HAUTEUR_MAX = 180

    def __init__(self) -> None:
        super().__init__()
        self.qimage = None
        self.rapport = 256 / 320
        self.setMinimumWidth(240)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._ajuster()

    def montrer(self, qimage) -> None:
        self.qimage = qimage
        rapport = qimage.height() / qimage.width()
        if rapport != self.rapport:
            self.rapport = rapport
            self._ajuster()
        self.update()

    def _ajuster(self) -> None:                         # les docks ignorent heightForWidth
        hauteur = round(max(self.width(), self.minimumWidth()) * self.rapport)
        self.setFixedHeight(min(hauteur, self.HAUTEUR_MAX))

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self._ajuster()

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.GlobalColor.black)
        if self.qimage is not None:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            largeur = min(self.width(), self.height() / self.rapport)
            hauteur = largeur * self.rapport
            cible = QRectF((self.width() - largeur) / 2, (self.height() - hauteur) / 2,
                           largeur, hauteur)
            p.drawImage(cible, self.qimage)
        p.end()


class FenetreSSTV(QWidget):
    """Image en grand, dans une fenêtre à part (redimensionnable, proportions gardées)."""

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.setWindowTitle("Décodeur SSTV")
        self.resize(760, 660)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.vue = VueImage()
        self.vue.HAUTEUR_MAX = 10_000
        self.vue.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.vue._ajuster = lambda: None                   # la fenêtre décide de la taille
        self.vue.setMinimumSize(320, 256)
        self.vue.setMaximumHeight(16_777_215)
        lay.addWidget(self.vue, 1)
        self.statut = QLabel()
        self.statut.setWordWrap(True)
        lay.addWidget(self.statut)


class SSTV(Module):
    nom = "Décodeur SSTV"
    description = "Images SSTV (ISS en PD120, Martin, Scottie, Robot, PD) décodées depuis l'audio, enregistrées en PNG."

    def demarrer(self) -> None:
        self.decodeur = DecodeurSSTV()
        self.file: Queue = Queue(maxsize=200)            # ~10 s d'audio
        self.ordres: Queue = Queue()
        self.fin = threading.Event()
        self.perdus = 0
        self.dossier = self.ctx.dossier_donnees("sstv")
        self.fil = threading.Thread(target=self._travail, name="SSTV", daemon=True)
        self.fil.start()
        self.abonner("audio", self._sur_audio)

    def _sur_audio(self, echantillons, taux) -> None:   # fil DSP : on passe le bloc et on rend la main
        try:
            self.file.put_nowait(echantillons.copy())
        except Full:
            self.perdus += 1

    def _travail(self) -> None:
        d = self.decodeur
        while not self.fin.is_set():
            try:
                while True:
                    ordre = self.ordres.get_nowait()
                    if ordre[0] == "demarrer":
                        d.demarrer(PAR_NOM[ordre[1]])
                    elif ordre[0] == "arreter":
                        d.arreter("Réception arrêtée.")
            except Empty:
                pass
            try:
                bloc = self.file.get(timeout=0.1)
            except Empty:
                continue
            d.traiter(bloc)

    # --------------------------------------------------------------- interface
    def creer_widget(self):
        w = QWidget()
        w.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(4)
        self.grande = None
        ligne0 = QHBoxLayout()
        self.c_freq = QComboBox()
        self.c_freq.addItem("Prérégler une fréquence…")
        for f, mode, nom in FREQUENCES:
            self.c_freq.addItem(libelle_frequence(f, mode, nom))
        self.c_freq.setToolTip("Accorde le récepteur sur une fréquence SSTV, dans le bon mode "
                               "(ondes courtes : clé qui reçoit la HF ou convertisseur)")
        self.c_freq.activated.connect(self._preregler)
        ligne0.addWidget(self.c_freq, 1)
        b_grand = QPushButton("Agrandir")
        b_grand.setToolTip("Affiche l'image en grand dans une fenêtre à part")
        b_grand.clicked.connect(self.agrandir)
        ligne0.addWidget(b_grand)
        lay.addLayout(ligne0)
        self.avertissement = QLabel()
        self.avertissement.setWordWrap(True)
        self.avertissement.setStyleSheet("color: #ff7043")
        self.avertissement.hide()
        lay.addWidget(self.avertissement)
        self.vue = VueImage()
        lay.addWidget(self.vue)
        self.statut = QLabel()
        self.statut.setWordWrap(True)
        lay.addWidget(self.statut)
        self.tonalite = QLabel()
        self.tonalite.setStyleSheet("color: gray")
        lay.addWidget(self.tonalite)

        ligne = QHBoxLayout()
        self.choix = QComboBox()
        self.choix.addItem("Automatique (VIS)")
        self.choix.addItems(sorted(PAR_NOM, key=lambda n: (n[0], len(n), n)))
        self.choix.setToolTip("Mode à forcer pour un démarrage à la main (en-tête VIS manqué)")
        ligne.addWidget(self.choix, 1)
        self.b_demarrer = QPushButton("Démarrer")
        self.b_demarrer.setToolTip("Commence l'image maintenant dans le mode choisi")
        self.b_demarrer.clicked.connect(self._demarrer)
        ligne.addWidget(self.b_demarrer)
        self.b_arreter = QPushButton("Arrêter")
        self.b_arreter.clicked.connect(lambda: self.ordres.put(("arreter",)))
        ligne.addWidget(self.b_arreter)
        lay.addLayout(ligne)
        self.choix.currentIndexChanged.connect(lambda i: self.b_demarrer.setEnabled(i > 0))
        self.b_demarrer.setEnabled(False)

        ligne2 = QHBoxLayout()
        self.auto = QCheckBox("Enregistrer chaque image")
        self.auto.setChecked(True)
        ligne2.addWidget(self.auto, 1)
        b_sauver = QPushButton("Enregistrer…")
        b_sauver.clicked.connect(self._enregistrer_sous)
        ligne2.addWidget(b_sauver)
        b_dossier = QPushButton("Dossier")
        b_dossier.setToolTip(str(self.dossier))
        b_dossier.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.dossier))))
        ligne2.addWidget(b_dossier)
        lay.addLayout(ligne2)

        self.version_vue = -1
        self.minuterie = QTimer(w, interval=250, timeout=self._rafraichir)
        self.minuterie.start()
        self._rafraichir()
        return w

    def _preregler(self, i: int) -> None:
        if i <= 0:
            return
        f, mode, _ = FREQUENCES[i - 1]
        avant_f, avant_mode = self.ctx.frequence(), self.ctx.mode()
        self.ctx.regler_mode(mode)
        self.ctx.regler_frequence(f)
        if abs(self.ctx.frequence() - f) > 1:
            # Hors de la plage de la clé : la fenêtre a laissé le VFO au bord de la bande. On revient
            # où l'on était et on dit pourquoi, plutôt que d'écouter une fréquence qui n'est pas la bonne.
            self.ctx.regler_mode(avant_mode)
            self.ctx.regler_frequence(avant_f)
            self.c_freq.setCurrentIndex(0)
            self.avertissement.setText(
                f"{f / 1e6:.3f} MHz est hors de la plage de la clé : réception impossible. ".replace(".", ",", 1)
                + "Ondes courtes : convertisseur HF (réglage « Convertisseur »), clé RTL-SDR Blog V4 ou "
                  "échantillonnage direct.")
            self.avertissement.show()
        else:
            self.avertissement.hide()

    def agrandir(self) -> None:
        if self.grande is None:
            self.grande = FenetreSSTV()
            if self.vue.qimage is not None:
                self.grande.vue.montrer(self.vue.qimage)
        self.grande.show()
        self.grande.raise_()
        self.grande.activateWindow()

    def _demarrer(self) -> None:
        if self.choix.currentIndex() > 0:
            self.ordres.put(("demarrer", self.choix.currentText()))

    @staticmethod
    def _qimage(img: np.ndarray):
        h, l, _ = img.shape
        return QImage(np.ascontiguousarray(img).data, l, h, 3 * l, QImage.Format.Format_RGB888).copy()

    def _rafraichir(self) -> None:
        d = self.decodeur
        with d.verrou:
            nouvelle = d.image is not None and d.version != self.version_vue
            img = d.image.copy() if nouvelle else None
            self.version_vue = d.version
            terminees, d.terminees = d.terminees, []
        if nouvelle:
            q = self._qimage(img)
            self.vue.montrer(q)
            if self.grande is not None:
                self.grande.vue.montrer(q)
        for image, mode, date, partielle in terminees:
            if self.auto.isChecked():
                nom = time.strftime("sstv_%Y%m%d_%H%M%S", date) + "_" + mode.replace(" ", "") + (
                    "_partielle" if partielle else "") + ".png"
                self._qimage(image).save(str(self.dossier / nom), "PNG")
        texte = d.message
        if self.perdus:
            texte += f" ({self.perdus} blocs audio perdus : processeur trop chargé)"
        self.statut.setText(texte)
        if self.grande is not None:
            self.grande.statut.setText(texte)
        self.tonalite.setText(f"Tonalité : {d.tonalite:,.0f} Hz".replace(",", " ") if d.tonalite else "")
        self.b_arreter.setEnabled(d.etat == "image")

    def _enregistrer_sous(self) -> None:
        d = self.decodeur
        with d.verrou:
            img = None if d.image is None else d.image.copy()
        if img is None:
            return
        chemin, _ = QFileDialog.getSaveFileName(self.vue, "Enregistrer l'image SSTV",
                                                str(self.dossier / time.strftime("sstv_%Y%m%d_%H%M%S.png")),
                                                "Images PNG (*.png)")
        if chemin:
            self._qimage(img).save(chemin, "PNG")

    def arreter(self) -> None:
        if hasattr(self, "minuterie"):
            self.minuterie.stop()
        if getattr(self, "grande", None) is not None:
            self.grande.close()
            self.grande.deleteLater()
            self.grande = None
        self.fin.set()
        self.fil.join(timeout=1)
