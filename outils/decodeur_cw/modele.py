"""Réseau CNN + GRU bidirectionnel + CTC (PyTorch, entraînement seulement) et inférence numpy."""

import numpy as np

from cw_commun import ALPHABET, K

H = 64


def construire_torch():
    import torch.nn as nn

    class ReseauCW(nn.Module):
        def __init__(self):
            super().__init__()
            self.conv1 = nn.Conv1d(K, 64, 5, padding=2)
            self.bn1 = nn.BatchNorm1d(64)
            self.conv2 = nn.Conv1d(64, 64, 5, stride=2, padding=2)
            self.bn2 = nn.BatchNorm1d(64)
            self.conv3 = nn.Conv1d(64, 64, 5, stride=2, padding=2)
            self.bn3 = nn.BatchNorm1d(64)
            self.gru = nn.GRU(64, H, batch_first=True, bidirectional=True)
            self.sortie = nn.Linear(2 * H, len(ALPHABET))
            self.act = nn.ReLU()

        def forward(self, x):                   # x : (B, K, T)
            x = self.act(self.bn1(self.conv1(x)))
            x = self.act(self.bn2(self.conv2(x)))
            x = self.act(self.bn3(self.conv3(x)))
            x, _ = self.gru(x.transpose(1, 2))  # (B, T', 2H)
            return self.sortie(x)               # logits (B, T', C)

    return ReseauCW()


SOUS_ECHANTILLONNAGE = 4      # trames d'entrée par trame de sortie


def replier_bn(etat: dict) -> dict:
    """Intègre chaque BatchNorm dans la convolution qui la précède (poids numpy float32)."""
    p = {k: v.detach().cpu().numpy().astype(np.float32) for k, v in etat.items()
         if not k.endswith("num_batches_tracked")}
    for i in (1, 2, 3):
        g, b = p.pop(f"bn{i}.weight"), p.pop(f"bn{i}.bias")
        m, v = p.pop(f"bn{i}.running_mean"), p.pop(f"bn{i}.running_var")
        e = g / np.sqrt(v + 1e-5)
        p[f"conv{i}.weight"] = p[f"conv{i}.weight"] * e[:, None, None]
        p[f"conv{i}.bias"] = (p[f"conv{i}.bias"] - m) * e + b
    return p


# ---------------------------------------------------------------- inférence numpy
def _conv1d(x, w, b, pas=1):
    cin, t = x.shape
    cout, _, k = w.shape
    p = k // 2
    xp = np.pad(x, ((0, 0), (p, p)))
    n = (t + 2 * p - k) // pas + 1
    idx = np.arange(k)[None, :] + pas * np.arange(n)[:, None]       # (n, k)
    fen = xp[:, idx]                                                # (cin, n, k)
    return np.einsum("ink,oik->on", fen, w, optimize=True) + b[:, None]


def _sig(x):
    return 1.0 / (1.0 + np.exp(-x))


def _gru(x, wi, wh, bi, bh):
    t = x.shape[0]
    gi = x @ wi.T + bi                                              # (T, 3H)
    h = np.zeros(H, dtype=np.float32)
    sortie = np.empty((t, H), dtype=np.float32)
    whr, whz, whn = wh[:H], wh[H:2 * H], wh[2 * H:]
    bhr, bhz, bhn = bh[:H], bh[H:2 * H], bh[2 * H:]
    for i in range(t):
        g = gi[i]
        r = _sig(g[:H] + whr @ h + bhr)
        z = _sig(g[H:2 * H] + whz @ h + bhz)
        n = np.tanh(g[2 * H:] + r * (whn @ h + bhn))
        h = (1 - z) * n + z * h
        sortie[i] = h
    return sortie


def inferer(p: dict, car: np.ndarray) -> np.ndarray:
    """car (K, T) -> logits (T', C) ; p = poids exportés (noms PyTorch)."""
    x = np.maximum(_conv1d(car, p["conv1.weight"], p["conv1.bias"]), 0)
    x = np.maximum(_conv1d(x, p["conv2.weight"], p["conv2.bias"], pas=2), 0)
    x = np.maximum(_conv1d(x, p["conv3.weight"], p["conv3.bias"], pas=2), 0).T   # (T', 64)
    av = _gru(x, p["gru.weight_ih_l0"], p["gru.weight_hh_l0"], p["gru.bias_ih_l0"], p["gru.bias_hh_l0"])
    ar = _gru(x[::-1], p["gru.weight_ih_l0_reverse"], p["gru.weight_hh_l0_reverse"],
              p["gru.bias_ih_l0_reverse"], p["gru.bias_hh_l0_reverse"])[::-1]
    return np.concatenate([av, ar], axis=1) @ p["sortie.weight"].T + p["sortie.bias"]
