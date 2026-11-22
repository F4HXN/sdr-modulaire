# SDR Web

Version navigateur de SDR Modulaire : la page pilote la clé RTL-SDR par **WebUSB** et fait tout
le traitement dans le navigateur. En ligne : https://rtl-sdr.fr/sdr-web.html

| Fichier         | Rôle                                                        | Licence |
|-----------------|-------------------------------------------------------------|---------|
| `rtlsdr.js`     | pilote WebUSB : puce RTL2832U + tuners FC0013, R820T/R820T2, R828D et Blog V4 (portage de librtlsdr) | GPL-2.0-or-later |
| `dsp.js`        | NCO, filtres décimants à état, FFT, démodulateurs, AGC      | MIT |
| `audio.js`      | sortie audio (AudioWorklet, tampon circulaire)              | MIT |
| `simulation.js` | source simulée (émetteurs fictifs), plus proposée dans la page : sert au développement | MIT |
| `modules.js`    | modules à chaud : S-mètre, mémoires, enregistreur WAV, journal, capteurs 433 MHz, stéréo et RDS, ADS-B, scanners de mémoires et de fréquences | MIT |
| `capteurs433.js` | décodeur OOK des sondes Nexus-T, Nexus-TH et Rubicson (433,92 MHz) | MIT |
| `adsb.js`       | ADS-B : démodulation Mode S, CRC-24, positions CPR, suivi des avions | MIT |
| `carte-adsb.js` | carte radar des avions (canevas)                              | MIT |
| `carte-monde.js` | côtes et frontières du monde (Natural Earth, domaine public), chargé à l'ouverture de la carte | domaine public |
| `fm-stereo.js`  | décodage stéréo (pilote 19 kHz) et RDS de la FM large bande  | MIT |
| `app.js`        | interface, spectre, chute d'eau, bus des modules            | MIT |

La page HTML qui les assemble est celle du site (`sdr-web.html`), qui charge ces fichiers depuis `sdr-web/`.

Contraintes : Chrome, Edge ou Opera (WebUSB), page en HTTPS ou `localhost`.

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
