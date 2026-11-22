"""S-mètre : niveau du canal en dBFS avec maintien de crête.

Exemple minimal de module : s'abonne au sujet « niveau » et affiche un widget.
"""

from PySide6.QtCore import QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from sdrmod.modules import Module


class BarreLED(QWidget):
    NOMBRE_SEGMENTS = 24
    COULEURS = tuple(QColor(couleur) for couleur in ("#59ef65", "#ffcc42", "#ff5555"))
    COULEUR_ETEINTE = QColor("#26313a")

    def __init__(self) -> None:
        super().__init__()
        self.segments_allumes = 0
        self.setFixedHeight(14)
        self.setMinimumWidth(3 * self.NOMBRE_SEGMENTS)

    def regler_niveau(self, dbfs: float) -> None:
        niveau = max(-120, min(0, dbfs))
        segments = round((niveau + 120) / 120 * self.NOMBRE_SEGMENTS)
        if segments != self.segments_allumes:
            self.segments_allumes = segments
            self.update()

    def paintEvent(self, ev) -> None:
        peintre = QPainter(self)
        peintre.setRenderHint(QPainter.RenderHint.Antialiasing)
        peintre.setPen(Qt.PenStyle.NoPen)
        pas = self.width() / self.NOMBRE_SEGMENTS
        for i in range(self.NOMBRE_SEGMENTS):
            couleur = self.COULEUR_ETEINTE
            if i < self.segments_allumes:
                zone = 0 if i < 18 else 1 if i < 22 else 2
                couleur = self.COULEURS[zone]
            peintre.setBrush(couleur)
            peintre.drawRoundedRect(QRectF(i * pas, 1, pas - 2, self.height() - 2), 2, 2)
        peintre.end()


class SMetre(Module):
    nom = "S-mètre"
    description = "Affiche le niveau du signal dans le canal (dBFS) avec maintien de crête."

    def demarrer(self) -> None:
        self.niveau = -120.0
        self.crete = -120.0
        self.abonner("niveau", self._sur_niveau)

    def _sur_niveau(self, dbfs: float) -> None:      # thread DSP : on stocke seulement
        self.niveau = dbfs
        self.crete = max(dbfs, self.crete - 0.3)     # décroissance lente de la crête

    def creer_widget(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.barre = BarreLED()
        self.lbl = QLabel()
        self.lbl.setStyleSheet("font: bold 16pt 'DejaVu Sans Mono'; color: #7CFC00;")
        lay.addWidget(self.lbl)
        lay.addWidget(self.barre)
        lay.addStretch(1)
        self.minuterie = QTimer(w, interval=100, timeout=self._rafraichir)
        self.minuterie.start()
        return w

    def _rafraichir(self) -> None:
        self.barre.regler_niveau(self.niveau)
        self.lbl.setText(f"{self.niveau:6.1f} dBFS   crête {self.crete:6.1f}")

    def arreter(self) -> None:
        if hasattr(self, "minuterie"):
            self.minuterie.stop()
