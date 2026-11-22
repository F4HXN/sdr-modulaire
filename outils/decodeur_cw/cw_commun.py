"""Éléments communs à l'entraînement et au module : alphabet, Morse, prétraitement.

Le prétraitement DOIT être identique dans le module (copié tel quel).
"""

import numpy as np

TAUX = 8000            # Hz : taux de travail du décodeur
NFFT = 128             # 62,5 Hz par case, fenêtre de 16 ms
PAS = 32               # 4 ms par trame
K = 8                  # cases gardées autour de la tonalité (500 Hz)
BANDE = (300.0, 1500.0)

MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.", "H": "....",
    "I": "..", "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.", "O": "---", "P": ".--.",
    "Q": "--.-", "R": ".-.", "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-", "5": ".....",
    "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    "/": "-..-.", "?": "..--..", ".": ".-.-.-", ",": "--..--", "=": "-...-", "+": ".-.-.",
}
# index 0 = « blanc » CTC, 1 = espace entre mots
ALPHABET = ["_", " "] + list(MORSE)
INDEX = {c: i for i, c in enumerate(ALPHABET)}

_FENETRE = np.hanning(NFFT).astype(np.float32)
_CASE_MIN = int(BANDE[0] / (TAUX / NFFT))
_CASE_MAX = int(BANDE[1] / (TAUX / NFFT))


def spectrogramme(x: np.ndarray) -> np.ndarray:
    """Puissance |STFT|², forme (cases, trames)."""
    n = 1 + (len(x) - NFFT) // PAS
    idx = np.arange(NFFT)[None, :] + PAS * np.arange(n)[:, None]
    trames = x[idx] * _FENETRE
    return (np.abs(np.fft.rfft(trames, axis=1)) ** 2).T.astype(np.float32)


def pretraiter(x: np.ndarray) -> tuple[np.ndarray, float]:
    """Audio 8 kHz -> (caractéristiques K×T normalisées, fréquence de la tonalité en Hz)."""
    p = spectrogramme(x.astype(np.float32))
    moy = p[_CASE_MIN:_CASE_MAX].mean(axis=1)
    moy = np.convolve(moy, np.ones(3) / 3, mode="same")
    case = _CASE_MIN + int(np.argmax(moy))
    bas = min(max(case - K // 2, 0), p.shape[0] - K)
    zone = np.log10(p[bas:bas + K] + 1e-9)
    med = np.median(zone)
    ecart = zone.std() + 1e-6
    car = np.clip((zone - med) / ecart, -3.0, 8.0).astype(np.float32)
    return car, case * TAUX / NFFT


def decoder_ctc(sorties: np.ndarray) -> list[tuple[int, str]]:
    """Décodage glouton CTC : sorties (T, C) -> [(trame, caractère)]."""
    meilleur = sorties.argmax(axis=1)
    res, prec = [], 0
    for t, c in enumerate(meilleur):
        if c != prec and c != 0:
            res.append((t, ALPHABET[c]))
        prec = c
    return res
