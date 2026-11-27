# SDR Modulaire

Récepteur SDR pour Linux, écrit en Python, construit autour d'un cœur minimal
(source → waterfall → démodulation → audio) auquel on **ajoute ou retire des
modules à chaud**, sans redémarrer.

![Réception d'une station FM avec les modules Stéréo et RDS, S-mètre et Enregistreur audio](docs/captures/fenetre-principale.png)

*Réception d'une station FM avec une clé RTL-SDR (tuner FC0013), modules Stéréo et RDS, S-mètre et Enregistreur audio.*

## Ce que fait la base

- Spectre + waterfall (FFT 1024 à 16384, moyenne, plancher/plafond, échelle auto)
- Afficheur de fréquence façon transceiver : molette sur un chiffre, clic haut/bas, double-clic pour saisir
- Accord par clic sur le spectre ou le waterfall (calé sur le pas), molette = ± 1 pas, ↑/↓ et PgPréc/PgSuiv au clavier
- Modes : AM, NFM, WFM (mono, désaccentuation 50 µs), USB, LSB, CW (note 700 Hz)
- Bande passante réglable par mode (mémorisée), AGC audio, squelch en dBFS, volume
- Détection de la clé : modèle, tuner, couverture, paliers de gain, taux max ; options non prises en charge grisées, fréquence en rouge hors couverture (menu Source → Informations sur le matériel)
- RTL-SDR : taux d'échantillonnage, gain RF (auto ou manuel), correction ppm, échantillonnage direct I/Q (HF sur V3), bias-T
- Saturation de la clé affichée en continu (part des échantillons écrêtés, en rouge au-delà de 0,05 %) et bouton « Ajuster » : gain le plus fort sans saturation, trouvé par dichotomie sur les paliers du tuner (~6 s), puis niveaux du spectre recalés ; même chose dans SDR Web
- Sources de son déjà démodulé (menu Source) : **Son du PC**, une entrée son au choix dans une liste (carte son, codec USB d'un transceiver…), ou **IC-7300 (serveur)**, le son de l'IC-7300 lu sur le PC de la radio (PipeWire-Pulse TCP 4713, serveur d'IC-7300 Scope). Pas de démodulation : spectre du son de 0 à 3, 4, 6, 12 ou 24 kHz, et les modules (CW, SSTV, FT8, fax…) reçoivent ce son. Avec l'IC-7300, fréquence et mode synchronisés dans les deux sens par le serveur CAT (port 7300), en second d'IC-7300 Scope : l'émission reste à IC-7300 Scope
- Convertisseur HF (Ham It Up…) : décalage en MHz dans les réglages ; l'afficheur et le spectre montrent la vraie fréquence HF, la clé est accordée sur fréquence + décalage
- Réglages, modules actifs et disposition des fenêtres retrouvés au lancement suivant

## Installation (Debian / Ubuntu)

```bash
sudo apt install rtl-sdr librtlsdr-dev libportaudio2
python3 -m venv ~/.venv/sdrmod && . ~/.venv/sdrmod/bin/activate
pip install -r requirements.txt
```

- **RTL-SDR Blog V4** : il faut le pilote `rtl-sdr-blog` (le paquet de la distribution ne gère pas le tuner R828D de la V4).
- Si l'import de pyrtlsdr échoue avec `rtlsdr_set_dithering` : `pip install pyrtlsdr==0.2.93`.
- Pense à blacklister `dvb_usb_rtl28xxu` si la clé est prise par le pilote TNT.

### Ajouter des modules

Menu **Modules → Ajouter des modules…** : les modules proposés sur rtl-sdr.fr s'installent
(et se mettent à jour) en un clic dans `~/.config/sdr-modulaire/modules/`.

Le catalogue (`https://rtl-sdr.fr/catalogue-modules.json`) est **signé en Ed25519** par l'auteur
(`catalogue-modules.json.sig`) ; la clé publique est écrite dans l'application (`sdrmod/signature.py`).
Avant toute installation, l'application vérifie :

- la signature du catalogue : invalide, absente ou illisible → catalogue refusé, rien n'est installable ;
- l'adresse de téléchargement : seuls les fichiers sous `https://rtl-sdr.fr/` sont acceptés ;
- l'empreinte SHA-256 de chaque module, annoncée par le catalogue signé ;
- le nom de fichier, la taille et la syntaxe Python.

« Installer depuis un fichier… » reste possible pour un module local : c'est alors vous qui lui faites confiance.

![Gestionnaire d'ajout de modules](docs/captures/ajout-modules.png)

## Lancement

Depuis le dossier du projet :

```bash
python3 -m sdrmod                # clé RTL-SDR
python3 -m sdrmod --debug        # avec un journal détaillé
```

## Architecture

```
source (thread lecture) ──file──> moteur (thread DSP)
                                    ├─ spectre ──────────────────────> waterfall (Qt)
                                    ├─ NCO → décimation 1 (~240 kHz) → [WFM]
                                    │        → décimation 2 (48 kHz) → [AM NFM USB LSB CW]
                                    │        → AGC / squelch → audio
                                    └─ bus : "iq", "audio", "niveau", "vfo" ──> modules
```

Les taux proposés sont tous des multiples de 48 kHz : la décimation est entière
de bout en bout, sans rééchantillonnage fractionnaire. À 2,4 MS/s, le DSP prend
environ 13 % d'un cœur.

```
sdrmod/
  moteur.py        threads lecture + DSP, publication sur le bus
  dsp.py           NCO, FIR/IIR à état, décimateurs, AGC
  bus.py           publication / abonnement
  modules.py       Module, Contexte, GestionnaireModules
  signature.py     vérification Ed25519 du catalogue des modules (Python pur)
  sources/         rtlsdr_source.py (une classe Source par matériel),
                   audio_source.py (son du PC, IC-7300 par le réseau, synchro CI-V),
                   simulation.py (émetteurs fictifs, pour les tests automatiques seulement)
  demods/          am.py, fm.py, blu.py              (un fichier = un ou plusieurs modes)
  ui/              fenêtre, waterfall, afficheur, panneau, dialogue modules
modules/           smetre.py, enregistreur_wav.py, memoires.py, journal_activite.py,
                   capteurs_433.py, stereo_rds_fm.py
modules-a-telecharger/  adsb.py, scanner_memoires.py, scanner_frequences.py,
                        pilotage_rigctl.py, sstv.py, decodeur_cw.py, decodeur_aprs.py (non chargés d'office : à copier dans ~/.config/sdr-modulaire/modules/)
outils/decodeur_cw/     entraînement du réseau du Décodeur CW (PyTorch, non nécessaire à l'exécution)
tests/             test_dsp.py, test_interface.py, test_materiel.py, test_catalogue.py, test_sstv.py, test_decodeur_cw.py, test_decodeur_aprs.py, test_decodeur_dmr.py, test_tonalites.py, test_enregistreur_iq.py, test_decodeur_acars.py, test_fax_meteo.py, test_decodeur_ais.py, test_gain.py, test_memoires.py
```

## Trois niveaux d'extension

Guide complet pour écrire un module (règles, bus, contexte, texte à donner à une IA) :
[MODULES.md](MODULES.md). Modèle à copier : `modules/_modele.py`.

**1. Ajouter un module** (le plus courant) : déposer un `.py` dans `modules/` ou
`~/.config/sdr-modulaire/modules/`, puis *Modules → Gérer les modules → Rescanner*.
Cocher pour l'ajouter, décocher (ou fermer son panneau) pour le retirer.

```python
from sdrmod.modules import Module

class MonModule(Module):
    nom = "Mon module"
    description = "Ce qu'il fait."

    def demarrer(self):
        self.abonner("audio", self.sur_audio)    # désabonnement automatique au retrait

    def sur_audio(self, echantillons, taux):     # thread DSP : rester rapide
        ...

    def creer_widget(self):                      # facultatif : panneau à droite
        ...

    def arreter(self):                           # fermer fichiers, sockets…
        ...
```

Sujets disponibles : `iq` (complex64, centre, taux), `audio` (float32 48 kHz,
taux), `niveau` (dBFS), `vfo` (fréquence, mode). Le contexte `self.ctx` permet de
lire et piloter le récepteur : `frequence()`, `mode()`, `regler_frequence()`,
`regler_mode()`, `dossier_donnees()`.

Un module qui lève une exception est retiré automatiquement ; le récepteur continue.
Le bouton *Recharger le module* relit son fichier : pratique pendant le développement.

**2. Ajouter un mode** : un fichier dans `sdrmod/demods/` avec une sous-classe de
`Demodulateur` (`nom`, largeurs, `construire()`, `traiter()`). Il apparaît dans la liste au lancement.

**3. Ajouter un matériel** : une sous-classe de `Source` dans `sdrmod/sources/`,
déclarée dans `SOURCES` (`sources/__init__.py`).

## Tests

```bash
python3 -m tests.test_dsp                                            # chaque mode sur signaux simulés
QT_QPA_PLATFORM=offscreen python3 -m tests.test_interface capture.png # interface + modules à chaud
QT_QPA_PLATFORM=offscreen python3 -m tests.test_materiel capture.png  # affichage du matériel (clé FC0013 émulée)
QT_QPA_PLATFORM=offscreen python3 -m tests.test_catalogue             # signature Ed25519 et adresse du catalogue
python3 -m tests.test_sstv                                            # décodeur SSTV sur signaux synthétiques
python3 -m tests.test_decodeur_cw                                     # Décodeur CW de bout en bout (IQ → mode CW → texte)
python3 -m tests.test_decodeur_aprs                                   # Décodeur APRS sur trames AFSK synthétiques
QT_QPA_PLATFORM=offscreen python3 -m tests.test_decodeur_dmr dmr.wav  # Décodeur DMR sur un flux de relais synthétique
QT_QPA_PLATFORM=offscreen python3 -m tests.test_tonalites          # CTCSS, DTMF et 1750 Hz sur des émissions NFM synthétiques
QT_QPA_PLATFORM=offscreen HOME=$(mktemp -d) python3 -m tests.test_enregistreur_iq  # formats IQ, enregistrement et relecture dans l'appli
QT_QPA_PLATFORM=offscreen python3 -m tests.test_decodeur_acars      # ACARS : MSK synthétique, corrections, 3 canaux ensemble
QT_QPA_PLATFORM=offscreen python3 -m tests.test_fax_meteo carte.png # fax météo : départ, phasage, pente, arrêt, carte comparée point par point
QT_QPA_PLATFORM=offscreen python3 -m tests.test_decodeur_ais        # AIS : messages de gpsd, banc GMSK de 40 salves par niveau, 2 canaux
QT_QPA_PLATFORM=offscreen HOME=$(mktemp -d) python3 -m tests.test_gain  # saturation et bouton « Ajuster » (clé simulée qui écrête)
QT_QPA_PLATFORM=offscreen HOME=$(mktemp -d) python3 -m tests.test_memoires  # mémoires, CSV vérifié par CHIRP s'il est installé
```

## Modules disponibles

Fournis avec l'application : S-mètre (cadran à aiguille et barre de LED, comme dans SDR Web), Mémoires, Enregistreur audio (WAV), Journal d'activité,
Capteurs 433 MHz, Stéréo et RDS en FM.

Le module **Mémoires** gère les fréquences favorites : tableau numéroté (les mêmes CH que le
scanner de mémoires), recherche, groupes, ajout / modification / suppression, double-clic pour
écouter. Tout est enregistré automatiquement ; « Sauvegarder… » écrit un CSV compatible CHIRP
(ou un JSON) et « Importer… » relit ces fichiers, ceux de CHIRP et ceux de SDR Web.

À télécharger (Modules → Ajouter des modules…) :

- **ADS-B** : avions sur 1090 MHz, carte radar et tableau ;
- **Scanner de mémoires** : parcourt les mémoires, s'arrête sur un signal, reprend après un silence ;
- **Scanner de fréquences** : plage début–fin avec un pas, seuil relatif au bruit, en boucle ;
  se cale au milieu des stations plus larges que le pas (WFM) ;
- **Décodeur SSTV** : images Martin M1/M2, Scottie S1/S2/DX, Robot 36/72, PD50 à PD290 (l'ISS émet en
  PD120 sur 145,800 MHz NFM ; en HF : 14,230 · 21,340 · 28,680 MHz USB). Mode reconnu par l'en-tête VIS
  (ou forcé à la main), lignes calées sur les tops de synchro (pas d'image penchée même sans correction ppm),
  images enregistrées en PNG dans `~/.config/sdr-modulaire/donnees/sstv/` ; liste de fréquences préréglées qui accorde
  dans le bon mode (ISS, 2 m, 6 m, 10 à 80 m en USB/LSB), bouton « Agrandir » (image en grand dans une fenêtre à part) ;
  aussi dans SDR Web (`web/sstv.js`) ;
- **Pilotage rigctl** : serveur compatible rigctld sur `localhost:4532` (IPv4 `127.0.0.1` et IPv6 `::1`,
  connexions locales seulement) : Gpredict corrige le Doppler des satellites, WSJT-X, fldigi… règlent
  fréquence et mode. Dans Gpredict : radio « RX only », hôte localhost, port 4532, puis « Engage ».
- **Décodeur CW** : Morse décodé par un réseau de neurones (CNN + GRU bidirectionnel + CTC, poids
  embarqués, numpy seul), 10 à 42 WPM, robuste au bruit, au QSB et à la manipulation manuelle ; texte
  avec ~4,5 s de retard. Mode CW, squelch ouvert. Entraînement : `outils/decodeur_cw/` ; aussi dans SDR Web
  (`web/cw.js`, poids `web/cw-poids.bin` exportés par `outils/decodeur_cw/exporter_web.py`) ;
- **Décodeur APRS** : trames AFSK 1200 bauds décodées sans logiciel externe (plus besoin de Dire Wolf),
  sur 144,800 MHz (ou 144,390, ou l'ISS 145,825) extrait des IQ, même si le VFO écoute ailleurs ; alerte
  de saturation. Sur les 100 trames bruitées de `gen_packets -n 100` : 72 décodées (Dire Wolf : 70).
  Carte des stations comme pour l'ADS-B (positions non compressées, compressées, Mic-E, objets, items).
  Aussi dans SDR Web (`web/aprs.js`, même algorithme, mêmes trames).
- **Décodeur FT8 / FT4** : messages FT8 (créneaux de 15 s) et FT4 (7,5 s) reçus en USB, décodés par `jt9`
  de WSJT-X (`sudo apt install wsjtx`). Horloge synchronisée (NTP) requise. Liste « Bande » : 160 m à 70 cm,
  fréquences d'appel de WSJT-X, accord en USB d'un clic (clé qui reçoit la HF ou convertisseur).
  Aussi dans SDR Web (`web/ft8-worker.js`, moteur ft8_lib compilé en WebAssembly dans `web/ft8-lib/`).
- **Décodeur DMR** (données, sans logiciel externe) : sur la fréquence du VFO, quel que soit le mode, il affiche
  les appels des relais et du direct : slot (TS1, TS2), color code, groupe (TG) ou appel privé, ID et alias
  de l'émetteur, chiffrement, urgence, durée ; les SMS (paquets IP, Motorola TMS, blocs 1/2 et 3/4) ; les CSBK
  sur demande. L'appel est trouvé même sans son en-tête (LC intégré des salves vocales). La voix (AMBE+2) n'est
  pas décodée. Codes de la norme ETSI TS 102 361-1 vérifiés avec dsd-fme sur le flux de `tests/test_decodeur_dmr.py`.
  Aussi dans SDR Web (`web/dmr.js`, même algorithme, mêmes résultats).
- **Tonalités CTCSS et DTMF** (relais VHF / UHF) : sur la fréquence du VFO, quel que soit le mode, la sous-porteuse
  CTCSS d'accès d'un relais (tonalité normalisée parmi les 52, fréquence mesurée au centième de hertz, déviation,
  durée), les codes DTMF entendus (regroupés en séquences) et la tonalité 1750 Hz d'ouverture des relais.
  CTCSS par DFT sur 2 s avec interpolation (150,0 / 151,4 et 156,7 / 157,7 Hz bien séparés), DTMF par Goertzel.
  Fonctionne jusqu'à ~5 dB de C/N sur les signaux de `tests/test_tonalites.py`.
  Aussi dans SDR Web (`web/tonalites.js`, même algorithme, mêmes résultats).
- **Décodeur AIS (navires)** : 161,975 et 162,025 MHz décodés ensemble, quel que soit le mode du VFO, sans logiciel
  externe. Positions (classes A et B), identité et voyage (nom, indicatif, type de navire, dimensions, destination),
  stations côtières et aides à la navigation ; carte (navires orientés selon leur route, vecteur vitesse sur 6 min,
  couleur selon le type) et tableau trié par distance. GMSK 9 600 bauds : composante continue par moyenne centrée
  (décalage de la clé affiché), trois seuils, horloge bit recalée sur les passages par zéro ; HDLC et CRC comme l'APRS.
  Lecture des champs vérifiée sur les exemples de la documentation de gpsd ; sur un banc de 40 salves avec ±1,5 kHz de
  décalage : toutes jusqu'à 15 dB de C/N, ~3/4 à 13 dB. Aussi dans SDR Web (`web/ais.js`, mêmes résultats).
- **Fax météo (WEFAX)** (ondes courtes) : cartes des stations météo (DWD Pinneberg 3 855 · 7 880 · 13 882,5 kHz,
  Northwood, Athènes, Boston), choisies dans une liste (ou VFO en USB + 1,9 kHz) ; repris du décodeur d'IC-7300 Scope.
  Le module extrait le canal de la station dans les IQ (mode du VFO indifférent), attend le signal de départ, cale le
  bord gauche et la pente sur le phasage, trace la carte et l'enregistre en PNG au signal d'arrêt. Démarrage à la main,
  clic sur le bord gauche, pente réglable, inversion (LSB), bouton « Accorder » (USB 1,9 kHz sous la fréquence annoncée).
  Clé qui reçoit la HF (V4, échantillonnage direct) ou convertisseur. Aussi dans SDR Web (`web/fax-meteo.js`, même carte).
- **Décodeur ACARS** (VHF AM, 131 MHz) : messages des avions et des stations sol, sans logiciel externe. Liste de
  fréquences préenregistrées (Europe 131,525 · 131,725 · 131,825 MHz, Amérique du Nord, Japon, Asie, ou fréquence du
  VFO) : toutes celles qui tiennent dans la bande reçue sont décodées en même temps, quel que soit le mode ; bouton
  « Accorder ». Affiche sens (↓ avion vers sol, ↑ sol vers avion), immatriculation, vol, label et sa signification,
  numéro de message, texte. Démodulation MSK cohérente (2 400 bauds), CRC-16, correction de 1 à 3 caractères de
  parité fausse ou de deux bits d'un caractère. Testé sur des signaux synthétiques construits comme l'inverse exact
  du démodulateur d'acarsdec (MSK vérifiée : enveloppe constante, 1 200 / 2 400 Hz) ; pas encore sur l'air.
  Aussi dans SDR Web (`web/acars.js`, mêmes messages sur le même signal).
- **Enregistreur IQ** (toute la bande reçue) : enregistre les échantillons IQ au taux de la clé dans un WAV deux canaux
  8 bits (2 octets par échantillon : 288 Mo par minute à 2,4 MS/s), nommé et marqué comme ceux de SDR# / HDSDR
  (fréquence centrale et heure UTC dans le bloc `auxi`), donc lisible par SDR#, HDSDR, SDR++. Nouveau fichier si
  le centre ou le taux change, ou près de 4 Go ; arrêt si le disque passe sous 1 Go libre. **Relecture** : le
  module remplace la clé par l'enregistrement, à vitesse réelle, au taux et au centre du fichier (position réglable,
  en boucle ou non) ; on s'accorde n'importe où dans la bande et tous les modules marchent comme en direct.
  Lit aussi les WAV IQ 16 bits et flottants, SigMF (cu8, ci8, ci16_le, cf32_le) et les fichiers bruts de gqrx.
  Aussi dans SDR Web (`web/enregistreur-iq.js` : même format de fichier, enregistrement dans un dossier choisi
  sous Chrome, relecture des WAV 8 et 16 bits).

## Pistes

Déjà réalisées : stéréo + RDS en WFM, scanner de mémoires, scanner de fréquences, ADS-B,
capteurs 433 MHz, pilotage rigctl (Gpredict), décodeur SSTV, décodeur CW, décodeur APRS, décodeur FT8/FT4,
décodeur DMR (données), tonalités CTCSS / DTMF / 1750 Hz, enregistreur IQ, décodeur ACARS, fax météo, décodeur AIS, convertisseur HF,
version navigateur (SDR Web).

Restent : voix DMR (dsd-fme dans l'appli), envoi des trames APRS vers aprs.jerko.fr / APRS-IS,
WSPR via `jt9`, sortie MQTT vers InfluxDB/Grafana, serveur web pour
l'écoute à distance.

## Licence

Logiciel libre sous licence MIT : vous pouvez l'utiliser, le modifier et le
redistribuer librement, y compris dans un autre projet, en gardant la mention de
copyright. Voir le fichier [LICENSE](LICENSE).

Exception : `web/rtlsdr.js` (pilote WebUSB de SDR Web) est un portage de librtlsdr et reste sous
licence GPL-2.0 ou ultérieure, comme l'original.

## SDR Web

Une version navigateur (WebUSB, Chrome/Edge/Opera) est dans [web/](web/) : https://rtl-sdr.fr/sdr-web.html

Présentation du projet : https://rtl-sdr.fr/sdr-modulaire.html
