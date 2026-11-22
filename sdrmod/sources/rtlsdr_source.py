"""Source RTL-SDR (pyrtlsdr + librtlsdr).

Lecture continue (asynchrone) : librtlsdr garde plusieurs transferts USB en vol, comme rtl_fm ;
un fil reçoit les tampons dans une réserve où lire() prélève ses blocs. Avec des lectures
synchrones successives, les échantillons arrivés entre deux lectures étaient perdus : sans
effet audible en FM ou en ADS-B, mais assez pour rendre illisibles les trames APRS, plus
longues qu'un bloc.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

import numpy as np

from .base import Source

# Caractéristiques connues des tuners (plages en Hz, sans échantillonnage direct)
TUNERS = {
    0: ("Inconnu", [], "Tuner non identifié par librtlsdr."),
    1: ("Elonics E4000", [(52e6, 1_100e6), (1_250e6, 2_200e6)],
        "Trou de réception vers 1100–1250 MHz."),
    2: ("Fitipower FC0012", [(22e6, 948.6e6)], "Ancien tuner TNT, sensibilité modeste."),
    3: ("Fitipower FC0013", [(22e6, 1_100e6)],
        "Ancien tuner TNT : gain max ~20 dB, pas de HF, pas de bias-T."),
    4: ("FCI FC2580", [(146e6, 308e6), (438e6, 924e6)], "Deux bandes séparées."),
    5: ("Rafael R820T/R820T2", [(24e6, 1_766e6)],
        "Tuner le plus courant. HF possible en échantillonnage direct (branche Q) sur RTL-SDR Blog V3."),
    6: ("Rafael R828D", [(24e6, 1_766e6)],
        "Sur RTL-SDR Blog V4 : HF 0,5–28,8 MHz par convertisseur intégré (pilote rtl-sdr-blog requis)."),
}
TAUX_MAX_STABLE = 2_400_000
TAMPON_USB = 16 * 16_384        # octets par transfert asynchrone (multiple de 512, ~55 ms à 2,4 MS/s)
RESERVE_MAX_S = 2.0             # au-delà, le traitement a pris trop de retard : on jette le plus ancien
log = logging.getLogger(__name__)


def lister_cles() -> list[dict]:
    """Clés RTL-SDR branchées, sans les ouvrir (marche aussi pour une clé déjà en service)."""
    try:
        from ctypes import c_ubyte

        from rtlsdr.librtlsdr import librtlsdr
        nombre = int(librtlsdr.rtlsdr_get_device_count())
    except Exception:
        return []
    cles = []
    for i in range(nombre):
        fabricant = produit = serie = nom = ""
        try:
            tampons = [(c_ubyte * 256)() for _ in range(3)]
            if librtlsdr.rtlsdr_get_device_usb_strings(i, *tampons) == 0:
                fabricant, produit, serie = ("".join(chr(b) for b in t if b > 0).strip() for t in tampons)
            brut = librtlsdr.rtlsdr_get_device_name(i)
            nom = brut.decode(errors="replace") if isinstance(brut, bytes) else str(brut or "")
        except Exception:
            pass
        cles.append({"index": i, "fabricant": fabricant, "produit": produit, "serie": serie, "nom": nom})
    return cles


def libelle_cle(cle: dict, cles: list[dict]) -> str:
    """Texte du menu ; la position USB est ajoutée si deux clés ont le même numéro de série."""
    texte = f"RTL-SDR — {cle['produit'] or cle['nom'] or 'RTL2832U'}"
    if cle["serie"]:
        texte += f" n° {cle['serie']}"
    if sum(c["serie"] == cle["serie"] for c in cles) > 1:
        texte += f" (clé {cle['index'] + 1})"
    return texte


def choisir_index(cles: list[dict], serie: str, index: int) -> int:
    """Retrouve la clé mémorisée : par numéro de série, ou par position s'il est en double."""
    if not serie:
        return index
    memes = [c["index"] for c in cles if c["serie"] == serie]
    if not memes:
        branchees = ", ".join(c["serie"] or "?" for c in cles) or "aucune"
        raise RuntimeError(f"La clé n° {serie} n'est pas branchée (clés présentes : {branchees}).")
    return index if index in memes else memes[0]


class SourceRtlSdr(Source):
    nom = "RTL-SDR"

    def __init__(self, index: int = 0, serie: str = "") -> None:
        self.index = index
        self.serie = serie
        self.sdr = None
        self._ppm = 0
        self._reserve: deque[np.ndarray] = deque()
        self._octets = 0
        self._cond = threading.Condition()
        self._fil: threading.Thread | None = None
        self._erreur: Exception | None = None
        self.perdus = 0                 # octets jetés faute d'avoir été lus à temps
        self._dernier_avis = 0.0

    def ouvrir(self, taux: int, frequence: float) -> None:
        try:
            from rtlsdr import RtlSdr
        except Exception as exc:  # bibliothèque absente ou incompatible
            if "rtlsdr_set_dithering" in str(exc):
                raise RuntimeError(
                    "pyrtlsdr est trop récent pour la librtlsdr du système.\n"
                    "Corrige avec : pip install \"pyrtlsdr==0.2.93\" \"setuptools<81\""
                ) from exc
            if "pkg_resources" in str(exc):
                raise RuntimeError(
                    "pyrtlsdr 0.2.x a besoin de pkg_resources.\n"
                    "Corrige avec : pip install \"setuptools<81\""
                ) from exc
            raise RuntimeError(
                f"pyrtlsdr/librtlsdr indisponible : {exc}\n"
                "Installe le paquet système « rtl-sdr » (ou le pilote rtl-sdr-blog pour une V4) "
                "et « pip install pyrtlsdr »."
            ) from exc
        self.index = choisir_index(lister_cles(), self.serie, self.index)
        self.sdr = RtlSdr(self.index)
        self.sdr.sample_rate = taux
        self.sdr.center_freq = int(frequence)
        self._ppm = 0
        self._taux = taux
        self._reserve.clear()
        self._octets = 0
        self._erreur = None
        self._fil = threading.Thread(target=self._lecture_continue, name="rtlsdr-async", daemon=True)
        self._fil.start()

    def _lecture_continue(self) -> None:
        try:
            self.sdr.read_bytes_async(self._recu, TAMPON_USB)
        except Exception as exc:           # clé débranchée, erreur USB
            self._erreur = exc
        finally:
            with self._cond:
                self._cond.notify_all()

    def _recu(self, valeurs, _contexte) -> None:
        bloc = np.frombuffer(valeurs, dtype=np.uint8).copy()     # le tampon de librtlsdr est réutilisé
        with self._cond:
            self._reserve.append(bloc)
            self._octets += len(bloc)
            limite = int(2 * self._taux * RESERVE_MAX_S)
            while self._octets > limite and len(self._reserve) > 1:
                jete = self._reserve.popleft()
                self._octets -= len(jete)
                self.perdus += len(jete)
            self._cond.notify_all()
        if self.perdus and time.monotonic() - self._dernier_avis > 5:     # un avis toutes les 5 s au plus
            self._dernier_avis = time.monotonic()
            log.warning("Traitement en retard : %d octets IQ jetés au total", self.perdus)

    def fermer(self) -> None:
        if self.sdr is not None:
            try:
                if self._fil is not None and self._fil.is_alive():
                    self.sdr.cancel_read_async()
                    self._fil.join(timeout=2)
            except Exception:
                log.exception("Arrêt de la lecture continue")
            try:
                self.sdr.close()
            finally:
                self.sdr = None
                self._fil = None

    def lire(self, n: int) -> np.ndarray:
        voulu = 2 * n
        with self._cond:
            if not self._cond.wait_for(lambda: self._octets >= voulu or self._erreur is not None
                                       or self._fil is None or not self._fil.is_alive(), timeout=3.0):
                raise RuntimeError("La clé n'envoie plus d'échantillons")
            if self._octets < voulu:
                raise RuntimeError("Lecture interrompue : " + (str(self._erreur) if self._erreur else "la clé a cessé d'envoyer"))
            morceaux, reste = [], voulu
            while reste:
                b = self._reserve[0]
                if len(b) <= reste:
                    morceaux.append(self._reserve.popleft())
                    reste -= len(b)
                else:
                    morceaux.append(b[:reste])
                    self._reserve[0] = b[reste:]
                    reste = 0
            self._octets -= voulu
        brut = np.concatenate(morceaux)
        iq = (brut.astype(np.float32) - 127.4) * (1.0 / 128.0)
        return iq.view(np.complex64)

    def regler_frequence(self, frequence: float) -> None:
        self.sdr.center_freq = int(frequence)

    def regler_gain(self, gain: float | None) -> None:
        self.sdr.gain = "auto" if gain is None else float(gain)

    def gains(self) -> list[float]:
        try:
            return [float(g) for g in self.sdr.valid_gains_db]
        except Exception:
            return []

    def regler_ppm(self, ppm: int) -> None:
        # librtlsdr renvoie une erreur si la valeur ne change pas
        if int(ppm) != self._ppm:
            self.sdr.freq_correction = int(ppm)
            self._ppm = int(ppm)

    def regler_echantillonnage_direct(self, mode: int) -> None:
        self.sdr.set_direct_sampling(int(mode))

    def _chaines_usb(self) -> tuple[str, str, str]:
        from ctypes import c_ubyte

        from rtlsdr.librtlsdr import librtlsdr
        tampons = [(c_ubyte * 256)() for _ in range(3)]
        if librtlsdr.rtlsdr_get_device_usb_strings(self.index, *tampons) != 0:
            return ("", "", "")
        return tuple("".join(chr(b) for b in t if b > 0).strip() for t in tampons)

    def infos(self) -> dict:
        fabricant = produit = serie = puce = ""
        try:
            fabricant, produit, serie = self._chaines_usb()
        except Exception:
            pass
        try:
            from rtlsdr.librtlsdr import librtlsdr
            nom = librtlsdr.rtlsdr_get_device_name(self.index)
            puce = nom.decode(errors="replace") if isinstance(nom, bytes) else str(nom or "")
        except Exception:
            pass
        try:
            code = int(self.sdr.get_tuner_type())
        except Exception:
            code = 0
        tuner, plages, remarque = TUNERS.get(code, TUNERS[0])

        blog = "rtlsdrblog" in (fabricant + produit).replace(" ", "").replace("-", "").lower()
        v4 = blog and code == 6
        if v4:
            plages = [(0.5e6, 28.8e6)] + plages
        return {
            "fabricant": fabricant,
            "modele": produit or puce or "RTL2832U",
            "puce": puce,
            "numero_serie": serie,
            "tuner": tuner,
            "plages": plages,
            "gains": self.gains(),
            "taux_max": TAUX_MAX_STABLE,
            # bias-T : seules les clés RTL-SDR Blog en ont un ; sinon on ne sait pas
            "bias_t": True if blog else (False if code in (2, 3, 4) else None),
            # échantillonnage direct : utile seulement avec R820T/R828D modifiés (V3) ; inutile en V4
            "echantillonnage_direct": False if code in (1, 2, 3, 4) or v4 else None,
            "remarque": remarque,
        }

    def regler_bias_t(self, actif: bool) -> None:
        if not hasattr(self.sdr, "set_bias_tee"):
            raise RuntimeError("Cette version de pyrtlsdr ne gère pas le bias-T")
        self.sdr.set_bias_tee(bool(actif))
