// SPDX-License-Identifier: MIT
// Décodeur CW neuronal pour SDR Web : portage fidèle du module decodeur_cw.py de SDR Modulaire.
// audio 8 kHz → spectrogramme (FFT 128, pas de 4 ms) → 8 cases autour de la tonalité →
// 3 convolutions + GRU bidirectionnel (64) → 44 classes → décodage CTC glouton.
// Poids : cw-poids.bin (exporté par outils/decodeur_cw/exporter_web.py). Aucune dépendance.

export const TAUX_CW = 8000;
const NFFT = 128, PAS = 32, K = 8, H = 64, SOUS_ECHANTILLONNAGE = 4;
const CASE_MIN = Math.floor(300 / (TAUX_CW / NFFT)), CASE_MAX = Math.floor(1500 / (TAUX_CW / NFFT));
const ALPHABET = ["_", " ", ..."ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/?.,=+"];
export const DUREE_FENETRE = 6, DUREE_PAS = 3;   // s

// ------------------------------------------------------------------ poids
function demiVersFlottant(h) {
  const s = h & 0x8000 ? -1 : 1, e = (h >> 10) & 0x1f, f = h & 0x3ff;
  if (e === 0) return s * f * 2 ** -24;
  if (e === 31) return f ? NaN : s * Infinity;
  return s * (1 + f / 1024) * 2 ** (e - 15);
}
/** ArrayBuffer de cw-poids.bin → { nom: { forme, v: Float32Array } }. */
export function chargerPoids(tampon) {
  const d = new DataView(tampon);
  if (String.fromCharCode(...new Uint8Array(tampon, 0, 4)) !== "CWP1") throw new Error("poids CW : format inconnu");
  const n = d.getUint32(4, true), index = JSON.parse(new TextDecoder().decode(new Uint8Array(tampon, 8, n)));
  const base = 8 + n, p = {};
  for (const [nom, { forme, debut }] of Object.entries(index)) {
    const taille = forme.reduce((a, b) => a * b, 1), v = new Float32Array(taille);
    for (let i = 0; i < taille; i++) v[i] = demiVersFlottant(d.getUint16(base + 2 * (debut + i), true));
    p[nom] = { forme, v };
  }
  return p;
}

// ------------------------------------------------------------------ outils numpy
function centile(valeurs, q) {               // comme np.percentile (interpolation linéaire)
  const t = Float64Array.from(valeurs).sort(), pos = q / 100 * (t.length - 1), i = Math.floor(pos);
  return i + 1 < t.length ? t[i] + (pos - i) * (t[i + 1] - t[i]) : t[i];
}
function mediane(t) { return centile(t, 50); }

// ------------------------------------------------------------------ prétraitement
const FENETRE = Float32Array.from({ length: NFFT }, (_, i) => 0.5 - 0.5 * Math.cos(2 * Math.PI * i / (NFFT - 1)));
const BITS = Math.log2(NFFT), INVERSE = Uint8Array.from({ length: NFFT }, (_, i) => {
  let r = 0; for (let b = 0; b < BITS; b++) r |= ((i >> b) & 1) << (BITS - 1 - b); return r;
});

/** Puissance |STFT|² : tableau [case][trame] (cases 0 à 64). */
function spectrogramme(x) {
  const n = 1 + Math.floor((x.length - NFFT) / PAS), nc = NFFT / 2 + 1;
  const p = Array.from({ length: nc }, () => new Float32Array(n));
  const re = new Float64Array(NFFT), im = new Float64Array(NFFT);
  for (let t = 0; t < n; t++) {
    for (let i = 0; i < NFFT; i++) { re[INVERSE[i]] = x[t * PAS + i] * FENETRE[i]; im[INVERSE[i]] = 0; }
    for (let taille = 2; taille <= NFFT; taille *= 2) {
      const demi = taille / 2, a0 = -2 * Math.PI / taille;
      for (let j = 0; j < demi; j++) {
        const c = Math.cos(a0 * j), s = Math.sin(a0 * j);
        for (let i = j; i < NFFT; i += taille) {
          const k = i + demi, tr = re[k] * c - im[k] * s, ti = re[k] * s + im[k] * c;
          re[k] = re[i] - tr; im[k] = im[i] - ti; re[i] += tr; im[i] += ti;
        }
      }
    }
    for (let c = 0; c < nc; c++) p[c][t] = re[c] * re[c] + im[c] * im[c];
  }
  return p;
}

/** Audio 8 kHz → { car (K × T), T, tonalité (Hz), puissance de la case de la tonalité }. */
export function pretraiter(x) {
  const p = spectrogramme(x), T = p[0].length;
  const moy = [];
  for (let c = CASE_MIN; c < CASE_MAX; c++) { let s = 0; for (const v of p[c]) s += v; moy.push(s / T); }
  let meilleur = 0, max = -Infinity;
  for (let i = 0; i < moy.length; i++) {      // np.convolve(moy, ones(3)/3, "same")
    const v = ((i > 0 ? moy[i - 1] : 0) + moy[i] + (i + 1 < moy.length ? moy[i + 1] : 0)) / 3;
    if (v > max) { max = v; meilleur = i; }
  }
  const cas = CASE_MIN + meilleur, bas = Math.min(Math.max(cas - K / 2, 0), p.length - K);
  const zone = new Float32Array(K * T);
  for (let k = 0; k < K; k++) for (let t = 0; t < T; t++) zone[k * T + t] = Math.log10(p[bas + k][t] + 1e-9);
  const med = mediane(zone);
  let s = 0, s2 = 0; for (const v of zone) { s += v; s2 += v * v; }
  const m = s / zone.length, ecart = Math.sqrt(Math.max(0, s2 / zone.length - m * m)) + 1e-6;
  const car = zone.map(v => Math.min(8, Math.max(-3, (v - med) / ecart)));
  return { car, T, tonalite: cas * TAUX_CW / NFFT, pTonalite: p[cas] };
}

// ------------------------------------------------------------------ réseau
/** Convolution 1D : x (cin × t), w (cout × cin × k), remplissage k/2, pas donné, puis ReLU. */
function conv1dRelu(x, cin, t, w, b, pas) {
  const [cout, , k] = w.forme, pad = k >> 1, n = Math.floor((t + 2 * pad - k) / pas) + 1;
  const y = new Float32Array(cout * n), W = w.v;
  for (let o = 0; o < cout; o++) {
    for (let j = 0; j < n; j++) {
      let acc = b.v[o];
      for (let i = 0; i < cin; i++) {
        const xi = i * t, wi = (o * cin + i) * k;
        for (let q = 0; q < k; q++) {
          const pos = j * pas + q - pad;
          if (pos >= 0 && pos < t) acc += x[xi + pos] * W[wi + q];
        }
      }
      y[o * n + j] = acc > 0 ? acc : 0;
    }
  }
  return { y, n, c: cout };
}

const sig = x => 1 / (1 + Math.exp(-x));
/** GRU (portes r, z, n comme PyTorch) sur x (T × 64), dans un sens. */
function gru(x, T, entree, wi, wh, bi, bh, inverse) {
  const sortie = new Float32Array(T * H), h = new Float32Array(H), nh = new Float32Array(H);
  const gi = new Float32Array(3 * H), gh = new Float32Array(3 * H);
  for (let pas = 0; pas < T; pas++) {
    const t = inverse ? T - 1 - pas : pas;
    for (let r = 0; r < 3 * H; r++) {
      let a = bi.v[r], c = bh.v[r];
      const oi = r * entree, oh = r * H;
      for (let i = 0; i < entree; i++) a += wi.v[oi + i] * x[t * entree + i];
      for (let i = 0; i < H; i++) c += wh.v[oh + i] * h[i];
      gi[r] = a; gh[r] = c;
    }
    for (let i = 0; i < H; i++) {
      const r = sig(gi[i] + gh[i]), z = sig(gi[H + i] + gh[H + i]);
      const n = Math.tanh(gi[2 * H + i] + r * gh[2 * H + i]);
      nh[i] = (1 - z) * n + z * h[i];
    }
    h.set(nh);
    sortie.set(h, t * H);
  }
  return sortie;
}

/** car (K × T) → logits (T' × 44), T'. */
export function inferer(p, car, T) {
  let c1 = conv1dRelu(car, K, T, p["conv1.weight"], p["conv1.bias"], 1);
  let c2 = conv1dRelu(c1.y, c1.c, c1.n, p["conv2.weight"], p["conv2.bias"], 2);
  let c3 = conv1dRelu(c2.y, c2.c, c2.n, p["conv3.weight"], p["conv3.bias"], 2);
  const Tp = c3.n, E = c3.c, x = new Float32Array(Tp * E);
  for (let o = 0; o < E; o++) for (let t = 0; t < Tp; t++) x[t * E + o] = c3.y[o * Tp + t];   // transposée
  const av = gru(x, Tp, E, p["gru.weight_ih_l0"], p["gru.weight_hh_l0"], p["gru.bias_ih_l0"], p["gru.bias_hh_l0"], false);
  const ar = gru(x, Tp, E, p["gru.weight_ih_l0_reverse"], p["gru.weight_hh_l0_reverse"],
    p["gru.bias_ih_l0_reverse"], p["gru.bias_hh_l0_reverse"], true);
  const C = ALPHABET.length, W = p["sortie.weight"].v, B = p["sortie.bias"].v, logits = new Float32Array(Tp * C);
  for (let t = 0; t < Tp; t++) for (let c = 0; c < C; c++) {
    let a = B[c];
    for (let i = 0; i < H; i++) a += W[c * 2 * H + i] * av[t * H + i] + W[c * 2 * H + H + i] * ar[t * H + i];
    logits[t * C + c] = a;
  }
  return { logits, Tp, C };
}

/** Décodage CTC glouton : [(trame, caractère)]. */
export function decoderCTC(logits, Tp, C) {
  const res = []; let prec = 0;
  for (let t = 0; t < Tp; t++) {
    let m = 0; for (let c = 1; c < C; c++) if (logits[t * C + c] > logits[t * C + m]) m = c;
    if (m !== prec && m !== 0) res.push([t, ALPHABET[m]]);
    prec = m;
  }
  return res;
}

/** Durée d'un point en trames de 4 ms, ou null (comme estimer_dit du module). */
export function estimerDit(puissance) {
  const lp = Array.from(puissance, v => Math.log10(v + 1e-12));
  const bas = centile(lp, 20), haut = centile(lp, 95);
  if (haut - bas < 0.5) return null;
  const seuil = (bas + haut) / 2, actif = lp.map(v => v > seuil);
  let fronts = []; for (let i = 0; i + 1 < actif.length; i++) if (actif[i] !== actif[i + 1]) fronts.push(i);
  if (actif[0]) fronts = fronts.slice(1);
  const durees = [];
  for (let i = 0; i + 1 < fronts.length; i += 2) { const d = fronts[i + 1] - fronts[i]; if (d >= 3) durees.push(d); }
  if (durees.length < 4) return null;
  const limite = 2 * centile(durees, 15), points = durees.filter(d => d <= limite);
  return points.length ? mediane(points) : null;
}

/** Une fenêtre de 6 s à 8 kHz → texte du milieu de la fenêtre (ou du début pour la première). */
export function decoderFenetre(p, fenetre, premiere) {
  let nonNuls = 0; for (const v of fenetre) if (v !== 0) nonNuls++;
  if (nonNuls < 0.5 * fenetre.length) return { silence: true };     // squelch fermé
  const { car, T, tonalite, pTonalite } = pretraiter(fenetre);
  const dit = estimerDit(pTonalite);
  const lp = Array.from(pTonalite, v => Math.log10(v + 1e-12));
  const contraste = 10 * (centile(lp, 95) - centile(lp, 20));
  const { logits, Tp, C } = inferer(p, car, T);
  const pasSortie = SOUS_ECHANTILLONNAGE * PAS / TAUX_CW;          // 16 ms par trame de sortie
  const debut = premiere ? 0 : (DUREE_FENETRE - DUREE_PAS) / 2, fin = (DUREE_FENETRE + DUREE_PAS) / 2;
  const texte = decoderCTC(logits, Tp, C).filter(([t]) => debut <= t * pasSortie && t * pasSortie < fin).map(([, c]) => c).join("");
  return { silence: false, texte, tonalite, wpm: dit ? 1.2 / (dit * PAS / TAUX_CW) : null, contraste };
}
