# Écrire un module pour SDR Modulaire

Guide de référence, pour les humains comme pour les assistants IA (Claude, ChatGPT…).
Présentation illustrée : https://rtl-sdr.fr/sdr-modulaire-modules.html

## En bref

Un module est **un seul fichier Python** contenant **une sous-classe de `Module`**.
On le dépose dans `modules/` (à côté du programme) ou dans `~/.config/sdr-modulaire/modules/`,
puis *Modules → Gérer les modules… → Rescanner les dossiers*, et on coche le module.
Il s'ajoute et se retire **à chaud**, sans redémarrer le récepteur.
Les fichiers dont le nom commence par `_` sont ignorés (voir `modules/_modele.py`, le modèle à copier).

## Structure d'un module

```python
from sdrmod.modules import Module

class MonModule(Module):
    nom = "Mon module"            # obligatoire, unique : nom affiché dans le menu
    description = "Une phrase."   # affichée dans le gestionnaire de modules

    def demarrer(self):           # à l'activation : état initial + abonnements
        self.abonner("niveau", self._sur_niveau)

    def _sur_niveau(self, dbfs):  # rappel du bus : thread DSP
        self.dernier = dbfs

    def creer_widget(self):       # facultatif : panneau à droite (QWidget) ou None
        ...

    def arreter(self):            # au retrait : minuteries, fichiers, sockets
        ...
```

Cycle de vie : `__init__(ctx)` → `demarrer()` → `creer_widget()` → … → `arreter()`.
Ne pas surcharger `__init__` : tout se prépare dans `demarrer()`.
Les abonnements faits avec `self.abonner()` sont retirés automatiquement au retrait.

## Le bus : ce que le module reçoit

| Sujet    | Arguments du rappel                                | Fréquence            | Thread |
|----------|----------------------------------------------------|----------------------|--------|
| `niveau` | `dbfs: float` (puissance dans le canal)            | un bloc ≈ 50 ms      | DSP    |
| `audio`  | `echantillons: np.ndarray float32, taux: int` (48 000) | un bloc ≈ 50 ms  | DSP    |
| `iq`     | `iq: np.ndarray complex64, centre: float (Hz), taux: int` | un bloc ≈ 50 ms | DSP |
| `vfo`    | `frequence: float (Hz), mode: str`                 | à chaque changement  | interface |

- `audio` : mono, valeurs dans [-1, 1], après AGC et squelch (des zéros quand le squelch est fermé), avant le volume.
- `iq` : toute la bande reçue (largeur = `taux`, centrée sur `centre`), composante continue retirée. Lourd : ne s'abonner que si nécessaire.
- Modes possibles : `AM`, `NFM`, `WFM`, `USB`, `LSB`, `CW`.

## Le contexte `self.ctx` : ce que le module peut faire

| Méthode                       | Rôle                                                        |
|-------------------------------|-------------------------------------------------------------|
| `frequence() -> float`        | fréquence d'écoute actuelle (Hz)                            |
| `mode() -> str`               | mode actuel                                                 |
| `regler_frequence(f: float)`  | changer de fréquence (**depuis le thread de l'interface**)  |
| `regler_mode(mode: str)`      | changer de mode (**depuis le thread de l'interface**)       |
| `taux() -> int`               | taux d'échantillonnage actuel (échantillons/s)              |
| `regler_taux(taux: int)`      | changer l'échantillonnage, la source redémarre (**thread de l'interface**) |
| `gains() -> list[float]`      | paliers de gain de la clé (dB), liste vide sans clé         |
| `regler_gain(g: float \| None)` | gain en dB, ou `None` pour l'automatique (**thread de l'interface**) |
| `dossier_donnees(nom) -> Path`| dossier propre au module, créé si besoin (`~/.config/sdr-modulaire/donnees/<nom>`) |

## Règles à respecter

1. **Rappels du bus rapides** : ils s'exécutent dans le thread DSP. Pas de réseau, pas d'attente, pas de calcul de plusieurs millisecondes. Pour un traitement lourd, mettre les données dans une `queue.Queue` et les traiter dans un `threading.Thread` du module (arrêté dans `arreter()`).
2. **Jamais de widget depuis un rappel du bus.** Stocker la valeur (attribut ou `collections.deque`), puis l'afficher depuis un `QTimer` créé dans `creer_widget()` (exemples : `modules/smetre.py`, `modules/journal_activite.py`).
3. **`regler_frequence` / `regler_mode` uniquement depuis le thread de l'interface** (bouton, `QTimer`), jamais depuis un rappel `niveau`, `audio` ou `iq`.
4. **Filtres à état** : un filtre appliqué bloc par bloc doit garder son état d'un bloc à l'autre (`scipy.signal.lfilter(..., zi=...)`), sinon des craquements apparaissent à chaque bloc.
5. **Énumérations Qt** (PySide6 ≥ 6.4) : ne jamais les comparer à un entier. Écrire `Qt.MouseButton.LeftButton`, pas `1`.
6. **Tout libérer dans `arreter()`** : minuteries, fichiers, threads, sockets. Un module doit pouvoir être retiré puis remis sans redémarrer.
7. **Une exception dans un rappel retire le module** (le récepteur continue). Lire le message dans la barre d'état ou le terminal (`python3 -m sdrmod --debug`).
8. **Dépendances** : déjà disponibles : `numpy`, `scipy`, `PySide6`, `pyqtgraph`, la bibliothèque standard. Toute autre dépendance doit être signalée en tête du fichier.
9. **Français** pour les noms, l'interface, les commentaires et les messages (convention du projet).

## Tester

```bash
python3 -m sdrmod --simulation --debug   # source simulée : 5 émetteurs fictifs autour de 145 MHz
```

Activer le module, regarder le terminal. Après une modification du fichier : *Modules → Gérer les modules… → Recharger le module*.

## Exemples fournis

| Fichier                        | Ce qu'il montre                                              |
|--------------------------------|--------------------------------------------------------------|
| `modules/smetre.py`            | abonnement `niveau`, affichage par `QTimer`                  |
| `modules/memoires.py`          | piloter le récepteur (`regler_frequence`, `regler_mode`), fichier JSON |
| `modules/enregistreur_wav.py`  | abonnement `audio`, fichier ouvert/fermé avec un verrou      |
| `modules/journal_activite.py`  | deux sujets, file `deque` du DSP vers l'interface, CSV       |
| `modules/_modele.py`           | modèle vide à copier                                         |

## Demander un module à une IA

Copier ce texte dans l'assistant, en remplaçant la dernière ligne :

```text
Tu écris un module pour SDR Modulaire, récepteur SDR en Python (PySide6, numpy, scipy).
Lis d'abord la spécification : https://raw.githubusercontent.com/F4HXN/sdr-modulaire/main/MODULES.md
et inspire-toi de l'exemple : https://raw.githubusercontent.com/F4HXN/sdr-modulaire/main/modules/journal_activite.py
Contraintes : un seul fichier .py, une sous-classe de sdrmod.modules.Module avec nom et description,
rappels du bus rapides et sans widget, affichage par QTimer, tout libérer dans arreter(),
code et interface en français. Donne le fichier complet, puis comment le tester avec --simulation.
Le module doit : <décrire ici ce que le module doit faire>
```

## Modules à télécharger

Certains modules ne sont pas fournis avec l'application : on les télécharge sur
<https://rtl-sdr.fr/sdr-modulaire.html#modules-a-venir> et on les place dans
`~/.config/sdr-modulaire/modules/`, puis **Modules → Gérer les modules → Rescanner les dossiers**.

| Fichier   | Ce qu'il fait |
|-----------|---------------|
| `adsb.py` | avions sur 1090 MHz (2,4 MS/s), carte radar dans une fenêtre séparée ; montre un décodage lourd dans un fil à part et `regler_taux` |
