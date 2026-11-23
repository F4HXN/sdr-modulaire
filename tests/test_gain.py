"""Saturation et ajustement du gain dans l'application, avec une clé simulée qui écrête comme une vraie.

La source simulée reçoit les paliers de gain d'une FC0013 (−9,9 à 19,7 dB) et une amplification qui fait
écrêter ses échantillons (comme les octets 0 et 255 d'une clé 8 bits) au-dessus d'environ 7 dB ; le gain
automatique du tuner sature.

    QT_QPA_PLATFORM=offscreen HOME=$(mktemp -d) python3 -m tests.test_gain
"""

import sys

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from sdrmod.sources.simulation import SourceSimulation

GAINS = [-9.9, -7.3, -6.3, -5.8, -2.6, 0.0, 2.6, 5.8, 7.1, 9.6, 12.6, 14.9, 17.9, 19.7]
_lire = SourceSimulation.lire


def _regler_gain(self, gain):
    self._g = 25.0 if gain is None else gain             # gain automatique du tuner : trop fort


def _lire_ecrete(self, n):
    x = _lire(self, n) * np.float32(4.6 * 10 ** ((getattr(self, "_g", 25.0) - 7.5) / 20))
    return (np.clip(x.real, -0.995, 0.992) + 1j * np.clip(x.imag, -0.995, 0.992)).astype(np.complex64)


SourceSimulation.gains = lambda self: list(GAINS)
SourceSimulation.regler_gain = _regler_gain
SourceSimulation.lire = _lire_ecrete


def main() -> int:
    app = QApplication(sys.argv)
    from sdrmod.ui.fenetre import FenetrePrincipale
    f = FenetrePrincipale(source="simulation")
    f.reglages.gain = None
    f.show()
    journal = []
    etat = {}

    def ok(cond, texte):
        journal.append(("OK    " if cond else "ÉCHEC ") + texte)

    def etape(t, action):
        QTimer.singleShot(t, action)

    def mesurer_paliers():
        """Écrêtage mesuré directement sur la source, palier par palier (référence indépendante)."""
        s = SourceSimulation()
        s.temps_reel = False
        s.ouvrir(f.reglages.taux, f.reglages.centre)
        taux = []
        for g in GAINS:
            s.regler_gain(g)
            x = s.lire(400_000)
            taux.append(float(np.mean((np.abs(x.real) >= 0.99) | (np.abs(x.imag) >= 0.99))))
        etat["reference"] = max((i for i, t in enumerate(taux) if t <= f.SEUIL_SATURATION), default=0)

    etape(100, f.demarrer)
    etape(1600, lambda: ok(f.panneau.sat_lbl.text().endswith("%") and "ff5252" in f.panneau.sat_lbl.styleSheet(),
                           f"gain automatique : saturation affichée en rouge ({f.panneau.sat_lbl.text()})"))
    etape(1700, mesurer_paliers)
    etape(1800, f.panneau.b_ajuster.click)
    etape(1900, lambda: ok(not f.panneau.b_ajuster.isEnabled(), "bouton Ajuster désactivé pendant l'ajustement"))

    def verifier():
        g = f.panneau.gain_actuel()
        ref = GAINS[etat["reference"]]
        ok(g == ref, f"gain ajusté : {g} dB (palier le plus fort sans écrêtage, mesuré à part : {ref} dB)")
        ok(f.reglages.gain == g and not f.panneau.gain_auto.isChecked(), "gain manuel enregistré dans les réglages")
        ok(f.panneau.b_ajuster.isEnabled() and f._ajustement is None, "ajustement terminé, bouton réactivé")
        ok("Gain ajusté" in f.statusBar().currentMessage(), f"message : {f.statusBar().currentMessage()}")

    etape(16_000, verifier)
    etape(17_200, lambda: ok(f.panneau.sat_lbl.text() == "aucune", f"plus de saturation ({f.panneau.sat_lbl.text()})"))
    etape(17_300, f.close)
    etape(17_500, app.quit)
    app.exec()
    print("\n".join(journal))
    tout = all(l.startswith("OK") for l in journal)
    print("\nRÉSULTAT :", "TOUT OK" if tout else "ÉCHECS")
    return 0 if tout else 1


if __name__ == "__main__":
    raise SystemExit(main())
