"""Écrit les poids du Décodeur CW pour SDR Web (web/cw-poids.bin), depuis le module.

python3 exporter_web.py ../../modules-a-telecharger/decodeur_cw.py ../../web/cw-poids.bin

Format : « CWP1 », longueur (uint32 LE) d'un index JSON {nom: {"forme": [...], "debut": n}},
l'index, un octet de remplissage si besoin, puis tous les poids en float16 LE à la suite
(debut compté en nombres float16). Lu par web/cw.js (chargerPoids).
"""

import ast
import base64
import io
import json
import struct
import sys
import zlib

import numpy as np


def poids_du_module(chemin: str) -> dict:
    """Relit la liste POIDS du module sans l'importer (pas besoin de PySide6)."""
    arbre = ast.parse(open(chemin, encoding="utf-8").read())
    for noeud in arbre.body:
        if isinstance(noeud, ast.Assign) and any(getattr(c, "id", "") == "POIDS" for c in noeud.targets):
            b64 = "".join(ast.literal_eval(noeud.value))
            z = np.load(io.BytesIO(zlib.decompress(base64.b64decode(b64))))
            return {k: z[k].astype(np.float16) for k in z.files}
    raise SystemExit("POIDS introuvable dans " + chemin)


def main() -> None:
    poids = poids_du_module(sys.argv[1])
    index, morceaux, debut = {}, [], 0
    for nom in sorted(poids):
        v = np.ascontiguousarray(poids[nom], dtype="<f2")
        index[nom] = {"forme": list(v.shape), "debut": debut}
        morceaux.append(v.tobytes())
        debut += v.size
    entete = json.dumps(index, separators=(",", ":")).encode()
    if (8 + len(entete)) % 2:
        entete += b" "
    with open(sys.argv[2], "wb") as f:
        f.write(b"CWP1" + struct.pack("<I", len(entete)) + entete + b"".join(morceaux))
    print(f"{sys.argv[2]} : {len(poids)} tableaux, {debut} poids, {8 + len(entete) + 2 * debut} octets")


if __name__ == "__main__":
    main()
