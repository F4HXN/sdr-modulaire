"""Simule une clé RTL2838UHIDIR / FC0013 pour vérifier l'affichage du matériel.

QT_QPA_PLATFORM=offscreen python3 -m tests.test_materiel capture.png
"""
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from sdrmod import sources
from sdrmod.__main__ import appliquer_theme_sombre
from sdrmod.sources.rtlsdr_source import SourceRtlSdr
from sdrmod.sources.simulation import SourceSimulation

GAINS_FC0013 = [-9.9, -7.3, -6.5, -6.3, -6.0, -5.8, -5.4, 5.8, 6.1, 6.3, 6.5, 6.7, 6.8, 7.0, 7.1,
                17.9, 18.1, 18.2, 18.4, 18.6, 18.8, 19.1, 19.7]


class FausseCle:
    valid_gains_db = GAINS_FC0013
    def get_tuner_type(self): return 3


class SourceFC0013(SourceRtlSdr):
    def ouvrir(self, taux, frequence):
        self.sdr = FausseCle()
        self._sim = SourceSimulation(); self._sim.ouvrir(taux, frequence)
    def _chaines_usb(self): return ("Realtek", "RTL2838UHIDIR", "00000001")
    def lire(self, n): return self._sim.lire(n)
    def regler_frequence(self, f): self._sim.regler_frequence(f)
    def regler_gain(self, g): pass
    def fermer(self): pass


def main():
    sources.SOURCES["rtlsdr"] = SourceFC0013
    import sdrmod.ui.fenetre as fen
    fen.SOURCES["rtlsdr"] = SourceFC0013
    app = QApplication(sys.argv); app.setStyle("Fusion"); appliquer_theme_sombre(app)
    f = fen.FenetrePrincipale(source="rtlsdr"); f.resize(1400, 900); f.show()
    res = []
    QTimer.singleShot(100, f.demarrer)
    def verif():
        i = f.infos_materiel
        res.append(f"infos : {i['fabricant']} {i['modele']} / {i['tuner']} / plages {i['plages']} / "
                   f"bias_t={i['bias_t']} direct={i['echantillonnage_direct']}")
        res.append(f"bias-T actif={f.panneau.bias.isEnabled()}  direct actif={f.panneau.direct.isEnabled()}")
        res.append(f"statut : {f.lbl_source.text()}")
        f.regler_frequence(1_296_200_000)
        res.append(f"1296,2 MHz -> '{f._avertissement}' rouge={f.afficheur.hors_plage}")
        f.grab().save(sys.argv[1] + ".hors.png")
        f.regler_frequence(145_500_000)
        res.append(f"145,5 MHz -> '{f._avertissement}' rouge={f.afficheur.hors_plage}")
    QTimer.singleShot(1500, verif)
    QTimer.singleShot(2500, lambda: f.grab().save(sys.argv[1]))
    QTimer.singleShot(2600, lambda: (f.close(), app.quit()))
    app.exec()
    print("\n".join(res))


if __name__ == "__main__":
    main()
