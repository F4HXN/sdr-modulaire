"""Sources audio : le son déjà démodulé par un récepteur, au lieu d'IQ.

- « Son du PC » : une entrée son de ce PC (PipeWire / PulseAudio), choisie dans
  le menu Source (carte son, codec USB d'un transceiver…).
- « IC-7300 (serveur) » : l'entrée « USB Audio CODEC » de l'IC-7300, lue à
  travers le réseau sur le PC de la radio (PipeWire-Pulse en TCP 4713, le même
  accès que IC-7300 Scope --remote).

Le moteur passe alors en mode audio : pas de démodulation, spectre 0–24 kHz du
son, et les modules (décodeurs CW, SSTV, FT8, fax…) reçoivent ce son sur le
sujet « audio » du bus. Lecture par parec (paquet pulseaudio-utils).
"""

from __future__ import annotations

import json
import queue
import shutil
import socket
import subprocess
import threading
import time

import numpy as np

from .base import Source

TAUX = 48_000
PORT_PULSE = 4713
PORT_CAT = 7300                 # serveur CAT d'IC-7300 Scope (cat_server.py), CI-V brut en TCP
ADRESSE_RADIO, ADRESSE_PC = 0x94, 0xE0
# Modes CI-V de l'IC-7300 ↔ modes de SDR Modulaire (RTTY, CW-R… : laissés tels quels)
MODES_CIV = {0x00: "LSB", 0x01: "USB", 0x02: "AM", 0x03: "CW", 0x07: "CW", 0x05: "NFM"}
VERS_CIV = {"LSB": 0x00, "USB": 0x01, "AM": 0x02, "CW": 0x03, "NFM": 0x05}
# Entrée de l'IC-7300 sur le PC de la radio (même nom que dans IC-7300 Scope).
ENTREE_IC7300 = "alsa_input.usb-Burr-Brown_from_TI_USB_Audio_CODEC-00.iec958-stereo"


def lister_entrees() -> list[dict]:
    """Entrées son de ce PC : [{"nom", "description"}], sans les moniteurs de sortie."""
    try:
        r = subprocess.run(["pactl", "-f", "json", "list", "sources"],
                           capture_output=True, text=True, timeout=5)
        sources = json.loads(r.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    entrees = []
    for s in sources:
        if s["name"].endswith(".monitor"):
            continue
        description = s.get("description") or ""
        if description in ("", "(null)"):
            description = s["name"]
        entrees.append({"nom": s["name"], "description": description})
    return entrees


class SourceAudio(Source):
    """Lit du son mono 48 kHz avec parec ; lire() rend un tableau float32 réel."""

    audio = True          # le moteur ne démodule pas : le son est déjà démodulé
    ecoute = True         # renvoyer le son vers les haut-parleurs de ce PC

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None

    def _commande(self) -> list[str]:
        raise NotImplementedError

    def ouvrir(self, taux: int, frequence: float) -> None:
        if not shutil.which("parec"):
            raise RuntimeError("parec introuvable : sudo apt install pulseaudio-utils")
        self.proc = subprocess.Popen(
            ["parec", "--raw", "--format=float32le", f"--rate={TAUX}", "--channels=1",
             "--latency-msec=30", "--client-name=SDR Modulaire", *self._commande()],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:                      # serveur injoignable, entrée inconnue : parec s'arrête aussitôt
            self.proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            return
        erreur = self.proc.stderr.read().decode(errors="replace").strip()
        self.proc = None
        raise RuntimeError(erreur or "parec s'est arrêté")

    def fermer(self) -> None:
        if self.proc:
            self.proc.kill()
            self.proc.wait()
            self.proc = None

    def lire(self, n: int) -> np.ndarray:
        donnees = self.proc.stdout.read(4 * n)
        if len(donnees) < 4 * n:
            erreur = self.proc.stderr.read().decode(errors="replace").strip()
            raise RuntimeError(erreur or "le son s'est arrêté")
        return np.frombuffer(donnees, dtype=np.float32)

    def regler_frequence(self, frequence: float) -> None:
        pass              # la fréquence se règle sur le récepteur lui-même


class SourceSonPC(SourceAudio):
    nom = "Son du PC"
    # Le son vient souvent du micro ou du récepteur posé à côté : le renvoyer aux
    # haut-parleurs ferait un larsen. Les modules le reçoivent quand même.
    ecoute = False

    def __init__(self, entree: str = "") -> None:
        super().__init__()
        self.entree = entree

    def _commande(self) -> list[str]:
        return [f"--device={self.entree}"] if self.entree else []

    def infos(self) -> dict:
        noms = {e["nom"]: e["description"] for e in lister_entrees()}
        return {"modele": "Son du PC", "bias_t": False, "echantillonnage_direct": False, "tuner": noms.get(self.entree, self.entree or "entrée par défaut"),
                "remarque": "Son déjà démodulé (48 kHz) : pas d'accord ni de démodulation ici, "
                            "seuls les modules l'exploitent. Pas de renvoi aux haut-parleurs (larsen)."}


def trame(cmd: int, donnees: bytes = b"") -> bytes:
    return bytes([0xFE, 0xFE, ADRESSE_RADIO, ADRESSE_PC, cmd]) + donnees + b"\xfd"


def bcd_frequence(f: float) -> bytes:
    chiffres = f"{int(round(f)):010d}"
    return bytes(int(chiffres[i - 1]) << 4 | int(chiffres[i]) for i in range(9, 0, -2))


def frequence_bcd(octets: bytes) -> int:
    return int("".join(f"{b >> 4}{b & 15}" for b in reversed(octets)))


class SourceIC7300(SourceAudio):
    """Son de l'IC-7300 + synchronisation de la fréquence et du mode par le serveur CAT.

    Le serveur CAT accepte SDR Modulaire en second à côté d'IC-7300 Scope : réglage de
    la fréquence et du mode permis, émission réservée au pilote."""

    nom = "IC-7300 (serveur)"
    INTERVALLE = 0.4            # s entre deux lectures de la fréquence et du mode
    SILENCE = 1.2               # s pendant lesquels une valeur réglée ici prime sur celle relue

    def __init__(self, hote: str) -> None:
        super().__init__()
        self.hote = hote
        # Lus par la fenêtre (thread GUI) : dernière fréquence (Hz) et mode connus de la radio.
        self.frequence_radio: int | None = None
        self.mode_radio: str | None = None
        self.etat_cat = "non relié"
        self._envois: queue.Queue = queue.Queue()
        self._t_local = 0.0
        self._actif = False
        self._fil: threading.Thread | None = None

    def _commande(self) -> list[str]:
        return [f"--server=tcp:{self.hote}:{PORT_PULSE}", f"--device={ENTREE_IC7300}"]

    def ouvrir(self, taux: int, frequence: float) -> None:
        super().ouvrir(taux, frequence)
        self._actif = True
        self._fil = threading.Thread(target=self._boucle_cat, name="ic7300-cat", daemon=True)
        self._fil.start()

    def fermer(self) -> None:
        self._actif = False
        if self._fil:
            self._fil.join(timeout=2)
            self._fil = None
        super().fermer()

    # appelées dans le thread de lecture du moteur (commande_source)
    def regler_frequence(self, frequence: float) -> None:
        f = int(round(frequence))
        if f <= 0 or f == self.frequence_radio:
            return
        self._t_local = time.monotonic()
        self.frequence_radio = f
        self._envois.put(trame(0x05, bcd_frequence(f)))

    def regler_mode(self, mode: str) -> None:
        code = VERS_CIV.get(mode)
        if code is None or mode == self.mode_radio:
            return
        self._t_local = time.monotonic()
        self.mode_radio = mode
        self._envois.put(trame(0x06, bytes([code])))

    # ------------------------------------------------------------ CI-V
    def _boucle_cat(self) -> None:
        while self._actif:
            try:
                with socket.create_connection((self.hote, PORT_CAT), timeout=3) as conn:
                    conn.sendall(b"SECOND\n")   # jamais pilote : l'émission reste à IC-7300 Scope
                    conn.settimeout(0.1)
                    self.etat_cat = "relié"
                    self._servir(conn)
            except OSError as exc:
                self.etat_cat = f"injoignable ({exc.strerror or exc})"
            if self.etat_cat == "relié":
                self.etat_cat = "refusé ou coupé par le serveur"
            fin = time.monotonic() + 5
            while self._actif and time.monotonic() < fin:
                time.sleep(0.2)

    def _servir(self, conn: socket.socket) -> None:
        tampon, prochaine = b"", 0.0
        while self._actif:
            try:
                while True:
                    conn.sendall(self._envois.get_nowait())
            except queue.Empty:
                pass
            if time.monotonic() >= prochaine:
                conn.sendall(trame(0x03) + trame(0x04))
                prochaine = time.monotonic() + self.INTERVALLE
            try:
                donnees = conn.recv(65536)
            except socket.timeout:
                continue
            if not donnees:
                return              # serveur parti, ou déjà trois PC connectés
            *trames, tampon = (tampon + donnees).split(b"\xfd")
            tampon = tampon[-8192:]
            for t in trames:
                self._lire_trame(t.lstrip(b"\xfe"))

    def _lire_trame(self, corps: bytes) -> None:
        """Réponses de la radio (vers n'importe quel PC) et annonces « transceive » (vers 00)."""
        if len(corps) < 3 or corps[1] != ADRESSE_RADIO:
            return
        if time.monotonic() - self._t_local < self.SILENCE:
            return                  # réglage local en cours : ne pas revenir en arrière
        cmd, donnees = corps[2], corps[3:]
        if cmd in (0x00, 0x03) and len(donnees) == 5:
            self.frequence_radio = frequence_bcd(donnees)
        elif cmd in (0x01, 0x04) and len(donnees) >= 1 and donnees[0] in MODES_CIV:
            self.mode_radio = MODES_CIV[donnees[0]]

    def infos(self) -> dict:
        return {"modele": "IC-7300", "bias_t": False, "echantillonnage_direct": False,
                "tuner": f"serveur {self.hote} (son {PORT_PULSE}, CAT {PORT_CAT})",
                "plages": [(30e3, 74.8e6)],
                "remarque": "Son reçu par l'IC-7300 (48 kHz), lu sur le PC de la radio. Fréquence et mode "
                            "synchronisés avec la radio par le serveur CAT (en second d'IC-7300 Scope : "
                            "pas d'émission depuis SDR Modulaire)."}
