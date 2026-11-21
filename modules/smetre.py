"""S-mètre : niveau du canal en dBFS avec maintien de crête.

Exemple minimal de module : s'abonne au sujet « niveau » et affiche un widget.
"""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QProgressBar, QVBoxLayout, QWidget

from sdrmod.modules import Module


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
        self.barre = QProgressBar()
        self.barre.setRange(-120, 0)
        self.barre.setTextVisible(False)
        self.lbl = QLabel()
        self.lbl.setStyleSheet("font: bold 16pt 'DejaVu Sans Mono'; color: #7CFC00;")
        lay.addWidget(self.lbl)
        lay.addWidget(self.barre)
        lay.addStretch(1)
        self.minuterie = QTimer(w, interval=100, timeout=self._rafraichir)
        self.minuterie.start()
        return w

    def _rafraichir(self) -> None:
        self.barre.setValue(int(max(-120, min(0, self.niveau))))
        self.lbl.setText(f"{self.niveau:6.1f} dBFS   crête {self.crete:6.1f}")

    def arreter(self) -> None:
        if hasattr(self, "minuterie"):
            self.minuterie.stop()
