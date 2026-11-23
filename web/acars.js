// SPDX-License-Identifier: MIT
// Décodeur ACARS (VHF AM, MSK 2 400 bauds) : même démodulateur et même trame que
// modules-a-telecharger/decodeur_acars.py (voir sa documentation). Toutes les fréquences de la liste
// qui tombent dans la bande reçue sont décodées en même temps.
import { FirComplexe, NCO, passeBas } from "./dsp.js?v=20261122-cw";

const TAUX_ACARS = 12000, SPB = 5, LONG = 2 * SPB;
const KI = 71e-7 / LONG, KP = 60e-3 / LONG;

export const LISTES = [
  ["Europe : 131,525 · 131,725 · 131,825 MHz", [131_725_000, 131_525_000, 131_825_000]],
  ["Europe principale : 131,725 MHz", [131_725_000]],
  ["Europe : 131,525 MHz", [131_525_000]],
  ["Europe : 131,825 MHz", [131_825_000]],
  ["Amérique du Nord : 129,125 à 131,550 MHz", [131_550_000, 131_125_000, 130_450_000, 130_025_000, 129_125_000]],
  ["Amérique du Nord principale : 131,550 MHz", [131_550_000]],
  ["Japon : 131,450 MHz", [131_450_000]],
  ["Asie et Océanie : 131,550 MHz", [131_550_000]],
  ["Fréquence du VFO", []],
];

export const LABELS = {
  "_d": "accusé, pas d'information", Q0: "essai de liaison", SQ: "annonce de la station sol",
  SA: "état des liaisons (VHF, satellite, HF)", H1: "message de l'équipage ou d'un système de bord",
  "5Z": "message de compagnie", QA: "départ du parking (OUT) et carburant", QB: "décollage (OFF)",
  QC: "atterrissage (ON)", QD: "arrivée au parking (IN) et carburant", QE: "départ du parking et destination",
  QF: "décollage et destination", QP: "départ du parking (OUT)", QQ: "décollage (OFF)",
  QR: "atterrissage (ON)", QS: "arrivée au parking (IN)", AA: "CPDLC (vers l'avion)",
  BA: "CPDLC (depuis l'avion)", A6: "ADS-C (demande)", B6: "ADS-C (rapport de position)",
};

// ---------------------------------------------------------------- trame
const SYNCHRO = [0x2B | 0x80, 0x2A, 0x16, 0x16, 0x01];
const N_MOTIF = 40;
const MOTIF = SYNCHRO.flatMap(o => [0, 1, 2, 3, 4, 5, 6, 7].map(i => (o >> i) & 1)).reduce((v, b) => v * 2n + BigInt(b), 0n);
const MASQUE = (1n << 40n) - 1n;
const TABLE = Array.from({ length: 256 }, (_, i) => { let c = i; for (let k = 0; k < 8; k++) c = c & 1 ? (c >>> 1) ^ 0x8408 : c >>> 1; return c; });
export const crc16 = (octets, crc = 0) => { for (const o of octets) crc = (crc >>> 8) ^ TABLE[(crc ^ o) & 0xFF]; return crc; };
const unsBits = o => { let n = 0; while (o) { n += o & 1; o >>>= 1; } return n; };
const pariteOk = o => (unsBits(o) & 1) === 1;
function syndrome(distance) {
  const o = new Uint8Array(((distance + 8) >> 3)); o[0] = 1 << (distance % 8); return crc16(o);
}

/** Corrige en place (texte + 2 octets de CRC) ; nombre de corrections, ou null. */
export function corriger(octets) {
  const crc = crc16(octets), n = octets.length;
  const fausses = [];
  for (let i = 0; i < n - 2; i++) if (!pariteOk(octets[i])) fausses.push(i);
  const syn = (i, b) => syndrome(8 * (n - 1 - i) + b);
  if (!fausses.length) {
    if (crc === 0) return 0;
    for (let i = 0; i < n; i++) for (let a = 0; a < 8; a++) for (let b = a + 1; b < 8; b++)
      if ((crc ^ syn(i, a) ^ syn(i, b)) === 0) { octets[i] ^= (1 << a) | (1 << b); return 2; }
    return null;
  }
  if (fausses.length > 3) return null;
  const essai = (k, reste) => {
    if (k === fausses.length) return reste === 0;
    const i = fausses[k];
    for (let b = 0; b < 8; b++) if (essai(k + 1, reste ^ syn(i, b))) { octets[i] ^= 1 << b; return true; }
    return false;
  };
  return essai(0, crc) ? fausses.length : null;
}

const latin = a => String.fromCharCode(...a);
export function champs(t, corrections) {
  const label = latin(t.slice(9, 11)).replace("\x7f", "d"), bloc = String.fromCharCode(t[11]);
  let texte = t[12] === 0x02 ? latin(t.slice(13, t.length - 1)) : "";
  texte = [...texte].map(c => c >= " " || c === "\r" || c === "\n" ? c : "·").join("").replace(/\r\n|\r/g, "\n");
  const m = { mode: String.fromCharCode(t[0]), immat: latin(t.slice(1, 8)).replace(/^\.+/, "").trim(), ack: t[8], label, bloc,
    suite: t[t.length - 1] === 0x17, corrections, descendant: /[0-9]/.test(bloc), numero: "", vol: "", texte };
  if (m.descendant && texte.length >= 10) { m.numero = texte.slice(0, 4); m.vol = texte.slice(4, 10).trim(); m.texte = texte.slice(10); }
  return m;
}

class Trameur {
  constructor() { this.reg = 0n; this.dans = false; this.octets = []; this.courant = 0; this.nbits = 0; this.inverse = false; this.messages = []; this.rejets = 0; }
  bit(b) {
    if (!this.dans) {
      this.reg = ((this.reg << 1n) | BigInt(b)) & MASQUE;
      let x = this.reg ^ MOTIF, e = 0;
      while (x) { e += Number(x & 1n); x >>= 1n; }
      if (e <= 2 || e >= N_MOTIF - 2) {
        if (e >= N_MOTIF - 2) this.inverse = !this.inverse;
        this.dans = true; this.octets = []; this.courant = 0; this.nbits = 0;
      }
      return;
    }
    this.courant |= b << this.nbits;
    if (++this.nbits < 8) return;
    this.octets.push(this.courant); this.courant = 0; this.nbits = 0;
    const t = this.octets;
    let fin = -1;
    for (let k = 12; k < t.length; k++) if ((t[k] & 0x7F) === 0x03 || (t[k] & 0x7F) === 0x17) { fin = k; break; }
    if (fin >= 0 && t.length >= fin + 3) this.finir(fin);
    else if (t.length > 240) { this.rejets++; this.dans = false; }
  }
  finir(fin) {
    this.dans = false;
    const t = Uint8Array.from(this.octets.slice(0, fin + 3));
    t[fin] = (t[fin] & 0x7F) === 0x03 ? 0x83 : 0x97;
    const n = corriger(t);
    if (n === null) { this.rejets++; return; }
    this.messages.push(champs(Array.from(t.slice(0, fin + 1), c => c & 0x7F), n));
  }
}

// ---------------------------------------------------------------- démodulateur MSK
class DemodMSK {
  constructor(trameur) {
    this.t = trameur; this.phase = 0; this.horloge = 0; this.df = 0; this.dphi = 0;
    this.br = new Float64Array(LONG); this.bi = new Float64Array(LONG); this.i = 0; this.k = 0;
    this.verrou = 0; this.suite = 0; this.dernier = -1; this.dc = 0;
  }
  traiter(am) {
    const w0 = 2 * Math.PI * 1800 / TAUX_ACARS, br = this.br, bi = this.bi;
    let p = this.phase, clk = this.horloge, i = this.i, dc = this.dc;
    for (let n = 0; n < am.length; n++) {
      const s = w0 + this.dphi;
      clk += s;
      if (clk > 1.5 * Math.PI) {
        clk -= 1.5 * Math.PI;
        const f = clk / s;
        let vr = 0, vi = 0;
        for (let j = 0; j < LONG; j++) { const h = Math.sin(Math.PI * (j + f) / LONG), q = (i + j) % LONG; vr += h * br[q]; vi += h * bi[q]; }
        const l = Math.hypot(vr, vi) + 1e-12; vr /= l; vi /= l;
        let vo, err;
        if (this.k & 1) { vo = vi; err = vo >= 0 ? -vr : vr; } else { vo = vr; err = vo >= 0 ? vi : -vi; }
        const b = ((vo > 0) ^ ((this.k & 2) !== 0) ^ this.t.inverse) ? 1 : 0;
        this.k++;
        this.bit(b);
        if (this.verrou > 0 || this.t.dans) { this.df += KI * err; this.dphi = this.df + KP * err; } else { this.df = 0; this.dphi = 0; }
      }
      p += s;
      if (p >= 2 * Math.PI) p -= 2 * Math.PI;
      dc += 0.002 * (am[n] - dc);
      const x = am[n] - dc;
      br[i] = x * Math.cos(p); bi[i] = -x * Math.sin(p);
      i = (i + 1) % LONG;
    }
    this.phase = p; this.horloge = clk; this.i = i; this.dc = dc;
  }
  bit(b) {
    this.suite = b === this.dernier ? this.suite + 1 : 1;
    this.dernier = b;
    if (this.suite >= 16) {
      this.verrou = 80;
      if (b === 0 && this.suite === 16 && !this.t.dans) this.t.inverse = !this.t.inverse;
    } else if (this.verrou > 0) this.verrou--;
    this.t.bit(b);
  }
}

class Canal {
  constructor(frequence, fs) {
    this.frequence = frequence; this.nco = new NCO(fs);
    const d1 = Math.round(fs / 48000);
    this.dec1 = d1 > 1 ? new FirComplexe(passeBas(fs, 8000, 8 * d1 + 1), d1) : null;
    this.dec2 = new FirComplexe(passeBas(48000, 5000, 49), 4);
    this.trameur = new Trameur(); this.demod = new DemodMSK(this.trameur);
    this.niveau = null; this.bruit = null; this.messages = 0; this.attente = null; this.actif = 0;
  }
  traiter(re, im, centre) {
    this.nco.freq = this.frequence - centre;
    let [a, b] = this.nco.melanger(re, im);
    if (this.dec1) [a, b] = this.dec1.traiter(a, b);
    [a, b] = this.dec2.traiter(a, b);
    if (!a.length) return [];
    const env = new Float64Array(a.length);
    let p = 0;
    for (let k = 0; k < a.length; k++) { const q = a[k] * a[k] + b[k] * b[k]; p += q; env[k] = Math.sqrt(q); }
    p = 10 * Math.log10(p / a.length + 1e-20);
    this.niveau = this.niveau === null ? p : Math.max(p, this.niveau - 0.3);
    this.bruit = this.bruit === null ? p : Math.min(p, this.bruit + 0.01);
    if (p > this.bruit + 3) this.actif = 2;            // salve : porteuse au-dessus du bruit
    if (this.actif > 0) {
      this.actif--;
      if (this.attente) this.demod.traiter(this.attente);
      this.demod.traiter(env); this.attente = null;
    } else this.attente = env;
    const sortie = this.trameur.messages.map(m => ({ ...m, frequence: this.frequence }));
    this.messages += sortie.length; this.trameur.messages = [];
    return sortie;
  }
}

function etageCommun(taux, largeur) {
  const n = Math.round(taux / 48000);
  let d = 1;
  for (let k = 1; k <= n; k++) if (n % k === 0 && taux / k >= 1.25 * largeur) d = k;
  return d;
}

/** IQ de la clé → messages de toutes les fréquences demandées qui sont dans la bande. */
export class MoteurACARS {
  constructor(frequences, centre, taux) {
    const lim = 0.42 * taux - 15000;
    this.dedans = frequences.filter(f => Math.abs(f - centre) < lim);
    this.dehors = frequences.filter(f => Math.abs(f - centre) >= lim);
    this.canaux = [];
    if (!this.dedans.length) return;
    const bas = Math.min(...this.dedans), haut = Math.max(...this.dedans);
    this.milieu = (bas + haut) / 2;
    const d = etageCommun(taux, haut - bas + 60000);
    this.fs = taux / d;
    this.nco = new NCO(taux); this.nco.freq = this.milieu - centre;
    this.dec = d > 1 ? new FirComplexe(passeBas(taux, Math.min(0.42 * this.fs, (haut - bas) / 2 + 30000), 6 * d + 1), d) : null;
    this.canaux = this.dedans.map(f => new Canal(f, this.fs));
  }
  traiter(re, im) {
    if (!this.canaux.length) return [];
    let [a, b] = this.nco.melanger(re, im);
    if (this.dec) [a, b] = this.dec.traiter(a, b);
    return this.canaux.flatMap(c => c.traiter(a, b, this.milieu));
  }
}

export const mhz = f => (f / 1e6).toFixed(3).replace(".", ",");
export function texteMessage(m) {
  const morceaux = [`${mhz(m.frequence)} ${m.descendant ? "↓" : "↑"}`];
  if (m.immat) morceaux.push(m.immat);
  if (m.vol) morceaux.push(`vol ${m.vol}`);
  morceaux.push(`label ${m.label}${LABELS[m.label] ? ` (${LABELS[m.label]})` : ""}`);
  if (m.numero) morceaux.push(`n° ${m.numero}`);
  if (m.suite) morceaux.push("suite à venir");
  if (m.corrections) morceaux.push(`${m.corrections} bit(s) corrigé(s)`);
  const t = m.texte.trim();
  return morceaux.join(" · ") + (t ? "\n    " + t.replace(/\n/g, "\n    ") : "");
}
