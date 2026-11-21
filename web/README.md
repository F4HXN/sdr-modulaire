# SDR Web

Version navigateur de SDR Modulaire : la page pilote la clé RTL-SDR par **WebUSB** et fait tout
le traitement dans le navigateur. En ligne : https://rtl-sdr.fr/sdr-web.html

| Fichier         | Rôle                                                        | Licence |
|-----------------|-------------------------------------------------------------|---------|
| `rtlsdr.js`     | pilote WebUSB : puce RTL2832U + tuner Fitipower FC0013 (portage de librtlsdr) | GPL-2.0-or-later |
| `dsp.js`        | NCO, filtres décimants à état, FFT, démodulateurs, AGC      | MIT |
| `audio.js`      | sortie audio (AudioWorklet, tampon circulaire)              | MIT |
| `simulation.js` | source simulée (émetteurs fictifs)                          | MIT |
| `modules.js`    | modules à chaud : S-mètre, mémoires, enregistreur WAV, journal | MIT |
| `app.js`        | interface, spectre, chute d'eau, bus des modules            | MIT |

La page HTML qui les assemble est celle du site (`sdr-web.html`), qui charge ces fichiers depuis `sdr-web/`.

Contraintes : Chrome, Edge ou Opera (WebUSB), page en HTTPS ou `localhost`.
Tuners gérés : FC0013 pour l'instant ; R820T/R828D à venir.

Essai local avec une clé : servir le dossier du site et ouvrir `http://localhost:8765/sdr-web.html` dans Chrome
(`localhost` compte comme contexte sécurisé pour WebUSB).
