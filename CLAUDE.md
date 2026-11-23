# SDR Modulaire — contexte pour Claude Code

Récepteur SDR pour Linux en Python (PySide6 + pyqtgraph + numpy/scipy), écrit par
F4HXN. Principe : un cœur minimal (source → spectre/waterfall → démodulation → audio)
et des **modules ajoutés / retirés à chaud** sans redémarrer.

## Matériel de l'auteur

- Clé **Realtek RTL2838UHIDIR, tuner Fitipower FC0013** (ancienne clé TNT) :
  22–1100 MHz, gain −9,9 à 19,7 dB (23 paliers), pas de HF, pas de bias-T.
  Oscillateur non compensé : prévoir une correction ppm.
- Distribution Ubuntu, librtlsdr de la distribution.

## Lancer / tester

```bash
source ~/.venv/sdrmod/bin/activate
python3 -m sdrmod                 # clé RTL-SDR (menu Source → RTL-SDR)
python3 -m tests.test_dsp                                              # chaque mode sur signaux simulés
QT_QPA_PLATFORM=offscreen python3 -m tests.test_interface capture.png  # interface + modules à chaud
QT_QPA_PLATFORM=offscreen python3 -m tests.test_materiel capture.png   # affichage matériel (FC0013 émulée)
QT_QPA_PLATFORM=offscreen python3 -m tests.test_catalogue              # signature du catalogue
python3 -m tests.test_sstv                                             # décodeur SSTV (9 modes, ~40 s)
```

Toujours relancer les trois tests après une modification du DSP ou de l'interface.

## Architecture

- `sdrmod/moteur.py` : thread de lecture (source) → file → thread DSP. Le DSP calcule
  le spectre, puis NCO → `Decimateur` étage 1 (~240 kHz) → [WFM] ou étage 2 (48 kHz)
  → [AM, NFM, USB, LSB, CW] → AGC/squelch → audio. Publie sur le bus.
- Taux d'échantillonnage : **uniquement des multiples de 48 kHz** (`config.TAUX_ECHANTILLONNAGE`)
  pour une décimation entière. Taille de bloc multiple de la décimation totale et de 256
  (`dsp.taille_bloc`), sinon on perd la continuité de phase.
- `sdrmod/bus.py` : sujets `iq`, `audio`, `niveau`, `vfo`. Les rappels tournent dans le
  thread qui publie (DSP) : ils doivent être rapides et ne pas toucher aux widgets Qt.
  Un rappel qui lève une exception est désabonné et son module retiré.
- `sdrmod/modules.py` : `Module` (demarrer / arreter / creer_widget / abonner),
  `Contexte` (frequence, mode, regler_frequence, regler_mode, dossier_donnees),
  `GestionnaireModules` (découverte dans `modules/` et `~/.config/sdr-modulaire/modules/`,
  activer / desactiver / recharger).
- `sdrmod/demods/` : un fichier = un ou plusieurs `Demodulateur`, découverts automatiquement.
  `domaine = "audio"` (reçoit 48 kHz) ou `"intermediaire"` (reçoit ~240 kHz, décime lui-même).
  `decalage_nco` sert de BFO (CW). `bande()` donne la bande affichée relative au VFO.
- `sdrmod/sources/` : `Source` (ouvrir, lire, regler_*, gains, infos). `SOURCES` dans
  `__init__.py`. `infos()` alimente le cadre « Matériel » (table `TUNERS` dans rtlsdr_source.py).
- `sdrmod/ui/` : `fenetre.py` (fenêtre principale, logique d'accord, docks des modules),
  `vue_spectre.py` (spectre + waterfall, clic/glisser/molette), `afficheur.py`
  (fréquence façon transceiver), `panneau.py` (réglages), `dialogue_modules.py`.
- Réglages persistants : `~/.config/sdr-modulaire/reglages.json` (dataclass `config.Reglages`).

## Conventions

- **Code, commentaires, interface et messages en français.**
- Filtres à état (`FIR`, `IIR`, `NCO`) : jamais de filtrage sans état entre blocs.
- PySide6 ≥ 6.4 : les énumérations Qt sont des `enum.Flag`. **Ne jamais comparer à un
  entier** (`ev.button() != 1` est toujours vrai) : utiliser `Qt.MouseButton.LeftButton`.
- Modifier les widgets uniquement depuis le thread GUI (signaux Qt ou `QTimer` qui lit
  une valeur stockée par le rappel du bus, cf. `modules/smetre.py`).

## Pièges d'installation déjà rencontrés

- `undefined symbol: rtlsdr_set_dithering` → pyrtlsdr 0.3 trop récent pour la librtlsdr
  d'Ubuntu : `pip install "pyrtlsdr==0.2.93"`.
- `No module named 'pkg_resources'` → pyrtlsdr 0.2.x : `pip install "setuptools<81"`.
- `usb_claim_interface error -6` → `sudo rmmod dvb_usb_rtl28xxu` (et le blacklister).
- « Audio indisponible » → `sudo apt install libportaudio2`.

## État (21/11/2026)

Vérifié hors matériel : 6 modes, squelch, modules à chaud, accord souris, changement de
taux en marche, détection matériel (clé émulée). Charge DSP ~13 % d'un cœur à 2,4 MS/s.

**Pas encore vérifié sur la vraie clé** : lecture RTL-SDR, sortie audio réelle, gain
manuel, ppm, tenue dans la durée (dérive de latence audio). C'est la première chose à
valider sur la machine.

## Catalogue des modules

`catalogue-modules.json` est signé en Ed25519 (`.sig` à côté, base64). Clé publique :
`sdrmod/signature.py` (vérification en Python pur) ; clé privée hors dépôts,
`~/.config/rtl-sdr.fr/catalogue-ed25519.pem`, script `signer_catalogue.py` du dépôt du site
(appelé par `deployer.sh`). `base` doit être sous `https://rtl-sdr.fr/`. Modifier un module
du catalogue → le recopier dans le site, régénérer et resigner.

## Pistes

Faites : stéréo + RDS, scanners, ADS-B, capteurs 433, rigctl, SSTV, CW, APRS, FT8/FT4, DMR (données), CTCSS/DTMF, enregistreur IQ, ACARS, fax météo, AIS, convertisseur HF, SDR Web.
Restent : voix DMR (dsd-fme, appli seule), envoi APRS vers aprs.jerko.fr, WSPR,
sortie MQTT vers InfluxDB/Grafana, écoute à distance par le web.
