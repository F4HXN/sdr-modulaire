"""Synthèse de CW réaliste (manipulation humaine, bruit, QSB, QRM, AGC) pour l'entraînement."""

import numpy as np

from cw_commun import MORSE, TAUX

PREFIXES = ["F", "F4", "F5", "F6", "F8", "TM", "ON", "DL", "G", "M0", "EA", "I", "HB9", "PA", "OH",
            "SM", "OK", "SP", "YO", "LZ", "K", "W", "N", "VE", "JA", "VK", "ZL", "UA", "EI", "LA", "OZ"]
MOTS = ["CQ", "DE", "TU", "73", "88", "5NN", "599", "RST", "QTH", "NAME", "OP", "UR", "ES", "FB", "OM",
        "PSE", "K", "KN", "BK", "R", "TNX", "FER", "QSO", "QSL", "VIA", "BURO", "WX", "ANT", "RIG", "PWR",
        "W", "HR", "IS", "GM", "GA", "GE", "DR", "HW", "CPY", "AGN", "SRI", "QRZ", "TEST", "UP", "QRS",
        "QRQ", "CUL", "GL", "DX", "POTA", "SOTA", "NR", "JN33", "WWFF", "QRP", "5W", "100W", "DIPOLE",
        "YAGI", "VERT", "SUNNY", "RAIN", "COLD", "TEMP", "HPE", "AGN", "=", "+", "?", "/"]


def indicatif(rng) -> str:
    lettres = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    p = rng.choice(PREFIXES)
    if not p[-1].isdigit():
        p += str(rng.integers(0, 10))
    s = p + "".join(rng.choice(list(lettres), rng.integers(1, 4)))
    if rng.random() < 0.08:
        s += "/" + rng.choice(["P", "M", "QRP", "MM"])
    return s


def texte_aleatoire(rng, n_mots: int = 40) -> str:
    mots = []
    alnum = list(MORSE)
    for _ in range(n_mots):
        r = rng.random()
        if r < 0.25:
            mots.append(indicatif(rng))
        elif r < 0.75:
            mots.append(str(rng.choice(MOTS)))
        elif r < 0.9:   # groupe aléatoire : pour ne pas apprendre seulement le « langage » radio
            mots.append("".join(rng.choice(alnum, rng.integers(1, 6))))
        else:
            mots.append(str(rng.integers(0, 1000)))
    return " ".join(mots)


def manipulation(texte: str, wpm: float, rng) -> tuple[list[tuple[float, float]], list[tuple[float, str]]]:
    """Retourne les impulsions [(début, fin)] en s et la fin de chaque caractère [(t, car)]."""
    u = 1.2 / wpm
    rapport_trait = rng.uniform(2.6, 3.6)
    gap_elem = rng.uniform(0.8, 1.25)
    farns = rng.uniform(1.0, 1.0 if rng.random() < 0.7 else 2.2)    # espacement Farnsworth
    gap_car = rng.uniform(2.6, 4.0) * farns
    gap_mot = rng.uniform(5.5, 9.0) * farns
    jitter = rng.uniform(0.03, 0.15)
    derive = rng.uniform(-0.15, 0.15)       # le manipulateur accélère / ralentit

    def j(x):
        return max(0.3 * x, x * (1 + jitter * rng.standard_normal()))

    t, imp, cars = 0.0, [], []
    for i_mot, mot in enumerate(texte.split(" ")):
        if i_mot:
            t += j(gap_mot * u) - j(gap_car * u)
            cars.append((t, " ", t))
        for i_car, c in enumerate(mot):
            if i_car:
                t += j(gap_car * u) - j(gap_elem * u)
            t_debut_car = t
            for i_el, el in enumerate(MORSE[c]):
                if i_el:
                    t += j(gap_elem * u)
                d = j((rapport_trait if el == "-" else 1.0) * u)
                imp.append((t, t + d))
                t += d
            cars.append((t, c, t_debut_car))
            t += j(gap_elem * u)
        u *= 1 + derive / max(1, len(texte.split(" ")))
    return imp, cars


def enveloppe(imp, n: int, montee: float) -> np.ndarray:
    env = np.zeros(n, dtype=np.float32)
    m = max(1, int(montee * TAUX))
    rampe = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, m, dtype=np.float32))
    for a, b in imp:
        i0, i1 = int(a * TAUX), int(b * TAUX)
        if i1 <= 0 or i0 >= n:
            continue
        i0c, i1c = max(i0, 0), min(i1, n)
        env[i0c:i1c] = 1.0
        if i0 >= 0 and i0 + m < n:
            env[i0:i0 + m] *= rampe
        if i1 - m >= 0 and i1 <= n:
            env[i1 - m:i1] *= rampe[::-1]
    return env


def agc_appli(x: np.ndarray, rng) -> np.ndarray:
    """Imite l'AGC audio de l'appli (attaque immédiate, relâchement lent, blocs de 50 ms)."""
    bloc = int(0.05 * TAUX)
    g, cible, relache = 1.0, 0.3, rng.uniform(0.02, 0.1)
    y = np.empty_like(x)
    for i in range(0, len(x), bloc):
        s = x[i:i + bloc]
        voulu = min(cible / (np.max(np.abs(s)) + 1e-9), 1e4)
        nouveau = voulu if voulu < g else g + (voulu - g) * relache
        y[i:i + bloc] = s * np.linspace(g, nouveau, len(s))
        g = nouveau
    return np.clip(y, -1, 1)


def exemple(rng, duree: float = 6.0, snr_db: float | None = None):
    """Un exemple d'entraînement : (audio 8 kHz, texte, infos)."""
    n = int(duree * TAUX)
    t = np.arange(n) / TAUX
    wpm = float(rng.uniform(10, 42))
    f0 = float(rng.uniform(350, 1400))
    if snr_db is None:
        snr_db = float(rng.choice([rng.uniform(-12, 0), rng.uniform(0, 40)], p=[0.4, 0.6]))
    texte = ""
    x = np.zeros(n, dtype=np.float64)

    vide = rng.random() < 0.05
    if not vide:
        if rng.random() < 0.3:      # début d'émission dans la fenêtre (silence avant)
            debut = rng.uniform(0.0, 3.5)
        else:                       # fenêtre découpée au milieu d'une émission continue
            debut = -rng.uniform(0.0, 25.0)
        imp, cars = manipulation(texte_aleatoire(rng, 80), wpm, rng)
        imp = [(a + debut, b + debut) for a, b in imp]
        if rng.random() < 0.15:     # fin d'émission dans la fenêtre
            fin_emission = rng.uniform(2.0, duree)
            garde_c = [(tf + debut, c, td + debut) for tf, c, td in cars]
            dernier = max([tf for tf, c, td in garde_c if c != " " and tf <= fin_emission] or [0.0])
            imp = [(a, b) for a, b in imp if b <= dernier + 1e-6]
        # étiquette : uniquement les caractères entièrement dans la fenêtre
        sortie, en_attente = [], False
        dernier_imp = max([b for a, b in imp] or [0.0])
        for tf, c, td in cars:
            tf, td = tf + debut, td + debut
            if c == " ":
                en_attente = bool(sortie)
                continue
            if td >= 0.0 and tf <= duree and tf <= dernier_imp + 1e-6:
                if en_attente:
                    sortie.append(" ")
                sortie.append(c)
            en_attente = False if sortie and sortie[-1] == c else en_attente
        texte = "".join(sortie).strip()
        env = enveloppe(imp, n, rng.uniform(0.002, 0.008))
        if rng.random() < 0.4:   # QSB
            env *= 1 - rng.uniform(0.2, 0.85) * (0.5 + 0.5 * np.sin(2 * np.pi * rng.uniform(0.05, 0.6) * t
                                                                      + rng.uniform(0, 6.3)))
        chirp = rng.uniform(-3, 3) * np.cumsum(env) / TAUX if rng.random() < 0.2 else 0.0
        ph = 2 * np.pi * (f0 * t + chirp) + rng.uniform(0, 6.3)
        x += env * np.sin(ph)

    if rng.random() < 0.3:      # QRM : autre station plus faible et décalée
        df = rng.uniform(80, 450) * rng.choice([-1, 1])
        imp2, _ = manipulation(texte_aleatoire(rng), rng.uniform(12, 35), rng)
        dec = rng.uniform(-2, 2)
        env2 = enveloppe([(a + dec, b + dec) for a, b in imp2], n, 0.005)
        x += rng.uniform(0.1, 0.6) * env2 * np.sin(2 * np.pi * (f0 + df) * t)

    # bruit blanc : SNR défini dans 2500 Hz pour une porteuse d'amplitude 1
    sigma2 = 0.5 / (10 ** (snr_db / 10)) / (2500 / (TAUX / 2))
    bruit = rng.standard_normal(n) * np.sqrt(sigma2)
    if rng.random() < 0.6:      # bruit passé dans le filtre du récepteur
        from scipy.signal import butter, sosfilt
        largeur = rng.uniform(300, 700) if rng.random() < 0.5 else rng.uniform(700, 2800)
        centre = f0 + rng.uniform(-100, 100)
        lo, hi = max(50, centre - largeur / 2), min(3900, centre + largeur / 2)
        sos = butter(4, [lo, hi], "bandpass", fs=TAUX, output="sos")
        bruit = sosfilt(sos, bruit) * np.sqrt(TAUX / 2 / (hi - lo))
        x = sosfilt(sos, x)
    x += bruit
    if rng.random() < 0.15:     # parasites impulsifs
        k = rng.integers(1, 30)
        pos = rng.integers(0, n, k)
        x[pos] += rng.standard_normal(k) * rng.uniform(1, 8) * np.sqrt(sigma2 * 4)
    x *= rng.uniform(0.05, 1.0) / (np.max(np.abs(x)) + 1e-9)
    if rng.random() < 0.7:
        x = agc_appli(x, rng)
    return x.astype(np.float32), texte, {"wpm": wpm, "f0": f0, "snr": snr_db}
