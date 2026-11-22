"""Vérification de signatures Ed25519 (RFC 8032), en Python pur.

Sert à authentifier le catalogue des modules de rtl-sdr.fr : il est signé hors ligne par
l'auteur (script signer_catalogue.py du dépôt du site) et l'application refuse d'installer
quoi que ce soit si la signature ne correspond pas à CLE_PUBLIQUE_CATALOGUE.

Seule la vérification est implémentée (aucun secret ici) : elle ne dépend que de la
bibliothèque standard, inutile d'ajouter une dépendance pour une signature par catalogue.
Contrôles stricts : S < L, points canoniques et sur la courbe, équation [S]B = R + [k]A
vérifiée sans facteur de cofacteur (comme libsodium et la bibliothèque cryptography).
"""

from __future__ import annotations

import base64
import binascii
import hashlib

# Clé publique de l'auteur (base64 de 32 octets). La clé privée ne quitte pas son poste.
CLE_PUBLIQUE_CATALOGUE = "FDDkVzjGT6e6cL6r9FvkmTTiBnr5aFKhFOwP2WqOXfE="

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_RACINE_M1 = pow(2, (_P - 1) // 4, _P)                 # racine carrée de -1 modulo p
_BY = 4 * pow(5, _P - 2, _P) % _P
_BX = None                                             # calculé à l'import (décompression de B)


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


# Points en coordonnées étendues (X, Y, Z, T) avec x = X/Z, y = Y/Z, xy = T/Z.
def _addition(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    A = (y1 - x1) * (y2 - x2) % _P
    B = (y1 + x1) * (y2 + x2) % _P
    C = 2 * t1 * t2 * _D % _P
    D = 2 * z1 * z2 % _P
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _multiplier(k: int, pt):
    resultat = (0, 1, 1, 0)                           # élément neutre
    while k:
        if k & 1:
            resultat = _addition(resultat, pt)
        pt = _addition(pt, pt)
        k >>= 1
    return resultat


def _egaux(a, b) -> bool:
    x1, y1, z1, _ = a
    x2, y2, z2, _ = b
    return (x1 * z2 - x2 * z1) % _P == 0 and (y1 * z2 - y2 * z1) % _P == 0


def _retrouver_x(y: int, signe: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1) % _P
    if x2 == 0:
        return None if signe else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _RACINE_M1 % _P
    if (x * x - x2) % _P:
        return None                                    # pas sur la courbe
    if x & 1 != signe:
        x = _P - x
    return x


def _decompresser(octets: bytes):
    if len(octets) != 32:
        return None
    y = int.from_bytes(octets, "little")
    signe = y >> 255
    y &= (1 << 255) - 1
    x = _retrouver_x(y, signe)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


_BX = _retrouver_x(_BY, 0)
_B = (_BX, _BY, 1, _BX * _BY % _P)


def verifier(cle_publique: bytes, message: bytes, signature: bytes) -> bool:
    """True si signature (64 octets) est une signature Ed25519 valide de message par cle_publique (32 octets)."""
    if len(cle_publique) != 32 or len(signature) != 64:
        return False
    A = _decompresser(cle_publique)
    R = _decompresser(signature[:32])
    if A is None or R is None:
        return False
    S = int.from_bytes(signature[32:], "little")
    if S >= _L:
        return False                                   # signature malléable : refusée
    k = int.from_bytes(hashlib.sha512(signature[:32] + cle_publique + message).digest(), "little") % _L
    return _egaux(_multiplier(S, _B), _addition(R, _multiplier(k, A)))


def verifier_catalogue(donnees: bytes, signature_b64: bytes | str) -> None:
    """Lève ValueError si le catalogue n'est pas signé par la clé de l'auteur."""
    try:
        signature = base64.b64decode(signature_b64.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("signature du catalogue illisible") from exc
    if not verifier(base64.b64decode(CLE_PUBLIQUE_CATALOGUE), donnees, signature):
        raise ValueError("signature du catalogue invalide : catalogue refusé")
