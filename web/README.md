# SDR Web

Version navigateur de SDR Modulaire : la page pilote la clé RTL-SDR par **WebUSB** et fait tout
le traitement dans le navigateur. En ligne : https://rtl-sdr.fr/sdr-web.html

| Fichier         | Rôle                                                        | Licence |
|-----------------|-------------------------------------------------------------|---------|
| `rtlsdr.js`     | pilote WebUSB : puce RTL2832U + tuners FC0013, R820T/R820T2, R828D et Blog V4 (portage de librtlsdr) | GPL-2.0-or-later |
| `dsp.js`        | NCO, filtres décimants à état, FFT, démodulateurs, AGC      | MIT |
| `audio.js`      | sortie audio (AudioWorklet, tampon circulaire)              | MIT |
| `simulation.js` | source simulée (émetteurs fictifs), plus proposée dans la page : sert au développement | MIT |
| `modules.js`    | modules à chaud : S-mètre, mémoires, enregistreurs WAV et IQ, journal, capteurs 433 MHz, stéréo et RDS, ADS-B, scanners de mémoires et de fréquences, décodeurs SSTV, fax météo, FT8, FT4, DMR et ACARS, tonalités CTCSS et DTMF | MIT |
| `ft8-worker.js` | décode les créneaux UTC FT8 (15 s) ou FT4 (7,5 s) depuis l’audio USB, sans bloquer l’interface | MIT |
| `ft8-lib/` | moteur WebAssembly ft8_lib pour le décodage FT8 et FT4 | voir `ft8-lib/LICENSE.txt` |
| `sstv.js`       | décodeur SSTV (Martin, Scottie, Robot, PD) : VIS, calage des lignes sur les tops de synchro ; même algorithme que `modules-a-telecharger/sstv.py` | MIT |
| `capteurs433.js` | décodeur OOK des sondes Nexus-T, Nexus-TH et Rubicson (433,92 MHz) | MIT |
| `adsb.js`       | ADS-B : démodulation Mode S, CRC-24, positions CPR, suivi des avions | MIT |
| `dmr.js`        | DMR (données) : synchros, Golay, BPTC, Reed-Solomon, LC intégré, alias, treillis 3/4, CRC-32 ; même algorithme que `modules-a-telecharger/decodeur_dmr.py` | MIT |
| `fax-meteo.js` | fax météo (WEFAX) : signaux de départ et d'arrêt, phasage, pente, lignes ; même algorithme que `modules-a-telecharger/fax_meteo.py` | MIT |
| `acars.js` | ACARS : démodulation MSK cohérente, trame, CRC-16 et correction, plusieurs fréquences ensemble ; même algorithme que `modules-a-telecharger/decodeur_acars.py` | MIT |
| `enregistreur-iq.js` | enregistreur IQ : WAV 8 bits à la SDR# (bloc `auxi`), source « fichier » pour la relecture ; même format que `modules-a-telecharger/enregistreur_iq.py` | MIT |
| `tonalites.js`  | CTCSS (DFT sur 2 s, interpolation, 52 tonalités normalisées), DTMF (Goertzel) et 1750 Hz ; même algorithme que `modules-a-telecharger/tonalites.py` | MIT |
| `carte-adsb.js` | carte radar des avions (canevas)                              | MIT |
| `carte-monde.js` | côtes et frontières du monde (Natural Earth, domaine public), chargé à l'ouverture de la carte | domaine public |
| `fm-stereo.js`  | décodage stéréo (pilote 19 kHz) et RDS de la FM large bande  | MIT |
| `app.js`        | interface, spectre, chute d'eau, bus des modules            | MIT |

La page HTML qui les assemble est celle du site (`sdr-web.html`), qui charge ces fichiers depuis `sdr-web/`.

Contraintes : Chrome, Edge ou Opera (WebUSB), page en HTTPS ou `localhost`.

FT8 et FT4 : synchroniser l’horloge du système et accorder la clé en USB sur la fréquence du mode. Le calcul se fait dans le navigateur.

Tuners gérés :

| Tuner | Clés courantes | Couverture |
|-------|----------------|------------|
| Fitipower FC0013 | anciennes clés TNT | 22–1100 MHz |
| Rafael R820T / R820T2 | RTL-SDR Blog V3, clés « R820T2 » | 24–1766 MHz |
| Rafael R828D | RTL-SDR Blog V4 (quartz 28,8 MHz, HF par son convertisseur intégré), autres clés R828D | 0,5–1766 MHz (V4), 24–1766 MHz (autres) |

HF :

- **échantillonnage direct** (menu HF) : branche Q de la RTL2832U, pour la Blog V3 et les clés modifiées ;
  la démodulation se fait alors sur les échantillons I/Q directs, sans le tuner ;
- **convertisseur HF** (Ham It Up…) : décalage en MHz ; la page affiche la vraie fréquence et accorde
  la clé sur fréquence + décalage, comme l'application.

Essai local avec une clé : servir le dossier du site et ouvrir `http://localhost:8765/sdr-web.html` dans Chrome
(`localhost` compte comme contexte sécurisé pour WebUSB).
