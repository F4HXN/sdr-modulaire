# SPDX-License-Identifier: MIT
"""Pilotage rigctl (Gpredict, WSJT-X…) pour SDR Modulaire.

Le module ouvre un petit serveur compatible rigctld (Hamlib) sur 127.0.0.1:4532 : un logiciel
qui sait piloter une radio Hamlib « par le réseau » peut alors régler la fréquence et le mode
de SDR Modulaire. Exemple : Gpredict, qui envoie la fréquence corrigée de l'effet Doppler
pendant le passage d'un satellite.

Réglage dans Gpredict : Édition → Préférences → Interfaces → Radios → Ajouter :
hôte localhost, port 4532, type « RX only ». Puis, dans le contrôleur de radio, choisir le
satellite et la liaison descendante, et cliquer sur « Engage ».

Seules les connexions locales sont acceptées. Module à télécharger : placez ce fichier dans
~/.config/sdr-modulaire/modules/. Aucune dépendance en plus de SDR Modulaire.
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QListWidget, QPushButton, QSpinBox, QVBoxLayout, QWidget

from sdrmod.modules import Module

PORT_DEFAUT = 4532
# Modes Hamlib → modes SDR Modulaire, et retour.
VERS_SDR = {"FM": "NFM", "FMN": "NFM", "PKTFM": "NFM", "WFM": "WFM", "AM": "AM", "SAM": "AM", "AMS": "AM",
            "USB": "USB", "PKTUSB": "USB", "LSB": "LSB", "PKTLSB": "LSB", "CW": "CW", "CWR": "CW"}
VERS_HAMLIB = {"NFM": "FM", "WFM": "WFM", "AM": "AM", "USB": "USB", "LSB": "LSB", "CW": "CW"}
BANDES = {"NFM": 12500, "WFM": 180000, "AM": 8000, "USB": 2700, "LSB": 2700, "CW": 500}

# Réponse à \dump_state (protocole 0 de rigctld) : ce que les clients Hamlib attendent pour une radio
# en réception seule, de 100 kHz à 6 GHz.
DUMP_STATE = "\n".join([
    "0", "2", "2",
    "100000.000000 6000000000.000000 0x1ff -1 -1 0x10000003 0x0",
    "0 0 0 0 0 0 0",
    "0 0 0 0 0 0 0",
    "0x1ff 1", "0x1ff 0", "0 0",
    "0x1e 2700", "0x2 500", "0x1 8000", "0x20 15000", "0x40 180000", "0 0",
    "0", "0", "0", "0", "0", "0",
    "0x0", "0x0", "0x0", "0x0", "0x0", "0x0", ""])

# Noms longs des commandes → lettre courte.
LONGUES = {"set_freq": "F", "get_freq": "f", "set_mode": "M", "get_mode": "m", "set_vfo": "V", "get_vfo": "v",
           "set_ptt": "T", "get_ptt": "t", "set_split_vfo": "S", "get_split_vfo": "s",
           "set_split_freq": "I", "get_split_freq": "i", "set_split_mode": "X", "get_split_mode": "x",
           "get_dcd": "\x8b", "get_powerstat": "\x88", "get_info": "_", "quit": "q"}


class PilotageRigctl(Module):
    nom = "Pilotage rigctl (Gpredict)"
    description = "Serveur rigctld local : Gpredict, WSJT-X… règlent la fréquence et le mode (Doppler des satellites)."

    def demarrer(self):
        self.fichier = self.ctx.dossier_donnees("pilotage_rigctl") / "reglages.json"
        try:
            r = json.loads(self.fichier.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            r = {}
        self.port = int(r.get("port", PORT_DEFAUT))
        self.suspendu = False
        self.ordres: queue.Queue = queue.Queue()      # (genre, valeur) vers le thread de l'interface
        self.clients: dict[str, float] = {}           # adresse → dernier contact
        self.journal: list[str] = []
        self.evenements: queue.Queue = queue.Queue()  # connexions, déconnexions (vers l'interface)
        self.premiere_f = None
        self.derniere_f = None
        self.nb_ordres = 0
        self.f_cache = self.ctx.frequence()           # lus par le serveur (autre thread)
        self.mode_cache = self.ctx.mode()
        self.erreur = ""
        self._fin = threading.Event()
        self._prise = None
        self._fil = None
        self.ouvrir()
        self.abonner("vfo", self._sur_vfo)

    def _sur_vfo(self, f, mode):
        self.f_cache, self.mode_cache = f, mode

    # ------------------------------------------------------------ serveur
    def ouvrir(self):
        self.fermer()
        self._fin = threading.Event()
        try:
            prise = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            prise.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            prise.bind(("127.0.0.1", self.port))
            prise.listen(4)
            prise.settimeout(0.5)
        except OSError as exc:
            self.erreur = f"port {self.port} indisponible ({exc.strerror or exc})"
            return
        self.erreur = ""
        self._prise = prise
        self._fil = threading.Thread(target=self._accepter, name="rigctl", daemon=True)
        self._fil.start()

    def fermer(self):
        self._fin.set()
        if self._prise:
            try:
                self._prise.close()
            except OSError:
                pass
            self._prise = None

    def _accepter(self):
        prise, fin = self._prise, self._fin
        while not fin.is_set():
            try:
                conn, adresse = prise.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._servir, args=(conn, f"{adresse[0]}:{adresse[1]}", fin),
                             daemon=True).start()

    def _servir(self, conn, nom, fin):
        conn.settimeout(0.5)
        self.clients[nom] = time.monotonic()
        self.evenements.put(f"connexion de {nom}")
        raison = "le client a fermé la connexion"
        tampon = b""
        try:
            while not fin.is_set():
                try:
                    morceau = conn.recv(4096)
                except socket.timeout:
                    continue
                if not morceau:
                    break
                tampon += morceau
                while b"\n" in tampon:
                    ligne, tampon = tampon.split(b"\n", 1)
                    self.clients[nom] = time.monotonic()
                    # latin-1 : Gpredict envoie l'octet 0x8b pour get_dcd
                    texte = ligne.decode("latin-1").strip()
                    try:
                        reponse, fermer = self.executer(texte)
                    except Exception as exc:          # une commande bizarre ne coupe jamais la liaison
                        reponse, fermer = "RPRT -1\n", False
                        self.evenements.put(f"commande {texte!r} : erreur {exc}")
                    if reponse:
                        conn.sendall(reponse.encode("latin-1"))
                    if fermer:
                        raison = "le client a envoyé « q » (fin de session)"
                        return
            raison = "module arrêté ou rechargé"
        except OSError as exc:
            raison = f"erreur réseau ({exc})"
        finally:
            self.clients.pop(nom, None)
            conn.close()
            self.evenements.put(f"déconnexion de {nom} : {raison}")

    def executer(self, ligne: str) -> tuple[str, bool]:
        """Exécute une commande rigctl ; renvoie (réponse, fermer la connexion)."""
        if not ligne:
            return "", False
        etendu = ligne[0] in "+;|,"                    # protocole étendu : réponses commentées
        if etendu:
            ligne = ligne[1:].strip()
        if ligne.startswith("\\"):
            mots = ligne[1:].split()
            long = mots[0] if mots else ""
            cmd, args = LONGUES.get(long, "\\" + long), mots[1:]
        else:
            cmd, args = ligne[0], ligne[1:].split()
        rep, nom_long = self._commande(cmd, args)
        if rep is None:                                  # quitter
            return "", True
        if etendu:
            code = "RPRT 0" if not rep.startswith("RPRT") else rep.strip()
            corps = "" if rep.startswith("RPRT") else rep
            return f"{nom_long}: {' '.join(args)}\n{corps}{code}\n", False
        return rep, False

    def _commande(self, cmd: str, args: list[str]) -> tuple[str | None, str]:
        ok = "RPRT 0\n"
        if cmd == "F":
            try:
                f = float(args[0])
            except (IndexError, ValueError):
                return "RPRT -1\n", "set_freq"
            if not self.suspendu:
                self.ordres.put(("frequence", f))
                self.f_cache = f
            return ok, "set_freq"
        if cmd == "f":
            return f"{self.f_cache:.0f}\n", "get_freq"
        if cmd == "M":
            mode = VERS_SDR.get(args[0].upper()) if args else None
            if mode and not self.suspendu:
                self.ordres.put(("mode", mode))
                self.mode_cache = mode
            return (ok if mode or not args else "RPRT -1\n"), "set_mode"
        if cmd == "m":
            return f"{VERS_HAMLIB.get(self.mode_cache, 'FM')}\n{BANDES.get(self.mode_cache, 12500)}\n", "get_mode"
        if cmd in ("V", "T", "S", "I", "X", "L", "U"):
            return ok, "set"
        if cmd == "v":
            return "VFOA\n", "get_vfo"
        if cmd == "t":
            return "0\n", "get_ptt"
        if cmd == "s":
            return "0\nVFOA\n", "get_split_vfo"
        if cmd == "i":
            return f"{self.f_cache:.0f}\n", "get_split_freq"
        if cmd == "x":
            return f"{VERS_HAMLIB.get(self.mode_cache, 'FM')}\n{BANDES.get(self.mode_cache, 12500)}\n", "get_split_mode"
        if cmd in ("\x8b", "\\get_dcd"):
            return "0\n", "get_dcd"
        if cmd == "\x88":
            return "1\n", "get_powerstat"
        if cmd == "_":
            return "SDR Modulaire\n", "get_info"
        if cmd == "\\get_lock_mode":
            return "0\nRPRT 0\n", "get_lock_mode"    # Hamlib lit la valeur puis un code retour
        if cmd in ("\\set_lock_mode", "\\set_vfo_opt", "\\set_powerstat"):
            return ok, cmd[1:]
        if cmd == "\\get_vfo_info":
            return (f"{self.f_cache:.0f}\n{VERS_HAMLIB.get(self.mode_cache, 'FM')}\n"
                    f"{BANDES.get(self.mode_cache, 12500)}\n0\n0\n"), "get_vfo_info"
        if cmd == "\\dump_state":
            return DUMP_STATE, "dump_state"
        if cmd == "\\chk_vfo":
            return "0\n", "chk_vfo"
        if cmd in ("q", "Q"):
            return None, "quit"
        return "RPRT -11\n", "inconnue"            # commande non prise en charge

    # ------------------------------------------------------------ interface
    def creer_widget(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        ligne = QHBoxLayout()
        ligne.addWidget(QLabel("Port"))
        self.c_port = QSpinBox()
        self.c_port.setRange(1024, 65535)
        self.c_port.setValue(self.port)
        b_port = QPushButton("Appliquer")
        b_port.clicked.connect(self._changer_port)
        ligne.addWidget(self.c_port)
        ligne.addWidget(b_port)
        lay.addLayout(ligne)
        self.c_suspendre = QCheckBox("Suspendre le pilotage")
        self.c_suspendre.setToolTip("Ignorer les ordres de fréquence et de mode (les lectures restent servies)")
        self.c_suspendre.toggled.connect(lambda v: setattr(self, "suspendu", bool(v)))
        lay.addWidget(self.c_suspendre)
        self.statut = QLabel()
        self.statut.setWordWrap(True)
        lay.addWidget(self.statut)
        aide = QLabel("Gpredict : Préférences → Interfaces → Radios → hôte localhost, port 4532, type RX only ; "
                      "puis contrôleur de radio → Engage.")
        aide.setWordWrap(True)
        aide.setStyleSheet("color: gray")
        lay.addWidget(aide)
        self.l_journal = QListWidget()
        self.l_journal.setMaximumHeight(90)
        lay.addWidget(self.l_journal)
        self.minuterie = QTimer(w, interval=100, timeout=self._appliquer)
        self.minuterie.start()
        self._afficher()
        w.setMaximumHeight(w.sizeHint().height())
        return w

    def _changer_port(self):
        self.port = self.c_port.value()
        try:
            self.fichier.write_text(json.dumps({"port": self.port}), encoding="utf-8")
        except OSError:
            pass
        self.ouvrir()
        self._afficher()

    def _appliquer(self):
        """Thread de l'interface : on applique le dernier ordre de chaque genre."""
        while True:
            try:
                self._noter(self.evenements.get_nowait())
            except queue.Empty:
                break
        f = mode = None
        while True:
            try:
                genre, valeur = self.ordres.get_nowait()
            except queue.Empty:
                break
            self.nb_ordres += 1
            if genre == "frequence":
                f = valeur
            else:
                mode = valeur
        if mode and mode != self.ctx.mode():
            self.ctx.regler_mode(mode)
            self._noter(f"mode {mode}")
        if f is not None:
            if abs(f - self.ctx.frequence()) >= 1:
                self.ctx.regler_frequence(f)
            if self.premiere_f is None:
                self.premiere_f = f
            if self.derniere_f is None or abs(f - self.derniere_f) >= 1:
                self._noter(f"{f / 1e6:.6f} MHz")
            self.derniere_f = f
        self._afficher()

    def _noter(self, texte):
        self.journal.insert(0, f"{time.strftime('%H:%M:%S')}  {texte}")
        del self.journal[30:]
        self.l_journal.clear()
        self.l_journal.addItems(self.journal)

    def _afficher(self):
        if self.erreur:
            self.statut.setText(f"⚠ Serveur arrêté : {self.erreur}.")
            return
        clients = list(self.clients)
        etat = f"En écoute sur 127.0.0.1:{self.port}"
        etat += f" · {len(clients)} client(s) connecté(s)" if clients else " · aucun client"
        if self.derniere_f is not None:
            etat += f"\nDernière fréquence : {self.derniere_f / 1e6:.6f} MHz"
            if self.premiere_f is not None:
                etat += f" · dérive {(self.derniere_f - self.premiere_f) / 1e3:+.2f} kHz"
        if self.suspendu:
            etat += "\nPilotage suspendu : les ordres sont ignorés."
        self.statut.setText(etat)

    def arreter(self):
        self.fermer()
