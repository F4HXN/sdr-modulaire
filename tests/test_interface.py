"""Test de fumée de l'interface (hors écran) avec la source simulée.

QT_QPA_PLATFORM=offscreen python3 -m tests.test_interface /tmp/capture.png
"""

import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from sdrmod.__main__ import appliquer_theme_sombre


def main():
    sortie = sys.argv[1] if len(sys.argv) > 1 else "capture.png"
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    appliquer_theme_sombre(app)
    from sdrmod.ui.fenetre import FenetrePrincipale

    f = FenetrePrincipale(source="simulation")
    f.resize(1400, 860)
    f.show()
    journal = []

    def etape(t, action):
        QTimer.singleShot(t, action)

    def verifier(cond, msg):
        journal.append(("OK  " if cond else "ÉCHEC ") + msg)

    etape(100, f.demarrer)
    etape(300, lambda: verifier(f.moteur.actif, "moteur démarré"))
    etape(400, lambda: verifier(f.activer_module("S-mètre"), "module S-mètre ajouté"))
    etape(450, lambda: verifier(f.activer_module("Mémoires"), "module Mémoires ajouté"))
    etape(500, lambda: verifier(f.activer_module("Enregistreur audio"), "module Enregistreur ajouté"))
    etape(1500, lambda: verifier("S-mètre" in f.docks and f.modules.actifs["S-mètre"].niveau > -40,
                                 f"S-mètre reçoit le niveau ({f.modules.actifs['S-mètre'].niveau:.1f} dBFS)"))
    etape(1600, lambda: f.desactiver_module("Enregistreur audio"))
    etape(1700, lambda: verifier("Enregistreur audio" not in f.modules.actifs
                                 and not f.bus._abonnes.get("audio"), "Enregistreur retiré et désabonné"))
    etape(1800, lambda: f._clic_spectre(145_300_000 - 250_000 + 1200))
    etape(1850, lambda: f.regler_mode("AM"))
    etape(1900, lambda: verifier(abs(f.reglages.frequence - 145_050_000) < 1, "clic sur le spectre → accord 145,050 MHz"))
    etape(2800, lambda: verifier(f.moteur.demod.nom == "AM" and f.moteur.demod.niveau_dbfs > -40,
                                 f"AM reçue ({f.moteur.demod.niveau_dbfs:.1f} dBFS)"))
    etape(2900, lambda: f._changer_taux(1_920_000))
    etape(3500, lambda: verifier(f.moteur.actif and f.moteur.taux == 1_920_000, "changement de taux à chaud"))
    etape(3600, lambda: f.regler_frequence(145_500_000))
    etape(3650, lambda: f.regler_mode("NFM"))
    etape(3700, f._echelle_auto)
    etape(5200, lambda: f.grab().save(sortie))
    etape(5300, f.close)
    etape(5500, app.quit)
    app.exec()
    print("\n".join(journal))
    return 0 if all(j.startswith("OK") for j in journal) else 1


if __name__ == "__main__":
    raise SystemExit(main())
