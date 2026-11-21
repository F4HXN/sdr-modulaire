# Modules à télécharger

Ces modules ne sont **pas** chargés automatiquement par SDR Modulaire. Le plus simple : dans l'application,
**Modules → Ajouter des modules…**, puis **Installer**. À la main :

1. copiez le fichier dans `~/.config/sdr-modulaire/modules/` ;
2. dans l'application : **Modules → Gérer les modules → Rescanner les dossiers**, puis cochez-le.

Ils sont aussi téléchargeables sur <https://rtl-sdr.fr/sdr-modulaire.html#modules-a-venir>.

| Fichier   | Ce qu'il fait |
|-----------|---------------|
| `adsb.py` | avions sur 1090 MHz (2,4 MS/s, gain maximal) : carte radar mondiale et tableau dans une fenêtre séparée |
| `scanner_memoires.py` | balaie les fréquences du module Mémoires, s'arrête sur un signal et reprend après un silence |
| `scanner_frequences.py` | balaie une plage début–fin avec un pas, s'arrête sur un signal qui dépasse le bruit estimé |
