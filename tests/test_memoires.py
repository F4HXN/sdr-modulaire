"""Module Mémoires : gestion (ajout, modification, suppression, groupes, recherche, rappel)
et fichiers (CSV compatible CHIRP, JSON).

Le CSV est contrôlé par CHIRP lui-même quand il est installé (bibliothèque « chirp » du système) :
CHIRP relit notre export, et un fichier écrit par CHIRP est importé par le module.

    HOME=$(mktemp -d) QT_QPA_PLATFORM=offscreen python3 -m tests.test_memoires
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

RACINE = Path(__file__).resolve().parent.parent
journal = []


def verifier(cond, msg):
    journal.append(("OK  " if cond else "ÉCHEC ") + msg)


# CHIRP tourne avec le Python du système (il n'est pas dans le venv).
CHIRP_LIRE = r"""
import sys
from chirp.drivers import generic_csv
r = generic_csv.CSVRadio(sys.argv[1])
for m in r.get_memories():
    if not m.empty:
        print(f"{m.freq}|{m.name}|{m.mode}|{m.comment}")
"""
CHIRP_ECRIRE = r"""
import sys
from chirp import chirp_common
from chirp.drivers import generic_csv
r = generic_csv.CSVRadio(None)
for i, (f, nom, mode) in enumerate([(145_600_000, "F1ZXX R2", "FM"), (118_700_000, "Tour LFMN", "AM"),
                                    (14_074_000, "FT8 20m", "USB"), (145_500_000, "Appel", "FM")]):
    m = chirp_common.Memory()
    m.number, m.freq, m.name, m.mode, m.comment = i, f, nom, mode, "Club"
    r.set_memory(m)
r.save(sys.argv[1])
"""


def chirp(script, chemin):
    try:
        return subprocess.run(["/usr/bin/python3", "-c", script, str(chemin)], capture_output=True,
                              text=True, timeout=60, env={"PATH": "/usr/bin:/bin", "HOME": tempfile.mkdtemp()})
    except (OSError, subprocess.TimeoutExpired):
        return None


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    from sdrmod.ui.fenetre import FenetrePrincipale
    f = FenetrePrincipale(source="simulation")
    verifier(f.activer_module("Mémoires"), "module Mémoires ajouté")
    mod = f.modules.actifs["Mémoires"]
    M = sys.modules[type(mod).__module__]
    app.processEvents()
    verifier(mod.table.rowCount() == 3 and mod.table.item(0, 1).text() == "144.8000",
             f"liste par défaut triée : {[mod.table.item(r, 3).text() for r in range(mod.table.rowCount())]}")

    # Ajout / modification par la boîte de dialogue (réponses simulées)
    reponses = []
    mod._dialogue = lambda titre, m: reponses.pop(0)
    f.regler_mode("NFM")
    f.regler_frequence(145_600_000)
    reponses.append({"nom": "Relais R2", "frequence": round(f.reglages.frequence), "mode": "NFM", "groupe": "Relais"})
    mod._ajouter()
    verifier(any(m["nom"] == "Relais R2" and m["frequence"] == 145_600_000 for m in mod.memoires),
             "Ajouter : fréquence actuelle mémorisée")
    sel = [mod.visibles[ix.row()] for ix in mod.table.selectionModel().selectedRows()]
    verifier(len(sel) == 1 and mod.memoires[sel[0]]["nom"] == "Relais R2", "la nouvelle mémoire est sélectionnée")
    reponses.append({"nom": "Relais R2 Nice", "frequence": 145_612_500, "mode": "NFM", "groupe": "Relais"})
    mod._modifier()
    verifier([m["frequence"] for m in mod.memoires if m["nom"] == "Relais R2 Nice"] == [145_612_500],
             "Modifier : nom et fréquence changés")
    disque = json.loads(mod.fichier.read_text(encoding="utf-8"))
    verifier(any(m["nom"] == "Relais R2 Nice" for m in disque), "enregistré automatiquement dans memoires.json")

    # Groupes, recherche, rappel
    mod.c_groupe.setCurrentText("Relais")
    verifier(mod.table.rowCount() == 1 and mod.table.item(0, 0).text() == "3",
             f"filtre de groupe « Relais » : {mod.table.rowCount()} ligne, CH {mod.table.item(0, 0).text()}")
    mod.c_groupe.setCurrentText(M.TOUS)
    mod.recherche.setText("433,5")
    verifier(mod.table.rowCount() == 1 and mod.table.item(0, 3).text() == "Appel FM 70 cm", "recherche « 433,5 »")
    mod.recherche.setText("")
    f.regler_frequence(100_000_000)
    mod._rappeler(0)
    verifier(f.reglages.frequence == 144_800_000 and f.reglages.mode == "NFM", "double-clic : fréquence rappelée")

    # Suppression
    mod.table.selectRow(0)
    mod._supprimer()
    verifier(len(mod.memoires) == 3 and all(m["nom"] != "APRS" for m in mod.memoires), "Supprimer")

    # Fichiers
    d = Path(tempfile.mkdtemp())
    mod.memoires.append({"nom": "Tour, « LFMN »", "frequence": 118_700_000, "mode": "AM", "groupe": "Aviation"})
    mod._enregistrer()
    mod.exporter_vers(d / "m.json")
    mod.exporter_vers(d / "m.csv")
    avant = [dict(m) for m in mod.memoires]
    mod.memoires.clear()
    n, dbl = mod.importer_texte((d / "m.json").read_text(encoding="utf-8"))
    verifier(mod.memoires == avant and n == 4, f"JSON : export puis import à l'identique ({n} mémoires)")
    n, dbl = mod.importer_texte((d / "m.csv").read_text(encoding="utf-8"))
    verifier(n == 0 and dbl == 4, f"CSV réimporté : aucun doublon ajouté ({dbl} déjà présentes)")
    mod.memoires.clear()
    mod.importer_texte((d / "m.csv").read_text(encoding="utf-8"))
    verifier(mod.memoires == avant, "CSV : export puis import à l'identique (nom avec virgule et guillemets)")
    francais = "Nom;Fréquence;Mode;Groupe\nBalise;144,4;usb;Balises\nmauvais;abc;FM;\n"
    n, _ = mod.importer_texte(francais)
    verifier(n == 1 and any(m["frequence"] == 144_400_000 and m["mode"] == "USB" for m in mod.memoires),
             "CSV en français (point-virgule, virgule décimale) ; ligne illisible ignorée")
    verifier(mod.importer_texte("rien d'utile") == (None, 0), "fichier sans mémoire : refusé")

    # CHIRP, vérification indépendante
    r = chirp(CHIRP_LIRE, d / "m.csv")
    if r is None or r.returncode:
        journal.append("INFO  CHIRP absent : CSV non contrôlé par CHIRP" + (f" ({r.stderr.strip()[-200:]})" if r else ""))
    else:
        lu = [l.split("|") for l in r.stdout.strip().splitlines()]
        attendu = [[str(m["frequence"]), m["nom"], M.VERS_CHIRP[m["mode"]], m["groupe"]] for m in avant]
        verifier(lu == attendu, f"CHIRP relit notre CSV : {len(lu)} mémoires identiques")
        r = chirp(CHIRP_ECRIRE, d / "chirp.csv")
        mod.memoires.clear()
        n, _ = mod.importer_texte((d / "chirp.csv").read_text(encoding="utf-8-sig"))
        vu = [(m["frequence"], m["nom"], m["mode"], m["groupe"]) for m in mod.memoires]
        verifier(vu == [(14_074_000, "FT8 20m", "USB", "Club"), (118_700_000, "Tour LFMN", "AM", "Club"),
                        (145_500_000, "Appel", "NFM", "Club"), (145_600_000, "F1ZXX R2", "NFM", "Club")],
                 f"fichier écrit par CHIRP importé : {n} mémoires, modes et groupes justes")

    f.desactiver_module("Mémoires")
    print("\n".join(journal))
    tout = all(not l.startswith("ÉCHEC") for l in journal)
    print("\nRÉSULTAT :", "TOUT OK" if tout else "ÉCHECS")
    return 0 if tout else 1


if __name__ == "__main__":
    raise SystemExit(main())
