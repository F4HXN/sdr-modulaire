"""Écrit le module decodeur_cw.py à partir du gabarit et des poids entraînés.

python3 exporter_module.py meilleur.pt ../../modules-a-telecharger/decodeur_cw.py
"""

import base64
import io
import sys
import zlib

import numpy as np
import torch


def main():
    from modele import replier_bn
    poids = replier_bn(torch.load(sys.argv[1], map_location="cpu"))   # BatchNorm intégrée aux convolutions
    tampon = io.BytesIO()
    np.savez(tampon, **{k: v.astype(np.float16) for k, v in poids.items()})
    b64 = base64.b64encode(zlib.compress(tampon.getvalue(), 9)).decode()
    lignes = "\n".join(f'    "{b64[i:i + 100]}",' for i in range(0, len(b64), 100))
    gabarit = open("decodeur_cw_gabarit.py", encoding="utf-8").read()
    assert "# @@POIDS@@" in gabarit
    open(sys.argv[2], "w", encoding="utf-8").write(gabarit.replace("# @@POIDS@@", lignes))
    print(f"{sys.argv[2]} : {len(b64) / 1024:.0f} Kio de poids")


if __name__ == "__main__":
    main()
