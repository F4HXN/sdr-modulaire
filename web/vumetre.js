// SPDX-License-Identifier: MIT
// Vu-mètre égaliseur à LED : l'audio démodulé découpé en bandes de fréquence, chaque bande affichée en
// colonne de LED (vert, jaune, rouge), crête maintenue, reflet ; colonne VU du niveau global à droite.
// Même calcul que modules-a-telecharger/vumetre.py :
//  - FFT de 4096 échantillons (fenêtre de Hann) sur les dernières 85 ms d'audio, à chaque image ;
//  - bandes réparties en échelle logarithmique de 50 Hz à la limite du mode (WFM 16 kHz, AM 5 kHz,
//    sinon 3,2 kHz) ; puissance de bande en dB, 0 dB = sinusoïde pleine échelle ;
//  - échelle automatique : le haut suit la bande la plus forte (montée immédiate, descente 3 dB/s),
//    45 dB de dynamique ; VU : niveau efficace global de −40 à 0 dB ;
//  - balistique : montée en 25 ms, retombée en 300 ms, crête tenue 0,8 s puis retombée 12 dB/s.

export const TAILLE = 4096;
export const F_MIN = 50;
export const DYNAMIQUE = 45, VU_MIN = -40;
export const NB_LED = 12;
export const BANDES = [8, 12, 16, 24, 32];

/** Fréquence haute affichée selon le mode. */
export const frequenceMax = mode => mode === "WFM" ? 16000 : mode === "AM" ? 5000 : 3200;

/** Couleur de la LED i (0 = bas) sur n : vert foncé → vert clair, jaune, rouge vif → rouge sombre. */
export function couleurLed(i, n) {
  const p = (i + 0.5) / n;
  if (p < 4 / 12) { const t = p / (4 / 12); return mix([62, 125, 32], [150, 245, 100], t); }
  if (p < 7 / 12) { const t = (p - 4 / 12) / (3 / 12); return mix([235, 200, 40], [255, 240, 110], t); }
  const t = (p - 7 / 12) / (5 / 12); return mix([255, 28, 10], [125, 26, 14], t);
}
const mix = (a, b, t) => a.map((v, k) => Math.round(v + (b[k] - v) * t));

function hann(n) {
  const w = new Float32Array(n);
  for (let i = 0; i < n; i++) w[i] = 0.5 - 0.5 * Math.cos(2 * Math.PI * i / n);
  return w;
}
const FEN = hann(TAILLE);
const SOMME_W2 = FEN.reduce((s, v) => s + v * v, 0);

/** |X_k|² (k = 0..n/2) d'un signal réel fenêtré, par FFT radix 2. */
function puissances(x) {
  const n = x.length, re = new Float64Array(n), im = new Float64Array(n);
  const bits = Math.log2(n);
  for (let i = 0; i < n; i++) {
    let r = 0;
    for (let b = 0; b < bits; b++) r |= ((i >> b) & 1) << (bits - 1 - b);
    re[r] = x[i] * FEN[i];
  }
  for (let t = 2; t <= n; t *= 2) {
    const d = t / 2, a = -2 * Math.PI / t;
    for (let j = 0; j < d; j++) {
      const c = Math.cos(a * j), s = Math.sin(a * j);
      for (let i = j; i < n; i += t) {
        const k = i + d, tr = re[k] * c - im[k] * s, ti = re[k] * s + im[k] * c;
        re[k] = re[i] - tr; im[k] = im[i] - ti; re[i] += tr; im[i] += ti;
      }
    }
  }
  const p = new Float64Array(n / 2 + 1);
  for (let k = 0; k <= n / 2; k++) p[k] = re[k] * re[k] + im[k] * im[k];
  return p;
}

/** Bords des bandes (Hz), répartis en échelle logarithmique. */
export function bords(n, fmax) {
  return Array.from({ length: n + 1 }, (_, i) => F_MIN * (fmax / F_MIN) ** (i / n));
}

/** Puissance des bandes en dB (0 dB = sinusoïde pleine échelle) et niveau efficace global. */
export function analyser(x, taux, n, fmax) {
  const p = puissances(x), b = bords(n, Math.min(fmax, taux / 2)), df = taux / TAILLE;
  const norme = 2 / (TAILLE * SOMME_W2) / 0.5;
  const db = new Float64Array(n);
  for (let j = 0; j < n; j++) {
    const k0 = Math.ceil(b[j] / df), k1 = Math.ceil(b[j + 1] / df);
    let s = 0;
    if (k1 > k0) for (let k = k0; k < k1; k++) s += p[k];
    else s = p[Math.round((b[j] + b[j + 1]) / 2 / df)] * (b[j + 1] - b[j]) / df;   // bande plus étroite qu'une raie
    db[j] = 10 * Math.log10(s * norme + 1e-12);
  }
  let e = 0;
  for (let i = 0; i < x.length; i++) e += x[i] * x[i];
  const vu = 10 * Math.log10(e / x.length / 0.5 + 1e-12);
  return { db, vu };
}

/** Analyse continue et balistique des LED : position 0..1 de chaque colonne et de sa crête. */
export class MoteurVumetre {
  constructor() {
    this.tampon = new Float32Array(TAILLE); this.taux = 48000; this.recu = 0;
    this.n = 0; this.niveaux = []; this.cretes = []; this.tenues = []; this.haut = -60;
  }

  pousser(a, taux) {
    this.taux = taux;
    const T = this.tampon;
    if (a.length >= T.length) T.set(a.subarray(a.length - T.length));
    else { T.copyWithin(0, a.length); T.set(a, T.length - a.length); }
    this.recu = Math.min(T.length, this.recu + a.length);
  }

  /** Une image : dt en secondes, n bandes, fmax en Hz, actif = audio récent. Colonnes 0..n-1, puis la VU. */
  image(dt, n, fmax, actif = true) {
    if (n + 1 !== this.n) {
      this.n = n + 1; this.niveaux = new Float64Array(n + 1); this.cretes = new Float64Array(n + 1); this.tenues = new Float64Array(n + 1);
    }
    let cibles = new Float64Array(n + 1);
    if (actif && this.recu === TAILLE) {
      const { db, vu } = analyser(this.tampon, this.taux, n, fmax);
      const max = Math.max(...db);
      this.haut = max > this.haut ? max : Math.max(max, this.haut - 3 * dt);
      const sommet = Math.max(this.haut, -60 + DYNAMIQUE) + 2;
      for (let j = 0; j < n; j++) cibles[j] = Math.max(0, Math.min(1, (db[j] - (sommet - DYNAMIQUE)) / DYNAMIQUE));
      cibles[n] = Math.max(0, Math.min(1, (vu - VU_MIN) / -VU_MIN));
    } else cibles = cibles.fill(0);
    for (let j = 0; j <= n; j++) {
      const v = this.niveaux[j], c = cibles[j];
      this.niveaux[j] = v + (c - v) * (1 - Math.exp(-dt / (c > v ? 0.025 : 0.3)));
      if (this.niveaux[j] >= this.cretes[j]) { this.cretes[j] = this.niveaux[j]; this.tenues[j] = 0.8; }
      else if ((this.tenues[j] -= dt) < 0) this.cretes[j] = Math.max(this.niveaux[j], this.cretes[j] - 12 / DYNAMIQUE * dt);
    }
    return { niveaux: this.niveaux, cretes: this.cretes };
  }
}

const COULEURS = Array.from({ length: NB_LED }, (_, i) => couleurLed(i, NB_LED));

/** Dessine les colonnes sur un canevas (taille CSS W × H) ; reflet = reflet au sol sous les colonnes. */
export function dessiner(toile, { niveaux, cretes }, reflet = true) {
  const r = devicePixelRatio || 1, W = toile.clientWidth, H = toile.clientHeight;
  if (!W || !H) return;
  if (toile.width !== Math.round(W * r) || toile.height !== Math.round(H * r)) { toile.width = Math.round(W * r); toile.height = Math.round(H * r); }
  const g = toile.getContext("2d");
  g.setTransform(r, 0, 0, r, 0, 0);
  g.fillStyle = "#000"; g.fillRect(0, 0, W, H);
  const nCol = niveaux.length, marge = Math.max(6, W * 0.025);
  const haut = 15, hBarres = reflet ? (H - haut) * 0.7 : H - haut - marge * 0.6, bas = haut + hBarres;
  // Colonnes égales, la VU séparée par un espace d'une demi-colonne.
  const pasX = (W - 2 * marge) / (nCol + 0.5), largeur = pasX * 0.84;
  const pasY = hBarres / NB_LED, hauteur = pasY * 0.66;
  const x = j => marge + j * pasX + (j === nCol - 1 ? pasX * 0.5 : 0) + (pasX - largeur) / 2;
  const led = (xx, y, i, allumee, alpha = 1) => {
    const [cr, cv, cb] = COULEURS[i];
    g.globalAlpha = alpha * (allumee ? 1 : 0.13);
    g.fillStyle = `rgb(${cr},${cv},${cb})`;
    g.shadowColor = allumee && alpha === 1 ? `rgba(${cr},${cv},${cb},.75)` : "transparent";
    g.shadowBlur = allumee && alpha === 1 ? hauteur * 0.9 : 0;
    g.beginPath(); g.roundRect(xx, y, largeur, hauteur, hauteur / 2); g.fill();
  };
  const allumees = j => Math.round(niveaux[j] * NB_LED);
  const crete = j => Math.min(NB_LED - 1, Math.round(cretes[j] * NB_LED) - 1);
  for (let j = 0; j < nCol; j++) {
    const a = allumees(j), c = crete(j);
    for (let i = 0; i < NB_LED; i++) led(x(j), bas - (i + 1) * pasY + (pasY - hauteur) / 2, i, i < a || (i === c && c >= 0));
  }
  g.shadowBlur = 0; g.shadowColor = "transparent";
  if (reflet) {                     // reflet au sol : écrasé, de plus en plus transparent
    const hReflet = H - bas - 2, pasR = Math.min(pasY * 0.6, hReflet / NB_LED);
    for (let j = 0; j < nCol; j++) {
      const a = allumees(j), c = crete(j);
      for (let i = 0; i < NB_LED; i++) {
        const y = bas + 4 + i * pasR;
        if (y + pasR * 0.6 > H) break;
        const [cr, cv, cb] = COULEURS[i], on = i < a || (i === c && c >= 0);
        g.globalAlpha = (on ? 0.34 : 0.05) * (1 - i / NB_LED) ** 1.3;
        g.fillStyle = `rgb(${cr},${cv},${cb})`;
        g.beginPath(); g.roundRect(x(j), y, largeur, pasR * 0.6, pasR * 0.3); g.fill();
      }
    }
  }
  g.globalAlpha = 1;
  g.fillStyle = "rgba(255,255,255,.55)"; g.textAlign = "center"; g.textBaseline = "bottom";
  g.font = `600 11px "Share Tech Mono", monospace`;
  g.fillText("VU", x(nCol - 1) + largeur / 2, haut - 1);
}
