"""S-mètre : cadran à aiguille et barre de LED, comme celui de SDR Web.

Échelle relative (la clé n'est pas étalonnée en dBm) : 6 dB par point S, S9 à
S9_DBFS ; au-delà, +10 à +60 dB. L'aiguille a la balistique d'un galvanomètre
(montée rapide, retombée plus lente), un point rouge garde la crête 1,5 s.
Valeur exacte en infobulle du cadran.

Exemple de module : le rappel du bus (thread DSP) ne fait que stocker la
valeur ; un QTimer du thread GUI anime les widgets.
"""

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QVBoxLayout, QWidget

from sdrmod.modules import Module

S9_DBFS = -55.0
S_MIN = S9_DBFS - 8 * 6 - 6          # S0
S_MAX = S9_DBFS + 60                 # S9+60
NB_LED = 30
ROUGE = 0.6                          # début de la zone rouge (S9) sur l'échelle 0..1


def position_s(db: float) -> float:
    """Position 0..1 : S0 à S9 sur les 60 % de gauche, +10 à +60 sur le reste."""
    if db <= S9_DBFS:
        return 0.6 * max(0.0, (db - S_MIN) / (S9_DBFS - S_MIN))
    return 0.6 + 0.4 * min(1.0, (db - S9_DBFS) / (S_MAX - S9_DBFS))


def texte_s(db: float) -> str:
    if db <= S9_DBFS:
        return f"S{max(0, min(9, round((db - S_MIN) / 6)))}"
    return f"S9+{min(60, round((db - S9_DBFS) / 10) * 10)}"


# Graduations : (position, longue, texte, rouge)
GRADUATIONS = [(position_s(S_MIN + 6 * n), n % 2 == 1, str(n) if n % 2 else "", False) for n in range(1, 10)] + \
              [(position_s(S9_DBFS + d), d % 20 == 0, f"+{d}" if d % 20 == 0 else "", True) for d in range(10, 70, 10)]


class Cadran(QWidget):
    BLANC, ROUGE_C = QColor("#ffffff"), QColor("#dd2222")

    def __init__(self) -> None:
        super().__init__()
        self.aiguille = 0.0
        self.crete = None            # position 0..1 du repère de crête
        self.setMinimumWidth(200)
        self.setFixedHeight(110)

    def paintEvent(self, _ev) -> None:
        W, H = self.width(), self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cadre = QPainterPath()
        cadre.addRoundedRect(QRectF(0, 0, W, H), 8, 8)
        p.fillPath(cadre, QColor("#000000"))
        p.setClipPath(cadre)

        # arc de 100° centré, pivot sous le bas du cadran
        ouverture = math.radians(100)
        cx, rayon = W / 2, min(W * 0.62, H * 1.25)
        cy = H * 0.12 + rayon

        def angle(pos):
            return -math.pi / 2 - ouverture / 2 + pos * ouverture

        def point(pos, d):
            a = angle(pos)
            return QPointF(cx + d * math.cos(a), cy + d * math.sin(a))

        def arc(debut, fin, r, couleur, epaisseur):
            p.setPen(QPen(couleur, epaisseur, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
            # Qt : angles en 1/16 de degré, sens trigonométrique (y vers le haut)
            depart = -math.degrees(angle(debut))
            etendue = -math.degrees(angle(fin) - angle(debut))
            p.drawArc(rect, round(depart * 16), round(etendue * 16))

        arc(0, ROUGE, rayon * 0.86, self.BLANC, 2.5)
        arc(ROUGE, 1, rayon * 0.86 + 1, self.ROUGE_C, 5)

        police = QFont("Share Tech Mono")
        police.setStyleHint(QFont.StyleHint.Monospace)
        police.setWeight(QFont.Weight.DemiBold)
        police.setPixelSize(max(9, round(W / 22)))
        p.setFont(police)
        for pos, longue, texte, rouge in GRADUATIONS:
            p.setPen(QPen(self.ROUGE_C if rouge else self.BLANC, 2 if longue else 1.2))
            p.drawLine(point(pos, rayon * 0.86), point(pos, rayon * (0.79 if longue else 0.82)))
            if texte:
                c = point(pos, rayon * 0.95)
                p.setPen(self.BLANC)
                p.drawText(QRectF(c.x() - 20, c.y() - 10, 40, 20), Qt.AlignmentFlag.AlignCenter, texte)

        titre = QFont("Orbitron")
        titre.setWeight(QFont.Weight.Bold)
        titre.setPixelSize(max(10, round(W / 18)))
        p.setFont(titre)
        p.setPen(self.BLANC)
        p.drawText(QRectF(cx - 30, cy - rayon * 0.5 - 12, 60, 24), Qt.AlignmentFlag.AlignCenter, "S")

        if self.crete is not None:   # repère de crête
            c = point(max(0.0, min(1.0, self.crete)), rayon * 0.74)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self.ROUGE_C)
            p.drawEllipse(c, 2.5, 2.5)

        bout = point(max(0.0, min(1.02, self.aiguille)), rayon * 0.9)
        p.setPen(QPen(QColor(0, 0, 0, 64), 3))
        p.drawLine(QPointF(cx + 2, cy + 2), bout + QPointF(2, 2))
        p.setPen(QPen(self.BLANC, 1.8))
        p.drawLine(QPointF(cx, cy), bout)

        p.fillRect(QRectF(0, H - 10, W, 10), QColor("#2a2a2a"))
        p.end()


class BarreLED(QWidget):
    COULEURS = {"v": QColor("#39ff6a"), "j": QColor("#ffd23f"), "r": QColor("#ff4040")}

    def __init__(self) -> None:
        super().__init__()
        self.allumees = 0
        self.i_crete = -1
        self.zones = ["v" if (i + 0.5) / NB_LED < ROUGE - 0.12 else "j" if (i + 0.5) / NB_LED < ROUGE else "r"
                      for i in range(NB_LED)]
        self.setFixedHeight(22)
        self.setMinimumWidth(3 * NB_LED)

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(255, 255, 255, 20), 1))
        p.setBrush(QColor("#000000"))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 6, 6)
        p.setPen(Qt.PenStyle.NoPen)
        pas = (self.width() - 8 + 2) / NB_LED
        for i, zone in enumerate(self.zones):
            couleur = QColor(self.COULEURS[zone])
            rect = QRectF(4 + i * pas, 5, pas - 2, 12)
            if i < self.allumees or i == self.i_crete:   # halo, comme le box-shadow de SDR Web
                halo = QColor(couleur)
                halo.setAlpha(70)
                p.setBrush(halo)
                p.drawRoundedRect(rect.adjusted(-1.5, -1.5, 1.5, 1.5), 3, 3)
            else:
                couleur.setAlphaF(0.18)
            p.setBrush(couleur)
            p.drawRoundedRect(rect, 2, 2)
        p.end()


class SMetre(Module):
    nom = "S-mètre"
    description = "Cadran à aiguille et barre de LED (points S), comme dans SDR Web."

    def demarrer(self) -> None:
        self.niveau = -120.0
        self.t_mesure = 0.0
        self.abonner("niveau", self._sur_niveau)

    def _sur_niveau(self, dbfs: float) -> None:      # thread DSP : on stocke seulement
        if math.isfinite(dbfs):
            self.niveau = dbfs
            self.t_mesure = time.monotonic()

    def creer_widget(self) -> QWidget:
        w = QWidget()
        w.setObjectName("smetre")
        w.setStyleSheet("#smetre { background: #000; }")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(8)
        self.cadran = Cadran()
        self.barre = BarreLED()
        lay.addWidget(self.cadran)
        lay.addWidget(self.barre)
        lay.addStretch(1)
        self.crete = -math.inf
        self.t_crete = 0.0
        self.t_avant = time.monotonic()
        self.t_texte = 0.0
        self.minuterie = QTimer(w, interval=33, timeout=self._animer)
        self.minuterie.start()
        return w

    def _animer(self) -> None:
        maintenant = time.monotonic()
        dt = min(0.1, maintenant - self.t_avant)
        self.t_avant = maintenant
        actif = maintenant - self.t_mesure < 1.0
        v = self.niveau if actif else -120.0
        if not actif:
            self.crete = v
        # balistique d'un galvanomètre : montée rapide, retombée plus lente
        c = self.cadran
        cible = max(0.0, min(1.02, position_s(v)))
        tau = 0.05 if cible > c.aiguille else 0.35
        c.aiguille += (cible - c.aiguille) * (1 - math.exp(-dt / tau))
        if v >= self.crete:
            self.crete, self.t_crete = v, maintenant
        elif maintenant - self.t_crete > 1.5:
            self.crete = max(v, self.crete - 20 * dt)
        c.crete = position_s(self.crete)
        c.update()
        b = self.barre
        b.allumees = round(min(1.0, c.aiguille) * NB_LED)
        i_crete = min(NB_LED - 1, math.floor(position_s(self.crete) * NB_LED))
        b.i_crete = i_crete if i_crete >= b.allumees else -1
        b.update()
        if maintenant - self.t_texte > 0.2:
            self.t_texte = maintenant
            c.setToolTip(f"{texte_s(v)} · {v:.1f} dBFS · crête {texte_s(self.crete)}")

    def arreter(self) -> None:
        if hasattr(self, "minuterie"):
            self.minuterie.stop()
