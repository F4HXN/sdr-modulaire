"""Vérifie la signature Ed25519 du catalogue des modules et la restriction de la base.

Lancer : QT_QPA_PLATFORM=offscreen python3 -m tests.test_catalogue  (depuis la racine du projet)
Les catalogues d'essai ci-dessous sont signés avec la vraie clé de l'auteur.
"""

from sdrmod.signature import verifier
from sdrmod.ui.dialogue_ajout_modules import lire_catalogue_signe, verifier_base

ZEROS = "0" * 64
CAT_OK = ('{"version": 1, "base": "https://rtl-sdr.fr/", "modules": [{"fichier": "essai.py", "nom": "Essai", '
          f'"sha256": "{ZEROS}", "taille": 1}}]}}\n').encode()
SIG_OK = b"w6UO+WHBgy3N/BoTUD+1tJaYpCl6aynEKCVLUw/WiDLZOJAHtEi1ok+o6TteIpB/dglxfqJpTUvfH5ofxamIDQ=="
CAT_AUTRE_BASE = ('{"version": 1, "base": "https://exemple.org/", "modules": [{"fichier": "essai.py", "nom": "Essai", '
                  f'"sha256": "{ZEROS}", "taille": 1}}]}}\n').encode()
SIG_AUTRE_BASE = b"zwCHHtgr3ovNTk9QvHYqF+OWl/amuo6CrOgMBailkMucGMeSYv6Ft+Nlt0RSJwsgm3r34GAn91gMILiqtnY1DQ=="


def refuse(fonction, *args) -> bool:
    try:
        fonction(*args)
    except ValueError:
        return True
    return False


def main() -> int:
    h = bytes.fromhex
    cas = [
        ("vecteur RFC 8032 n°1", verifier(
            h("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"), b"",
            h("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"))),
        ("catalogue signé accepté", lire_catalogue_signe(CAT_OK, SIG_OK)[1] == "https://rtl-sdr.fr/"),
        ("catalogue modifié refusé", refuse(lire_catalogue_signe, CAT_OK.replace(b"Essai", b"Esdai"), SIG_OK)),
        ("signature absente refusée", refuse(lire_catalogue_signe, CAT_OK, b"")),
        ("signature illisible refusée", refuse(lire_catalogue_signe, CAT_OK, b"pas du base64 !")),
        ("signature d'un autre catalogue refusée", refuse(lire_catalogue_signe, CAT_OK, SIG_AUTRE_BASE)),
        ("base hors rtl-sdr.fr refusée même signée", refuse(lire_catalogue_signe, CAT_AUTRE_BASE, SIG_AUTRE_BASE)),
    ]
    for base in ("http://rtl-sdr.fr/", "https://rtl-sdr.fr.exemple.org/", "https://rtl-sdr.fr@exemple.org/",
                 "https://rtl-sdr.fr:8443/", "https://rtl-sdr.fr/../", "https://rtl-sdr.fr/a?b=/", None):
        cas.append((f"base refusée : {base}", refuse(verifier_base, base)))
    cas.append(("base acceptée : https://rtl-sdr.fr/modules/", verifier_base("https://rtl-sdr.fr/modules/") is not None))
    ok = True
    for libelle, resultat in cas:
        print(f"{libelle:55} {'OK' if resultat else 'ÉCHEC'}")
        ok &= bool(resultat)
    print("\nRÉSULTAT :", "TOUT OK" if ok else "ÉCHECS")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
