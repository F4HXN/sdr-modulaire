// SPDX-License-Identifier: MIT
// Tonalités CTCSS et DTMF : sous-porteuse d'accès d'un relais, codes DTMF, tonalité 1750 Hz.
// Entrée : discriminateur FM à 48 kHz (en Hz de déviation). Même algorithme que
// modules-a-telecharger/tonalites.py (voir sa documentation) :
//  - CTCSS : décimation à 8 puis 1 kHz ; toutes les 0,5 s, DFT d'une fenêtre de Hann de 2 s (raies de
//    0,5 Hz, 55–270 Hz), interpolation gaussienne, tonalité normalisée la plus proche ; retenue si elle
//    porte la moitié de l'énergie de la bande deux analyses de suite, suivie ensuite tranche par tranche
//    de 0,5 s ;
//  - DTMF et 1750 Hz : 8 kHz, passe-haut à 400 Hz, blocs de 205 échantillons, Goertzel.

export const TAUX_TONALITES = 48000;
const TAUX_DTMF = 8000, TAUX_CTCSS = 1000;
const FENETRE = 2000, PAS = 500, RAIE_MIN = 110, RAIE_MAX = 540, BLOC = 205;
const TR_MIN = 28, TR_MAX = 135;

export const CTCSS = [67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8, 97.4, 100.0, 103.5, 107.2,
  110.9, 114.8, 118.8, 123.0, 127.3, 131.8, 136.5, 141.3, 146.2, 150.0, 151.4, 156.7, 157.7, 159.8,
  162.2, 165.5, 167.9, 171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5, 203.5,
  206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1];
const DTMF_BAS = [697, 770, 852, 941], DTMF_HAUT = [1209, 1336, 1477, 1633];
const TOUCHES = ["123A", "456B", "789C", "*0#D"];
const F_1750 = [1720, 1735, 1750, 1765, 1780];
const FREQ_BLOC = [...DTMF_BAS, ...DTMF_HAUT, ...F_1750];

/** Tonalité normalisée la plus proche, ou null si aucune à moins de 0,7 Hz. */
export function normeCtcss(f) {
  let n = CTCSS[0];
  for (const c of CTCSS) if (Math.abs(c - f) < Math.abs(n - f)) n = c;
  return Math.abs(n - f) <= 0.7 ? n : null;
}

function passeBas(fs, coupure, n) {
  const m = (n - 1) / 2, h = new Float64Array(n);
  let s = 0;
  for (let i = 0; i < n; i++) {
    const x = i - m;
    h[i] = (x === 0 ? 2 * coupure / fs : Math.sin(2 * Math.PI * coupure / fs * x) / (Math.PI * x))
      * (0.54 - 0.46 * Math.cos(2 * Math.PI * i / (n - 1)));
    s += h[i];
  }
  for (let i = 0; i < n; i++) h[i] /= s;
  return h;
}

class FirDecimant {
  constructor(h, d) { this.h = h; this.d = d; this.hist = new Float64Array(h.length - 1); this.phase = 0; }
  traiter(x) {
    const n = this.h.length, h = this.h, b = new Float64Array(this.hist.length + x.length);
    b.set(this.hist); b.set(x, this.hist.length);
    const y = [];
    for (let i = this.phase; i < x.length; i += this.d) {
      let acc = 0;
      for (let j = 0; j < n; j++) acc += b[i + j] * h[n - 1 - j];
      y.push(acc);
    }
    this.phase = (((this.phase - x.length) % this.d) + this.d) % this.d;
    this.hist = b.slice(b.length - this.hist.length);
    return Float64Array.from(y);
  }
}

/** Passe-haut du second ordre (formules RBJ), forme directe II transposée. */
class PasseHaut {
  constructor(fs, f0, q = Math.SQRT1_2) {
    const w = 2 * Math.PI * f0 / fs, al = Math.sin(w) / (2 * q), c = Math.cos(w), a0 = 1 + al;
    this.b = [(1 + c) / 2 / a0, -(1 + c) / a0, (1 + c) / 2 / a0];
    this.a = [-2 * c / a0, (1 - al) / a0];
    this.z0 = 0; this.z1 = 0;
  }
  traiter(x) {
    const [b0, b1, b2] = this.b, [a1, a2] = this.a, y = new Float64Array(x.length);
    let z0 = this.z0, z1 = this.z1;
    for (let i = 0; i < x.length; i++) {
      const v = x[i], s = b0 * v + z0;
      z0 = b1 * v - a1 * s + z1; z1 = b2 * v - a2 * s; y[i] = s;
    }
    this.z0 = z0; this.z1 = z1;
    return y;
  }
}

function hann(n) {
  const w = new Float64Array(n);
  for (let i = 0; i < n; i++) w[i] = 0.5 - 0.5 * Math.cos(2 * Math.PI * i / (n - 1));
  return w;
}

/** |X_k|² des raies k de kMin à kMax (DFT de longueur x.length) ; table cos/sin de cette longueur. */
function raies(x, kMin, kMax, cos, sin) {
  const n = x.length, p = new Float64Array(kMax - kMin + 1);
  for (let k = kMin; k <= kMax; k++) {
    let re = 0, im = 0, idx = 0;
    for (let i = 0; i < n; i++) { re += x[i] * cos[idx]; im -= x[i] * sin[idx]; idx += k; if (idx >= n) idx -= n; }
    p[k - kMin] = re * re + im * im;
  }
  return p;
}

function table(n) {
  const c = new Float64Array(n), s = new Float64Array(n);
  for (let i = 0; i < n; i++) { c[i] = Math.cos(2 * Math.PI * i / n); s[i] = Math.sin(2 * Math.PI * i / n); }
  return [c, s];
}

/** |X(f)|² d'un bloc à une fréquence quelconque (Goertzel). */
function goertzel(x, f, fs) {
  const k = 2 * Math.cos(2 * Math.PI * f / fs);
  let s1 = 0, s2 = 0;
  for (let i = 0; i < x.length; i++) { const s = x[i] + k * s1 - s2; s2 = s1; s1 = s; }
  return s1 * s1 + s2 * s2 - k * s1 * s2;
}

function centrer(x, w) {
  let m = 0;
  for (const v of x) m += v;
  m /= x.length;
  const y = new Float64Array(x.length);
  for (let i = 0; i < x.length; i++) y[i] = (x[i] - m) * w[i];
  return y;
}

const HANN = hann(FENETRE), HANN_TR = hann(PAS);
const SOMME_W2 = HANN.reduce((s, v) => s + v * v, 0);
const TR_SW = HANN_TR.reduce((s, v) => s + v, 0), TR_SW2 = HANN_TR.reduce((s, v) => s + v * v, 0);
const [COS_F, SIN_F] = table(FENETRE), [COS_TR, SIN_TR] = table(PAS);

/** La tonalité f porte-t-elle au moins 40 % de l'énergie 56–270 Hz de cette tranche de 0,5 s ? */
export function tranchePresente(brut, f) {
  const x = centrer(brut, HANN_TR);
  let bande = 1e-30;
  for (const v of raies(x, TR_MIN, TR_MAX, COS_TR, SIN_TR)) bande += v;
  let re = 0, im = 0;
  const w = 2 * Math.PI * f / TAUX_CTCSS;
  for (let i = 0; i < x.length; i++) { re += x[i] * Math.cos(w * i); im -= x[i] * Math.sin(w * i); }
  return (re * re + im * im) * PAS * TR_SW2 / (TR_SW * TR_SW * bande) >= 0.4;
}

export class MoteurTonalites {
  constructor() {
    this.vers8k = new FirDecimant(passeBas(TAUX_TONALITES, 3400, 63), TAUX_TONALITES / TAUX_DTMF);
    this.vers1k = new FirDecimant(passeBas(TAUX_DTMF, 300, 161), TAUX_DTMF / TAUX_CTCSS);
    this.hp = new PasseHaut(TAUX_DTMF, 400);
    this.ctcssBuf = new Float64Array(FENETRE); this.ctcssRempli = 0; this.ctcssN = 0;
    this.dtmfBuf = new Float64Array(BLOC); this.dtmfN = 0;
    this.t = 0;
    this.candidat = null; this.ctcss = null; this.manques = 0; this.mesure = null;
    this.precedent = null; this.tenue = null;
    this.blocs1750 = 0; this.ton1750 = null; this.manques1750 = 0;
    this.touches = 0; this.ev = [];
  }

  traiter(fm) {
    this.ev = [];
    const x8 = this.vers8k.traiter(fm);
    this.ctcssEntree(this.vers1k.traiter(x8));
    const hp = this.hp.traiter(x8);
    for (let i = 0; i < hp.length; i++) {
      this.dtmfBuf[this.dtmfN++] = hp[i];
      if (this.dtmfN === BLOC) { this.bloc(this.dtmfBuf); this.dtmfN = 0; this.t += BLOC / TAUX_DTMF; }
    }
    return this.ev;
  }

  // ------------------------------------------------------------- CTCSS
  ctcssEntree(x) {
    for (let i = 0; i < x.length; i++) {
      this.ctcssBuf.copyWithin(0, 1); this.ctcssBuf[FENETRE - 1] = x[i];
      if (this.ctcssRempli < FENETRE) this.ctcssRempli++;
      if (++this.ctcssN === PAS) {
        this.ctcssN = 0;
        if (this.ctcssRempli === FENETRE) this.analyser(this.ctcssBuf.slice());
      }
    }
  }

  analyser(brut) {
    const p = raies(centrer(brut, HANN), RAIE_MIN, RAIE_MAX, COS_F, SIN_F);
    let i = 1;
    for (let k = 2; k < p.length - 1; k++) if (p[k] > p[i]) i = k;
    let total = 1e-30, tonalite = 0;
    for (const v of p) total += v;
    for (let k = Math.max(0, i - 2); k < Math.min(p.length, i + 3); k++) tonalite += p[k];
    const part = tonalite / total;
    const a = Math.log(p[i - 1] + 1e-30), b = Math.log(p[i] + 1e-30), c = Math.log(p[i + 1] + 1e-30);
    const den = 2 * (2 * b - a - c), delta = den > 0 ? (c - a) / den : 0;
    const f = (RAIE_MIN + i + Math.max(-0.5, Math.min(0.5, delta))) * TAUX_CTCSS / FENETRE;
    const deviation = 2 * Math.sqrt(tonalite / (FENETRE * SOMME_W2));
    this.mesure = { f, part, deviation };
    const present = part >= 0.5, ref = normeCtcss(f) ?? Math.round(f * 2) / 2;
    if (this.ctcss) {
      const s = this.ctcss;
      if (tranchePresente(brut.subarray(FENETRE - PAS), s.f)) {
        s.fin = this.t; this.manques = 0;
        if (present && Math.abs(ref - s.ref) < 0.3) {
          s.n++; s.f += (f - s.f) / s.n; s.deviation += (deviation - s.deviation) / s.n;
        }
      } else if (++this.manques >= 2) this.finCtcss();
    }
    let k = 0;
    if (!this.ctcss && present && this.candidat && Math.abs(ref - this.candidat.ref) < 0.3) {
      while (k < FENETRE / PAS && tranchePresente(brut.subarray(FENETRE - (k + 1) * PAS, FENETRE - k * PAS), f)) k++;
    }
    if (!this.ctcss && k) {          // k = 0 : reste d'une tonalité finie dans la fenêtre de 2 s
      this.ctcss = { ref, norme: normeCtcss(f), f: (f + this.candidat.f) / 2, n: 2, deviation,
        debut: this.t - k * PAS / TAUX_CTCSS, fin: this.t };
      this.manques = 0;
      this.ev.push({ ...this.ctcss, type: "ctcss" });
    }
    this.candidat = present ? { ref, f } : null;
  }

  finCtcss() {
    const c = this.ctcss;
    this.ev.push({ ...c, type: "ctcss_fin", duree: c.fin - c.debut });
    this.ctcss = null; this.manques = 0;
  }

  // ------------------------------------------------------------- DTMF et 1750 Hz
  bloc(x) {
    let e = 0;
    for (const v of x) e += v * v;
    e = e * BLOC / 2 + 1e-30;
    const p = FREQ_BLOC.map(f => goertzel(x, f, TAUX_DTMF) / e);
    const bas = p.slice(0, 4), haut = p.slice(4, 8);
    const ib = bas.indexOf(Math.max(...bas)), ih = haut.indexOf(Math.max(...haut));
    const pb = bas[ib], ph = haut[ih];
    const autres = Math.max(...bas.filter((_, j) => j !== ib), ...haut.filter((_, j) => j !== ih));
    let touche = null;
    if (pb >= 0.08 && ph >= 0.08 && pb + ph >= 0.55 && autres < 0.25 * Math.min(pb, ph)) touche = TOUCHES[ib][ih];
    if (touche && touche === this.precedent && touche !== this.tenue) {
      this.tenue = touche; this.touches++;
      this.ev.push({ type: "dtmf", touche, t: this.t });
    } else if (!touche && !this.precedent) this.tenue = null;
    this.precedent = touche;
    const p1750 = Math.max(...p.slice(8));
    if (p1750 >= 0.5 && !touche) {
      this.blocs1750++; this.manques1750 = 0;
      if (this.blocs1750 === 4 && this.ton1750 === null) {
        this.ton1750 = this.t - 3 * BLOC / TAUX_DTMF;
        this.ev.push({ type: "1750", t: this.t });
      }
    } else {
      this.blocs1750 = 0;
      if (this.ton1750 !== null && ++this.manques1750 >= 2) {
        this.ev.push({ type: "1750_fin", duree: this.t - this.ton1750 - BLOC / TAUX_DTMF });
        this.ton1750 = null;
      }
    }
  }
}

const virgule = s => s.replace(".", ",");
export const secondes = d => `${virgule(d.toFixed(1))} s`;
export function texteCtcss(c) {
  return c.norme !== null
    ? `CTCSS ${virgule(c.norme.toFixed(1))} Hz (mesuré ${virgule(c.f.toFixed(2))} Hz, déviation ≈ ${Math.round(c.deviation)} Hz)`
    : `tonalité ${virgule(c.f.toFixed(1))} Hz hors norme (déviation ≈ ${Math.round(c.deviation)} Hz)`;
}
