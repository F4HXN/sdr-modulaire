"""Décodeur CW neuronal : décode le Morse de l'audio par un réseau de neurones.

Principe (comme la reconnaissance vocale) : audio → spectrogramme autour de la tonalité →
réseau convolutif + GRU bidirectionnel → décodage CTC → texte.
Le réseau (~100 000 paramètres) a été entraîné sur du Morse synthétique réaliste : vitesses
10 à 42 WPM, manipulation humaine irrégulière, QSB, QRM, parasites, bruit jusqu'à -12 dB
(SNR dans 2500 Hz). Ses poids sont embarqués dans ce fichier : seul numpy est nécessaire.
Code d'entraînement : outils/decodeur_cw/ dans le dépôt.

Utilisation : mode CW (ou USB/LSB), squelch ouvert, se caler sur la station. Le texte
apparaît avec environ 4,5 s de retard (le réseau lit des fenêtres de 6 s).
Généré par outils/decodeur_cw/exporter_module.py — ne pas modifier les poids à la main.
"""

import base64
import io
import json
import queue
import threading
import time
import zlib
from collections import deque

import numpy as np
from PySide6.QtCore import QByteArray, Qt, QTimer
from PySide6.QtGui import QFont, QGuiApplication, QTextCursor
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)
from scipy import signal

from sdrmod.modules import Module

# ======================================================================= prétraitement
# (identique à outils/decodeur_cw/cw_commun.py)
TAUX = 8000
NFFT = 128
PAS = 32
K = 8
BANDE = (300.0, 1500.0)
ALPHABET = ["_", " "] + list("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/?.,=+")
H = 64
SOUS_ECHANTILLONNAGE = 4      # trames d'entrée par trame de sortie (16 ms)

_FENETRE = np.hanning(NFFT).astype(np.float32)
_CASE_MIN = int(BANDE[0] / (TAUX / NFFT))
_CASE_MAX = int(BANDE[1] / (TAUX / NFFT))


def spectrogramme(x):
    n = 1 + (len(x) - NFFT) // PAS
    idx = np.arange(NFFT)[None, :] + PAS * np.arange(n)[:, None]
    return (np.abs(np.fft.rfft(x[idx] * _FENETRE, axis=1)) ** 2).T.astype(np.float32)


def pretraiter(x):
    p = spectrogramme(x.astype(np.float32))
    moy = p[_CASE_MIN:_CASE_MAX].mean(axis=1)
    moy = np.convolve(moy, np.ones(3) / 3, mode="same")
    case = _CASE_MIN + int(np.argmax(moy))
    bas = min(max(case - K // 2, 0), p.shape[0] - K)
    zone = np.log10(p[bas:bas + K] + 1e-9)
    med = np.median(zone)
    ecart = zone.std() + 1e-6
    car = np.clip((zone - med) / ecart, -3.0, 8.0).astype(np.float32)
    return car, case * TAUX / NFFT, p[case]


def decoder_ctc(sorties):
    meilleur = sorties.argmax(axis=1)
    res, prec = [], 0
    for t, c in enumerate(meilleur):
        if c != prec and c != 0:
            res.append((t, ALPHABET[c]))
        prec = c
    return res


# ======================================================================= réseau (numpy)
# (identique à outils/decodeur_cw/modele.py)
def _conv1d(x, w, b, pas=1):
    _, t = x.shape
    _, _, k = w.shape
    p = k // 2
    xp = np.pad(x, ((0, 0), (p, p)))
    n = (t + 2 * p - k) // pas + 1
    idx = np.arange(k)[None, :] + pas * np.arange(n)[:, None]
    return np.einsum("ink,oik->on", xp[:, idx], w, optimize=True) + b[:, None]


def _sig(x):
    return 1.0 / (1.0 + np.exp(-x))


def _gru(x, wi, wh, bi, bh):
    t = x.shape[0]
    gi = x @ wi.T + bi
    h = np.zeros(H, dtype=np.float32)
    sortie = np.empty((t, H), dtype=np.float32)
    whr, whz, whn = wh[:H], wh[H:2 * H], wh[2 * H:]
    bhr, bhz, bhn = bh[:H], bh[H:2 * H], bh[2 * H:]
    for i in range(t):
        g = gi[i]
        r = _sig(g[:H] + whr @ h + bhr)
        z = _sig(g[H:2 * H] + whz @ h + bhz)
        n = np.tanh(g[2 * H:] + r * (whn @ h + bhn))
        h = (1 - z) * n + z * h
        sortie[i] = h
    return sortie


def inferer(p, car):
    x = np.maximum(_conv1d(car, p["conv1.weight"], p["conv1.bias"]), 0)
    x = np.maximum(_conv1d(x, p["conv2.weight"], p["conv2.bias"], pas=2), 0)
    x = np.maximum(_conv1d(x, p["conv3.weight"], p["conv3.bias"], pas=2), 0).T
    av = _gru(x, p["gru.weight_ih_l0"], p["gru.weight_hh_l0"], p["gru.bias_ih_l0"], p["gru.bias_hh_l0"])
    ar = _gru(x[::-1], p["gru.weight_ih_l0_reverse"], p["gru.weight_hh_l0_reverse"],
              p["gru.bias_ih_l0_reverse"], p["gru.bias_hh_l0_reverse"])[::-1]
    return np.concatenate([av, ar], axis=1) @ p["sortie.weight"].T + p["sortie.bias"]


def charger_poids():
    brut = zlib.decompress(base64.b64decode("".join(POIDS)))
    z = np.load(io.BytesIO(brut))
    return {k: z[k].astype(np.float32) for k in z.files}


def estimer_wpm(puissance_tonalite):
    """Vitesse estimée d'après la durée des points (trames de 4 ms)."""
    dit = estimer_dit(puissance_tonalite)
    return 1.2 / (dit * PAS / TAUX) if dit else None


def estimer_dit(puissance_tonalite):
    """Durée d'un point en trames de 4 ms, ou None."""
    lp = np.log10(puissance_tonalite + 1e-12)
    bas, haut = np.percentile(lp, 20), np.percentile(lp, 95)
    if haut - bas < 0.5:                     # pas assez de contraste
        return None
    actif = lp > (bas + haut) / 2
    fronts = np.flatnonzero(np.diff(actif.astype(np.int8)))
    if actif[0]:
        fronts = fronts[1:]
    durees = np.diff(fronts)[::2]            # durées « allumé »
    durees = durees[durees >= 3]
    if len(durees) < 4:
        return None
    seuil = 2.0 * np.percentile(durees, 15)
    points = durees[durees <= seuil]
    if len(points) == 0:
        return None
    return float(np.median(points))


# ======================================================================= fenêtre agrandie
class FenetreCW(QWidget):
    """Texte décodé en grand, dans une fenêtre à part : déplaçable et redimensionnable comme toute
    fenêtre ; sa taille et sa place sont retenues (fenetre.json dans le dossier du module)."""

    def __init__(self, module: "DecodeurCW") -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.module = module
        self.setWindowTitle("Décodeur CW — SDR Modulaire")
        self.resize(900, 420)
        lay = QVBoxLayout(self)
        self.zone = QPlainTextEdit()
        self.zone.setReadOnly(True)
        self.zone.setMaximumBlockCount(2000)
        self.taille = 22
        self.zone.setFont(QFont("DejaVu Sans Mono", self.taille))
        lay.addWidget(self.zone, 1)
        self.etat = QLabel("—")
        lay.addWidget(self.etat)
        ligne = QHBoxLayout()
        moins, plus = QPushButton("A−"), QPushButton("A+")
        moins.setToolTip("Texte plus petit")
        plus.setToolTip("Texte plus grand")
        moins.clicked.connect(lambda: self._police(-2))
        plus.clicked.connect(lambda: self._police(+2))
        b_eff = QPushButton("Effacer")
        b_eff.clicked.connect(module.effacer)
        b_cop = QPushButton("Copier")
        b_cop.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.zone.toPlainText()))
        reduire = QPushButton("Réduire")
        reduire.setToolTip("Fermer la fenêtre : le texte reste dans le panneau du module")
        reduire.clicked.connect(self.close)
        for b in (moins, plus, b_eff, b_cop):
            ligne.addWidget(b)
        ligne.addStretch(1)
        ligne.addWidget(reduire)
        lay.addLayout(ligne)
        self._relire()

    def _fichier(self):
        return self.module.ctx.dossier_donnees("decodeur_cw") / "fenetre.json"

    def _relire(self) -> None:
        try:
            g = json.loads(self._fichier().read_text(encoding="utf-8"))
            self.restoreGeometry(QByteArray.fromBase64(g["geometrie"].encode()))
            self._police(g.get("taille", self.taille) - self.taille)
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _police(self, delta: int) -> None:
        self.taille = max(10, min(60, self.taille + delta))
        self.zone.setFont(QFont("DejaVu Sans Mono", self.taille))

    def ajouter(self, morceau: str) -> None:
        self.zone.moveCursor(QTextCursor.End)
        self.zone.insertPlainText(morceau)
        self.zone.moveCursor(QTextCursor.End)

    def closeEvent(self, evenement) -> None:
        try:
            self._fichier().write_text(json.dumps({"geometrie": bytes(self.saveGeometry().toBase64()).decode(),
                                                   "taille": self.taille}), encoding="utf-8")
        except OSError:
            pass
        super().closeEvent(evenement)


# ======================================================================= module
class DecodeurCW(Module):
    nom = "Décodeur CW"
    description = ("Décodage du Morse par réseau de neurones (CNN + GRU + CTC), robuste au bruit, "
                   "au QSB et à la manipulation manuelle. 10 à 42 WPM. Mode CW, squelch ouvert.")

    DUREE_FENETRE = 6.0      # s, comme à l'entraînement
    DUREE_PAS = 3.0          # s : on garde le milieu de chaque fenêtre

    def demarrer(self) -> None:
        self.poids = charger_poids()
        self._sos = None
        self._zi = None
        self._taux_entree = 0
        self._facteur = 1
        self._reste = 0                        # phase de la décimation entre blocs
        self._tampon = np.zeros(0, dtype=np.float32)
        self._echantillons_depuis = 0
        self._premiere = True
        self.texte: deque = deque()            # thread de décodage -> interface
        self.tonalite = None
        self.wpm = None
        self.contraste = None
        self.silence = False
        self.journal = None
        self.fenetre = None
        self._file: queue.Queue = queue.Queue(maxsize=3)
        self._actif = True
        self._fil = threading.Thread(target=self._boucle, name="decodeur-cw", daemon=True)
        self._fil.start()
        self.abonner("audio", self._sur_audio)

    # ------------------------------------------------------------- thread DSP : rapide
    def _preparer_decimation(self, taux: int) -> None:
        self._taux_entree = taux
        self._facteur = max(1, round(taux / TAUX))
        self._sos = signal.butter(8, 3400, fs=taux, output="sos")
        self._zi = np.zeros((self._sos.shape[0], 2))
        self._reste = 0

    def _sur_audio(self, audio: np.ndarray, taux: int) -> None:
        if taux != self._taux_entree:
            self._preparer_decimation(taux)
        y, self._zi = signal.sosfilt(self._sos, audio, zi=self._zi)
        f = self._facteur
        y = y[self._reste::f]
        self._reste = (self._reste - len(audio)) % f
        self._tampon = np.concatenate((self._tampon, y.astype(np.float32)))
        self._echantillons_depuis += len(y)
        n_fen = int(self.DUREE_FENETRE * TAUX)
        if len(self._tampon) > n_fen:
            self._tampon = self._tampon[-n_fen:]
        if len(self._tampon) == n_fen and self._echantillons_depuis >= self.DUREE_PAS * TAUX:
            self._echantillons_depuis = 0
            try:
                self._file.put_nowait((self._tampon.copy(), self._premiere))
                self._premiere = False
            except queue.Full:
                pass

    # ------------------------------------------------------------- thread de décodage
    def _boucle(self) -> None:
        while self._actif:
            try:
                tache = self._file.get(timeout=0.5)
            except queue.Empty:
                continue
            if tache is None:
                break
            fenetre, premiere = tache
            self._decoder(fenetre, premiere)

    def _decoder(self, fenetre: np.ndarray, premiere: bool) -> None:
        if np.count_nonzero(fenetre) < 0.5 * len(fenetre):     # squelch fermé
            self.silence = True
            return
        self.silence = False
        car, tonalite, p_tonalite = pretraiter(fenetre)
        self.tonalite = tonalite
        dit = estimer_dit(p_tonalite)
        self.wpm = 1.2 / (dit * PAS / TAUX) if dit else None
        lp = np.log10(p_tonalite + 1e-12)
        self.contraste = 10 * (np.percentile(lp, 95) - np.percentile(lp, 20))
        logits = inferer(self.poids, car)
        pas_sortie = SOUS_ECHANTILLONNAGE * PAS / TAUX          # 16 ms par trame de sortie
        debut = 0.0 if premiere else (self.DUREE_FENETRE - self.DUREE_PAS) / 2
        fin = (self.DUREE_FENETRE + self.DUREE_PAS) / 2
        texte = "".join(c for t, c in decoder_ctc(logits) if debut <= t * pas_sortie < fin)
        if texte:
            self.texte.append(texte)

    # ------------------------------------------------------------- interface
    def creer_widget(self) -> QWidget:
        # Panneau compact dans la colonne des modules : 2 à 3 lignes de texte ; le texte en grand
        # est dans la fenêtre « Agrandir ».
        w = QWidget()
        w.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        self.zone = QPlainTextEdit()
        self.zone.setReadOnly(True)
        self.zone.setFont(QFont("DejaVu Sans Mono", 11))
        self.zone.setMaximumBlockCount(2000)
        self.zone.setPlaceholderText("Mode CW, squelch ouvert, cale-toi sur une station…")
        ligne_h = self.zone.fontMetrics().lineSpacing()
        self.zone.setMinimumHeight(2 * ligne_h + 12)
        self.zone.setMaximumHeight(3 * ligne_h + 12)
        self.zone.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        lay.addWidget(self.zone)
        self.etat = QLabel("—")
        self.etat.setWordWrap(True)
        lay.addWidget(self.etat)
        ligne = QHBoxLayout()
        b_cw = QPushButton("Mode CW")
        b_cw.clicked.connect(lambda: self.ctx.regler_mode("CW"))
        b_eff = QPushButton("Effacer")
        b_eff.clicked.connect(self.effacer)
        b_cop = QPushButton("Copier")
        b_cop.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.zone.toPlainText()))
        b_agr = QPushButton("Agrandir")
        b_agr.setToolTip("Ouvre le texte en grand dans une fenêtre déplaçable et redimensionnable")
        b_agr.clicked.connect(self.agrandir)
        self.c_journal = QCheckBox("Journal")
        self.c_journal.setToolTip("Enregistre le texte décodé, horodaté, dans le dossier du module")
        self.c_journal.toggled.connect(self._basculer_journal)
        for b in (b_cw, b_eff, b_cop, b_agr, self.c_journal):
            ligne.addWidget(b)
        lay.addLayout(ligne)
        self.minuterie = QTimer(w, interval=250, timeout=self._rafraichir)
        self.minuterie.start()
        return w

    def agrandir(self) -> None:
        if self.fenetre is None:
            self.fenetre = FenetreCW(self)
            self.fenetre.zone.setPlainText(self.zone.toPlainText())
            self.fenetre.zone.moveCursor(QTextCursor.End)
        self.fenetre.show()
        self.fenetre.raise_()
        self.fenetre.activateWindow()

    def effacer(self) -> None:
        self.zone.clear()
        if self.fenetre is not None:
            self.fenetre.zone.clear()

    def _rafraichir(self) -> None:
        while self.texte:
            morceau = self.texte.popleft()
            self.zone.moveCursor(QTextCursor.End)
            fin_actuelle = self.zone.toPlainText()[-1:] if self.zone.toPlainText() else " "
            if fin_actuelle == " ":
                morceau = morceau.lstrip(" ")
            morceau = morceau.replace("  ", " ")
            self.zone.insertPlainText(morceau)
            self.zone.moveCursor(QTextCursor.End)
            if self.fenetre is not None:
                self.fenetre.ajouter(morceau)
            if self.journal:
                self.journal.write(morceau)
                self.journal.flush()
        if self.ctx.mode() not in ("CW", "USB", "LSB"):
            self.etat.setText(f"Mode {self.ctx.mode()} : passe en CW pour décoder")
        elif self.silence:
            self.etat.setText("Squelch fermé : ouvre-le pour décoder")
        elif self.tonalite is None:
            self.etat.setText("En attente de 6 s d'audio…")
        else:
            wpm = f"~{self.wpm:.0f} WPM" if self.wpm else "vitesse ?"
            self.etat.setText(f"Tonalité {self.tonalite:.0f} Hz · {wpm} · contraste {self.contraste:.0f} dB")
        if self.fenetre is not None and self.fenetre.isVisible():
            self.fenetre.etat.setText(self.etat.text())

    def _basculer_journal(self, actif: bool) -> None:
        if actif:
            nom = time.strftime("cw_%Y%m%d.txt")
            self.journal = open(self.ctx.dossier_donnees("decodeur_cw") / nom, "a", encoding="utf-8")
            self.journal.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')} "
                               f"{self.ctx.frequence() / 1e6:.4f} MHz] ")
        elif self.journal:
            self.journal.close()
            self.journal = None

    def arreter(self) -> None:
        self._actif = False
        try:
            self._file.put_nowait(None)
        except queue.Full:
            pass
        self._fil.join(timeout=2)
        if hasattr(self, "minuterie"):
            self.minuterie.stop()
        if self.fenetre is not None:
            self.fenetre.close()
            self.fenetre = None
        if self.journal:
            self.journal.close()
            self.journal = None


# ======================================================================= poids du réseau
# npz float16 compressé (zlib) puis base64
POIDS = [
# @@POIDS@@
]
