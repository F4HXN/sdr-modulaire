// SPDX-License-Identifier: MIT
// Décodeur APRS (AFSK 1200 bauds, AX.25) : portage du module decodeur_aprs.py de SDR Modulaire.
// audio (TAUX_AFSK) → corrélateurs complexes 1 200 / 2 200 Hz sur un bit → 9 décisions
// |marque| − g·|espace| (g de 0,5 à 2 : pente entre les tonalités) → passe-bas d'ordre 2 →
// changements de tonalité interpolés → bits par palier → NRZI → HDLC → CRC → texte TNC2.

export const TAUX_AFSK = 24000;
const DEBIT = 1200, MARQUE = 1200, ESPACE = 2200, LISSAGE = 900;
const PENTES = Array.from({ length: 9 }, (_, k) => 2 ** ((k - 4) / 4));

/** Butterworth d'ordre 2 (transformation bilinéaire), comme passe_bas_2 du module Python. */
function passeBas2(fc, fs) {
  const k = Math.tan(Math.PI * fc / fs), q = 1 / (1 + Math.SQRT2 * k + k * k), b0 = k * k * q;
  return [b0, 2 * b0, b0, 2 * (k * k - 1) * q, (1 - Math.SQRT2 * k + k * k) * q];
}

export function crcAX25(o, n = o.length) {
  let c = 0xffff;
  for (let i = 0; i < n; i++) { c ^= o[i]; for (let b = 0; b < 8; b++) c = c & 1 ? (c >>> 1) ^ 0x8408 : c >>> 1; }
  return c ^ 0xffff;
}

class Tranche {
  constructor(g, spb, filtre) {
    this.g = g; this.spb = spb; this.f = filtre; this.z1 = 0; this.z2 = 0;
    this.prec = 0; this.dernier = 0; this.reg = 0; this.uns = 0; this.bits = null;
  }
  traiter(am, as, t0, sortie) {
    const [b0, b1, b2, a1, a2] = this.f, g = this.g;
    const negatif = v => v < 0 || Object.is(v, -0);                         // comme np.signbit
    let prec = this.prec, z1 = this.z1, z2 = this.z2;
    for (let i = 0; i < am.length; i++) {
      const x = am[i] - g * as[i];
      const y = b0 * x + z1; z1 = b1 * x - a1 * y + z2; z2 = b2 * x - a2 * y;   // forme directe II transposée
      if (negatif(y) !== negatif(prec)) {
        const t = t0 + i - 1 + (prec !== y ? prec / (prec - y) : 0.5);           // passage par zéro interpolé
        const n = Math.round((t - this.dernier) / this.spb);
        this.dernier = t;
        if (n >= 1) {
          this.bit(0, sortie, t);                                              // NRZI : changement = 0
          for (let j = 0; j < Math.min(n - 1, 7); j++) this.bit(1, sortie, t);
        }
      }
      prec = y;
    }
    this.prec = prec; this.z1 = z1; this.z2 = z2;
  }
  bit(b, sortie, t) {
    this.reg = ((this.reg << 1) | b) & 0xff;
    if (this.reg === 0x7e) {                                                   // fanion 01111110
      const bits = this.bits;
      if (bits && bits.length >= 7 + 18 * 8 && (bits.length - 7) % 8 === 0) {
        const n = (bits.length - 7) / 8, o = new Uint8Array(n);
        for (let i = 0; i < n; i++) { let v = 0; for (let k = 0; k < 8; k++) v |= bits[8 * i + k] << k; o[i] = v; }
        if (crcAX25(o, n - 2) === (o[n - 2] | (o[n - 1] << 8))) sortie.push([t, o.slice(0, n - 2)]);
      }
      this.bits = []; this.uns = 0;
      return;
    }
    if (!this.bits) return;
    if (b) {
      if (++this.uns > 6) { this.bits = null; return; }                       // 7 uns : abandon
      this.bits.push(1);
    } else {
      if (this.uns === 5) { this.uns = 0; return; }                           // bit de bourrage
      this.uns = 0; this.bits.push(0);
    }
    if (this.bits.length > 8 * 340) this.bits = null;
  }
}

/** Audio en blocs (TAUX_AFSK) → trames AX.25 (Uint8Array, sans le CRC), chacune rendue une fois. */
export class DemodAFSK {
  constructor(fs = TAUX_AFSK) {
    this.fs = fs; this.spb = fs / DEBIT; this.n = Math.round(this.spb); this.t = 0;
    // moyennes glissantes sur un bit : anneaux des derniers produits et sommes courantes
    this.anneau = new Float64Array(4 * this.n); this.pos = 0; this.s = new Float64Array(4);
    const f = passeBas2(LISSAGE, fs);
    this.tranches = PENTES.map(g => new Tranche(g, this.spb, f));
    this.recentes = [];
  }
  traiter(audio) {
    const N = audio.length, am = new Float64Array(N), as = new Float64Array(N);
    const n = this.n, A = this.anneau, s = this.s, wm = 2 * Math.PI * MARQUE / this.fs, ws = 2 * Math.PI * ESPACE / this.fs;
    for (let k = 0; k < N; k++) {
      const t = this.t + k, x = audio[k];
      const v = [x * Math.cos(wm * t), -x * Math.sin(wm * t), x * Math.cos(ws * t), -x * Math.sin(ws * t)];
      const p = 4 * this.pos;
      for (let j = 0; j < 4; j++) { s[j] += v[j] - A[p + j]; A[p + j] = v[j]; }
      this.pos = (this.pos + 1) % n;
      am[k] = Math.hypot(s[0], s[1]); as[k] = Math.hypot(s[2], s[3]);
    }
    const trouvees = [];
    for (const tr of this.tranches) tr.traiter(am, as, this.t, trouvees);
    this.t += N;
    trouvees.sort((a, b) => a[0] - b[0]);
    const nouvelles = [], egal = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);
    for (const [t, o] of trouvees) {
      if (this.recentes.some(([tr, r]) => Math.abs(t - tr) < this.fs && egal(o, r))) continue;
      this.recentes.push([t, o]); nouvelles.push(o);
    }
    this.recentes = this.recentes.filter(([t]) => this.t - t < 2 * this.fs);
    return nouvelles;
  }
}

// ------------------------------------------------------------------ AX.25 → texte
function adresse(o, i) {
  let ind = ""; for (let k = 0; k < 6; k++) ind += String.fromCharCode(o[i + k] >> 1);
  ind = ind.trim(); const ssid = (o[i + 6] >> 1) & 15;
  return [ssid ? `${ind}-${ssid}` : ind, !!(o[i + 6] & 0x80), !!(o[i + 6] & 1)];
}
/** « SOURCE>DEST,RELAIS*,…:info », ou null si ce n'est pas une trame UI valide. */
export function trameVersTexte(o) {
  const adr = []; let i = 0;
  while (i + 7 <= o.length) { adr.push(adresse(o, i)); i += 7; if (adr[adr.length - 1][2]) break; }
  if (adr.length < 2 || !adr[adr.length - 1][2] || adr.length > 10 || i + 2 > o.length) return null;
  if (adr.some(([a]) => !a || !/^[A-Za-z0-9-]+$/.test(a))) return null;
  if (o[i] !== 0x03 || o[i + 1] !== 0xf0) return null;                       // trame UI, sans couche 3
  let info = ""; for (let k = i + 2; k < o.length; k++) info += String.fromCharCode(o[k]);
  info = info.replace(/[\r\n]+$/, "");
  return `${adr[1][0]}>${[adr[0][0], ...adr.slice(2).map(([a, h]) => a + (h ? "*" : ""))].join(",")}:${info}`;
}
