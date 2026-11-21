"""Source RTL-SDR (pyrtlsdr + librtlsdr)."""

from __future__ import annotations

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


class SourceRtlSdr(Source):
    nom = "RTL-SDR"

    def __init__(self, index: int = 0) -> None:
        self.index = index
        self.sdr = None
        self._ppm = 0

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
        self.sdr = RtlSdr(self.index)
        self.sdr.sample_rate = taux
        self.sdr.center_freq = int(frequence)
        self._ppm = 0

    def fermer(self) -> None:
        if self.sdr is not None:
            try:
                self.sdr.close()
            finally:
                self.sdr = None

    def lire(self, n: int) -> np.ndarray:
        brut = np.frombuffer(self.sdr.read_bytes(2 * n), dtype=np.uint8)
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
