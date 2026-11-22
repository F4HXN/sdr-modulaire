# Entraînement du Décodeur CW

Code qui produit le réseau embarqué dans `modules-a-telecharger/decodeur_cw.py`.
PyTorch n'est nécessaire **que** pour l'entraînement : le module tourne en numpy pur.

## Principe

audio 8 kHz → spectrogramme (FFT 128 points, pas de 4 ms) → 8 cases (500 Hz) autour de la
tonalité la plus forte entre 300 et 1500 Hz → 3 convolutions (sous-échantillonnage ×4) →
GRU bidirectionnel (64) → 44 classes → décodage CTC. Environ 100 000 paramètres.

Données : Morse synthétique (`synthese.py`) : 10 à 42 WPM, manipulation humaine (rapport
point/trait, espacements, jitter, Farnsworth, dérive de vitesse), QSB, chirp, QRM décalé,
parasites, bruit blanc ou filtré par le récepteur, AGC identique à celle de l'appli, SNR de
-12 à +40 dB (dans 2500 Hz). Les fenêtres de 6 s sont découpées n'importe où dans une émission
continue ; l'étiquette ne contient que les caractères entièrement dans la fenêtre.

## Reproduire

```bash
pip install torch scipy numpy
python3 gen_donnees.py 0 16            # 32 000 exemples (~15 min sur 1 cœur ; lancer 2 moitiés en parallèle)
python3 entrainer.py 45                # minutes ; écrit meilleur.pt
python3 entrainer.py 25 meilleur.pt    # reprise / affinage depuis un réseau existant
python3 exporter_module.py meilleur.pt ../../modules-a-telecharger/decodeur_cw.py
python3 -m tests.test_decodeur_cw      # depuis la racine du dépôt : test de bout en bout
```

`decodeur_cw_gabarit.py` est le module sans ses poids ; `exporter_module.py` y insère les poids
(BatchNorm intégrée aux convolutions, float16, zlib, base64). Le prétraitement du gabarit doit
rester identique à `cw_commun.py`.

## Améliorer

Le réseau livré a été entraîné environ 1 h sur 2 cœurs de processeur. Un entraînement plus long
(une nuit, ou un GPU), plus de données (`gen_donnees.py 0 64`) et un GRU plus large (`H = 96`
dans `modele.py` **et** dans le gabarit) devraient surtout améliorer les signaux faibles et les
espaces entre les mots.
