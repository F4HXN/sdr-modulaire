"""Vu-mètre égaliseur : étalonnage des bandes, échelle automatique et balistique.

    python3 -m tests.test_vumetre
"""

import importlib.util
from pathlib import Path

import numpy as np

CHEMIN = Path(__file__).resolve().parent.parent / "modules-a-telecharger" / "vumetre.py"
spec = importlib.util.spec_from_file_location("vumetre", CHEMIN)
vu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vu)
TAUX = 48_000


def sinus(f, a, n=vu.TAILLE, t0=0):
    return a * np.sin(2 * np.pi * f * (t0 + np.arange(n)) / TAUX)


def test_etalonnage() -> None:
    """Sinusoïde pleine échelle = 0 dB dans sa bande, rien ailleurs ; −20 dB pour une amplitude de 0,1."""
    for f, a, attendu in ((1000, 1.0, 0.0), (2000, 0.1, -20.0)):
        db, niveau = vu.analyser(sinus(f, a), TAUX, 16, 16000)
        b = vu.bords(16, 16000)
        j = int(np.searchsorted(b, f) - 1)
        assert abs(db[j] - attendu) < 0.5, (f, db[j])
        autres = np.delete(db, [j - 1, j, j + 1])
        assert autres.max() < attendu - 60, autres.max()
        assert abs(niveau - attendu) < 0.1, niveau


def test_bandes() -> None:
    for n in vu.BANDES:
        for mode, fmax in (("WFM", 16000), ("AM", 5000), ("NFM", 3200), ("USB", 3200)):
            assert vu.frequence_max(mode) == fmax
            b = vu.bords(n, fmax)
            assert len(b) == n + 1 and abs(b[0] - 50) < 1e-9 and abs(b[-1] - fmax) < 1e-6
            db, _ = vu.analyser(np.random.default_rng(1).normal(0, 0.1, vu.TAILLE), TAUX, n, fmax)
            assert np.all(np.isfinite(db))


def test_balistique() -> None:
    """Montée rapide, crête tenue puis retombée, tout à zéro sans audio."""
    m = vu.MoteurVumetre()
    t = 0
    for _ in range(30):                                  # 0,5 s de 1 kHz
        m.pousser(sinus(1000, 0.5, 800, t), TAUX)
        t += 800
        n, c = m.image(1 / 60, 16, 16000)
    j = int(np.argmax(n[:-1]))
    assert n[j] > 0.9 and n[-1] > 0.8, (n[j], n[-1])
    haut = c[j]
    for _ in range(30):                                  # 0,5 s sans audio : crête encore tenue
        n, c = m.image(1 / 60, 16, 16000, actif=False)
    assert n[j] < 0.25 and abs(c[j] - haut) < 1e-9
    for _ in range(270):                                 # 4,5 s (12 dB/s sur 45 dB) : la crête est retombée
        n, c = m.image(1 / 60, 16, 16000, actif=False)
    assert c[j] < 0.2 and n.max() < 0.01


if __name__ == "__main__":
    test_etalonnage()
    test_bandes()
    test_balistique()
    print("vu-mètre : OK")
