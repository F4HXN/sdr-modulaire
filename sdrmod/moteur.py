"""Moteur de réception : thread de lecture + thread DSP.

    source ──> [lecture] ──file──> [DSP] ──> spectre (signal Qt)
                                     ├─> canal : NCO → décimation → démodulation → AGC/squelch → audio
                                     └─> bus : "iq", "audio", "niveau" (pour les modules)
"""

from __future__ import annotations

import logging
import queue
import threading

import numpy as np
from PySide6.QtCore import QObject, Signal
from scipy import signal

from .audio import SortieAudio
from .bus import Bus
from .config import TAUX_AUDIO
from .demods import Demodulateur
from .dsp import AGC, NCO, Decimateur, plan_decimation, taille_bloc
from .sources import Source

log = logging.getLogger(__name__)


class Moteur(QObject):
    spectre_pret = Signal(object)   # (dbfs: np.ndarray, centre: float, taux: int)
    niveau = Signal(float)
    erreur = Signal(str)
    en_marche = Signal(bool)

    def __init__(self, bus: Bus, modes: dict[str, type[Demodulateur]], sortie: SortieAudio | None = None):
        super().__init__()
        self.bus = bus
        self.modes = modes
        self.sortie = sortie
        self.source: Source | None = None

        # paramètres modifiables depuis l'interface (lus par le thread DSP)
        self.centre = 145_300_000.0
        self.taux = 2_400_000
        self.decalage = 0.0
        self.volume = 0.5
        self.squelch = -120.0
        self.agc_actif = True
        self.taille_fft = 4096
        self.moyennage = 0.5
        self._mode_demande: tuple[str, float] | None = None
        self._largeur_demandee: float | None = None

        self.demod: Demodulateur | None = None
        self._commandes: queue.Queue = queue.Queue()
        self._file_iq: queue.Queue = queue.Queue(maxsize=8)
        self._actif = False
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------ cycle de vie
    @property
    def actif(self) -> bool:
        return self._actif

    def demarrer(self, source: Source, mode: str, largeur: float, initialiser=None) -> None:
        """initialiser(source) est appelé après l'ouverture, avant le lancement des threads."""
        self.source = source
        source.ouvrir(self.taux, self.centre)
        if initialiser:
            initialiser(source)
        self._construire_chaine(mode, largeur)
        self._vider(self._commandes)
        self._vider(self._file_iq)
        self._actif = True
        self._threads = [
            threading.Thread(target=self._boucle_lecture, name="sdr-lecture", daemon=True),
            threading.Thread(target=self._boucle_dsp, name="sdr-dsp", daemon=True),
        ]
        for t in self._threads:
            t.start()
        if self.sortie:
            self.sortie.demarrer()
        self.en_marche.emit(True)

    def arreter(self) -> None:
        if not self._actif and not self._threads:
            return
        self._actif = False
        for t in self._threads:
            t.join(timeout=2)
        self._threads = []
        if self.sortie:
            self.sortie.arreter()
        if self.source:
            try:
                self.source.fermer()
            except Exception:
                log.exception("Fermeture de la source")
        self.en_marche.emit(False)

    # ------------------------------------------------------------ commandes
    def commande_source(self, nom: str, *args) -> None:
        """Exécute source.<nom>(*args) dans le thread de lecture."""
        if self._actif:
            self._commandes.put((nom, args))

    def regler_centre(self, f: float) -> None:
        self.centre = f
        self.commande_source("regler_frequence", f)

    def regler_mode(self, mode: str, largeur: float) -> None:
        self._mode_demande = (mode, largeur)

    def regler_largeur(self, largeur: float) -> None:
        self._largeur_demandee = largeur

    # ------------------------------------------------------------ interne
    @staticmethod
    def _vider(q: queue.Queue) -> None:
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass

    def _construire_chaine(self, mode: str, largeur: float) -> None:
        d1, d2 = plan_decimation(self.taux, TAUX_AUDIO)
        self.d1, self.d2 = d1, d2
        self.fs_inter = self.taux / d1
        self.n_bloc = taille_bloc(self.taux, d1 * d2)
        self.nco = NCO(self.taux)
        self.dec1 = Decimateur(d1, self.taux, 0.42 * self.fs_inter)
        self.dec2 = Decimateur(d2, self.fs_inter, 0.42 * TAUX_AUDIO)
        self.agc = AGC()
        self._dc = 0j
        self._moy_spectre = None
        self._fenetre = None
        self._installer_demod(mode, largeur)

    def _installer_demod(self, mode: str, largeur: float) -> None:
        classe = self.modes.get(mode) or next(iter(self.modes.values()))
        d = classe()
        d.largeur = float(min(max(largeur or d.largeur_defaut, d.largeur_min), d.largeur_max))
        if d.domaine == "intermediaire":
            d.preparer(self.fs_inter, TAUX_AUDIO, self.d2)
        else:
            d.preparer(TAUX_AUDIO, TAUX_AUDIO, 1)
        self.demod = d
        self.agc = AGC()

    def _boucle_lecture(self) -> None:
        src = self.source
        while self._actif:
            try:
                while True:
                    nom, args = self._commandes.get_nowait()
                    try:
                        getattr(src, nom)(*args)
                    except Exception as exc:
                        self.erreur.emit(f"{nom} : {exc}")
            except queue.Empty:
                pass
            try:
                x = src.lire(self.n_bloc)
            except Exception as exc:
                log.exception("Lecture source")
                self.erreur.emit(f"Lecture impossible : {exc}")
                self._actif = False
                break
            try:
                self._file_iq.put_nowait(x)
            except queue.Full:   # le DSP ne suit pas : on jette le plus ancien
                try:
                    self._file_iq.get_nowait()
                except queue.Empty:
                    pass
                self._file_iq.put_nowait(x)

    def _boucle_dsp(self) -> None:
        while self._actif:
            try:
                x = self._file_iq.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                self.traiter_bloc(x)
            except Exception as exc:
                log.exception("Traitement DSP")
                self.erreur.emit(f"DSP : {exc}")

    def traiter_bloc(self, x: np.ndarray) -> np.ndarray:
        """Traite un bloc IQ et retourne l'audio produit (public pour les tests)."""
        if self._mode_demande:
            mode, largeur = self._mode_demande
            self._mode_demande = None
            self._installer_demod(mode, largeur)
        if self._largeur_demandee is not None:
            self.demod.regler_largeur(self._largeur_demandee)
            self._largeur_demandee = None
        demod = self.demod

        # suppression de la composante continue (pic central des RTL-SDR)
        self._dc = 0.9 * self._dc + 0.1 * complex(x.mean())
        x = x - np.complex64(self._dc)

        self._spectre(x)
        if self.bus.a_des_abonnes("iq"):
            self.bus.publier("iq", x, self.centre, self.taux)

        self.nco.freq = self.decalage + demod.decalage_nco
        y = self.dec1.traiter(self.nco.melanger(x))
        if demod.domaine != "intermediaire":
            y = self.dec2.traiter(y)
        audio = demod.traiter(y)

        niveau = demod.niveau_dbfs
        audio = self.agc.traiter(audio) if self.agc_actif else audio * demod.gain_manuel
        if niveau < self.squelch:
            audio = np.zeros_like(audio)
        audio = np.clip(audio, -1.0, 1.0).astype(np.float32)

        self.bus.publier("niveau", niveau)
        self.bus.publier("audio", audio, TAUX_AUDIO)
        self.niveau.emit(niveau)
        if self.sortie:
            self.sortie.ecrire(audio * self.volume)
        return audio

    def _spectre(self, x: np.ndarray) -> None:
        n = self.taille_fft
        if self._fenetre is None or len(self._fenetre) != n:
            self._fenetre = signal.windows.blackmanharris(n).astype(np.float32)
            self._norme = float(self._fenetre.sum()) ** 2
            self._moy_spectre = None
        nseg = min(len(x) // n, 8)
        if nseg == 0:
            return
        segs = x[: nseg * n].reshape(nseg, n) * self._fenetre
        p = (np.abs(np.fft.fft(segs, axis=1)) ** 2).mean(axis=0) / self._norme
        db = 10.0 * np.log10(np.fft.fftshift(p) + 1e-20)
        if self._moy_spectre is None:
            self._moy_spectre = db
        else:
            a = self.moyennage
            self._moy_spectre = a * self._moy_spectre + (1 - a) * db
        self.spectre_pret.emit((self._moy_spectre.astype(np.float32), self.centre, self.taux))
