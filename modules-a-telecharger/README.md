# Modules à télécharger

Ces modules ne sont **pas** chargés automatiquement par SDR Modulaire. Le plus simple : dans l'application,
**Modules → Ajouter des modules…**, puis **Installer**. À la main :

1. copiez le fichier dans `~/.config/sdr-modulaire/modules/` ;
2. dans l'application : **Modules → Gérer les modules → Rescanner les dossiers**, puis cochez-le.

Ils sont aussi téléchargeables sur <https://rtl-sdr.fr/sdr-modulaire.html#modules-a-venir>.

Le catalogue lu par l'application (`https://rtl-sdr.fr/catalogue-modules.json`) est signé en Ed25519 :
l'application refuse d'installer si la signature ne correspond pas à la clé publique qu'elle contient,
si l'adresse de téléchargement sort de `https://rtl-sdr.fr/`, ou si l'empreinte SHA-256 d'un module diffère.
Toute modification d'un module impose donc de régénérer puis de resigner le catalogue (c'est fait
par le script de déploiement du site).

| Fichier   | Ce qu'il fait |
|-----------|---------------|
| `adsb.py` | avions sur 1090 MHz (2,4 MS/s, gain maximal) : carte radar mondiale et tableau dans une fenêtre séparée |
| `scanner_memoires.py` | balaie les fréquences du module Mémoires, s'arrête sur un signal et reprend après un silence |
| `scanner_frequences.py` | balaie une plage début–fin avec un pas, s'arrête sur un signal qui dépasse le bruit estimé, se cale au milieu des stations larges (WFM) |
| `sstv.py` | décodeur SSTV : Martin, Scottie, Robot, PD (ISS en PD120 sur 145,800 MHz NFM) ; mode reconnu par le VIS, image calée sur les tops de synchro, PNG enregistrés |
| `pilotage_rigctl.py` | serveur rigctld local (port 4532, IPv4 et IPv6, connexions locales seulement) : Gpredict suit les satellites (Doppler), WSJT-X, fldigi… pilotent fréquence et mode |
| `decodeur_cw.py` | décode le Morse par réseau de neurones (CNN + GRU + CTC), 10 à 42 WPM, robuste au bruit et au QSB ; texte avec ~4,5 s de retard. Mode CW, squelch ouvert. Entraînement : `outils/decodeur_cw/` |
| `decodeur_aprs.py` | trames APRS (AFSK 1200, AX.25) décodées sans logiciel externe, sur 144,800 MHz extrait des IQ même si le VFO écoute ailleurs ; alerte de saturation |
| `decodeur_ft.py` | messages FT8 et FT4 reçus en USB, décodés par `jt9` (WSJT-X) sur les créneaux UTC de 15 s ou 7,5 s ; liste des bandes (160 m à 70 cm) qui accorde en USB sur la fréquence d'appel ; horloge synchronisée requise |
| `decodeur_dmr.py` | appels DMR des relais et du direct, sans logiciel externe : slot, color code, groupe ou privé, ID et alias de l'émetteur, chiffrement ; SMS (IP, Motorola TMS) ; CSBK sur demande. Suit le VFO, quel que soit le mode. La voix n'est pas décodée |
| `fax_meteo.py` | fax météo (WEFAX) en ondes courtes : stations préenregistrées (DWD, Northwood, Athènes, Boston), départ et arrêt automatiques, phasage (bord gauche, pente), PNG enregistrés ; repris d'IC-7300 Scope |
| `decodeur_acars.py` | messages ACARS (VHF AM) sans logiciel externe : listes de fréquences préenregistrées (Europe, Amérique du Nord, Japon, Asie) décodées ensemble, immatriculation, vol, label, texte ; CRC et correction d'erreurs |
| `enregistreur_iq.py` | enregistre toute la bande reçue (WAV IQ 8 bits à la SDR#, bloc `auxi`) et la rejoue à la place de la clé pour démoduler après coup ; lit aussi WAV 16/32 bits, SigMF et gqrx |
| `tonalites.py` | sous-porteuse CTCSS d'accès d'un relais (tonalité normalisée, fréquence mesurée, déviation, durée), codes DTMF et tonalité 1750 Hz, sur la fréquence du VFO quel que soit le mode ; journal horodaté |
