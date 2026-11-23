// SPDX-License-Identifier: MIT
// Décodeur AIS (navires) : 161,975 et 162,025 MHz décodés ensemble, GMSK 9 600 bauds, HDLC.
// Même algorithme que modules-a-telecharger/decodeur_ais.py (voir sa documentation) : discriminateur,
// passe-bas, composante continue par moyenne glissante centrée, trois seuils, horloge bit recalée sur les
// passages par zéro, NRZI, HDLC, CRC ; seules les salves sont démodulées.
import { FirComplexe, NCO, passeBas } from "./dsp.js?v=20261122-cw";

export const CANAUX = [[161_975_000, "A"], [162_025_000, "B"]];
const MILIEU = 162_000_000, TAUX_AIS = 48000, SPB = TAUX_AIS / 9600;
const SEUILS = [-300, 0, 300], FENETRE_DC = 400, COUPURE = 4800, LARGEUR_CANAL = 7500, GAIN = 0.15;

// ---------------------------------------------------------------- trame
export function crcHdlc(o) {
  let c = 0xFFFF;
  for (const x of o) { c ^= x; for (let k = 0; k < 8; k++) c = c & 1 ? (c >>> 1) ^ 0x8408 : c >>> 1; }
  return c ^ 0xFFFF;
}
function passeBas2(fc, fs) {
  const k = Math.tan(Math.PI * fc / fs), q = 1 / (1 + Math.SQRT2 * k + k * k), b0 = k * k * q;
  return [b0, 2 * b0, b0, 2 * (k * k - 1) * q, (1 - Math.SQRT2 * k + k * k) * q];
}
/** Octets reçus (poids faible d'abord) → bits du message (chaque octet lu poids fort d'abord). */
export const octetsVersBits = o => Array.from(o).flatMap(b => [7, 6, 5, 4, 3, 2, 1, 0].map(k => (b >> k) & 1));

class Trancheur {
  constructor(decalage) {
    this.decalage = decalage; this.prec = 0; this.centre = SPB / 2; this.niveau = 0;
    this.reg = 0; this.uns = 0; this.bits = null;
  }
  /** d : décision (Hz) ; t0 : instant absolu de d[0]. */
  traiter(d, t0, sortie) {
    const n = d.length, x = new Float64Array(n + 1);
    x[0] = this.prec;
    for (let i = 0; i < n; i++) x[i + 1] = d[i] - this.decalage;   // x[i] est l'échantillon t0 + i − 1
    const fin = t0 + n - 1;
    const bitsJusque = limite => {
      while (this.centre <= limite) {
        const k = Math.round(this.centre) - t0 + 1, v = x[Math.min(Math.max(k, 0), n)];
        const niveau = v > 0 ? 1 : -1;
        this.bit(niveau === this.niveau ? 1 : 0, sortie, this.centre);
        this.niveau = niveau; this.centre += SPB;
      }
    };
    for (let i = 0; i < n; i++) {
      const a = x[i], b = x[i + 1];
      if ((a < 0) === (b < 0)) continue;
      const tp = t0 + i - 1 + (a !== b ? a / (a - b) : 0.5);
      bitsJusque(Math.min(tp, fin));
      let e = tp - (this.centre - SPB / 2);
      e -= SPB * Math.round(e / SPB);
      this.centre += GAIN * e;
    }
    bitsJusque(fin);
    if (n) this.prec = x[n];
  }
  bit(b, sortie, t) {
    this.reg = ((this.reg << 1) | b) & 0xFF;
    if (this.reg === 0x7E) {
      if (this.bits && this.bits.length >= 7 + 80) {
        const bits = this.bits.slice(0, -7);
        if (bits.length % 8 === 0) {
          const o = new Uint8Array(bits.length / 8);
          for (let i = 0; i < o.length; i++) for (let k = 0; k < 8; k++) o[i] |= bits[8 * i + k] << k;
          const corps = o.subarray(0, o.length - 2);
          if (crcHdlc(corps) === (o[o.length - 2] | (o[o.length - 1] << 8))) sortie.push([t, corps.slice()]);
        }
      }
      this.bits = []; this.uns = 0; return;
    }
    if (!this.bits) return;
    if (b) { if (++this.uns > 6) { this.bits = null; return; } this.bits.push(1); }
    else { if (this.uns === 5) { this.uns = 0; return; } this.uns = 0; this.bits.push(0); }
    if (this.bits.length > 8 * 140) this.bits = null;
  }
}

const memes = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);

/** Discriminateur FM à 48 kHz (Hz) → messages (octets sans le CRC). */
export class DemodGMSK {
  constructor() {
    [this.b0, this.b1, this.b2, this.a1, this.a2] = passeBas2(COUPURE, TAUX_AIS);
    this.z1 = 0; this.z2 = 0;
    this.hist = new Float64Array(FENETRE_DC - 1); this.t = 0; this.td = -Math.floor(FENETRE_DC / 2);
    this.trancheurs = SEUILS.map(s => new Trancheur(s)); this.recents = [];
  }
  sauter(n) {
    this.t += n; this.td += n;
    for (const tr of this.trancheurs) tr.centre = Math.max(tr.centre, this.td + SPB / 2);
  }
  traiter(fm) {
    const n = fm.length, y = new Float64Array(n);
    let z1 = this.z1, z2 = this.z2;
    for (let i = 0; i < n; i++) {     // Butterworth d'ordre 2, forme directe II transposée
      const v = this.b0 * fm[i] + z1;
      z1 = this.b1 * fm[i] - this.a1 * v + z2; z2 = this.b2 * fm[i] - this.a2 * v; y[i] = v;
    }
    this.z1 = z1; this.z2 = z2;
    // moyenne glissante centrée sur FENETRE_DC échantillons (retard d'un demi-fenêtre)
    const H = this.hist.length, b = new Float64Array(H + n);
    b.set(this.hist); b.set(y, H);
    const nm = b.length - FENETRE_DC + 1, d = new Float64Array(Math.max(0, nm)), m0 = Math.floor(FENETRE_DC / 2);
    let s = 0;
    for (let i = 0; i < FENETRE_DC - 1; i++) s += b[i];
    for (let i = 0; i < nm; i++) { s += b[i + FENETRE_DC - 1]; d[i] = b[i + m0] - s / FENETRE_DC; s -= b[i]; }
    this.hist = b.slice(b.length - H);
    const trouvees = [];
    for (const tr of this.trancheurs) tr.traiter(d, this.td, trouvees);
    this.td += d.length; this.t += n;
    const nouvelles = [];
    for (const [t, o] of trouvees.sort((p, q) => p[0] - q[0])) {
      if (this.recents.some(([tr, r]) => Math.abs(t - tr) < 20 * SPB && memes(o, r))) continue;
      this.recents.push([t, o]); nouvelles.push(o);
    }
    this.recents = this.recents.filter(([t]) => this.t - t < TAUX_AIS);
    return nouvelles;
  }
}

class Canal {
  constructor(frequence, nom, fs) {
    this.frequence = frequence; this.nom = nom; this.nco = new NCO(fs);
    this.dec = new FirComplexe(passeBas(fs, LARGEUR_CANAL, 121), Math.round(fs / TAUX_AIS));
    this.pr = 1; this.pi = 0; this.demod = new DemodGMSK();
    this.messages = 0; this.crete = null; this.bruit = null; this.attente = null; this.actif = 0; this.decalage = null;
  }
  traiter(re, im, centre) {
    this.nco.freq = this.frequence - centre;
    const [a, b] = this.dec.traiter(...this.nco.melanger(re, im));
    if (!a.length) return [];
    // puissance sur des fenêtres de 2 ms : le plancher se mesure entre les salves
    let pmin = Infinity, pmax = -Infinity;
    for (let k = 0; k + 96 <= a.length; k += 96) {
      let p = 0; for (let j = k; j < k + 96; j++) p += a[j] * a[j] + b[j] * b[j];
      p = 10 * Math.log10(p / 96 + 1e-20); pmin = Math.min(pmin, p); pmax = Math.max(pmax, p);
    }
    if (pmin === Infinity) { pmin = pmax = -200; }
    this.bruit = this.bruit === null ? pmin : Math.min(pmin, this.bruit + 0.02);
    this.crete = this.crete === null ? pmax : Math.max(pmax, this.crete - 0.3);
    const fm = new Float64Array(a.length), k2 = TAUX_AIS / (2 * Math.PI);
    let pr = this.pr, pi = this.pi;
    for (let k = 0; k < a.length; k++) { const x = a[k], y = b[k]; fm[k] = Math.atan2(y * pr - x * pi, x * pr + y * pi) * k2; pr = x; pi = y; }
    this.pr = pr; this.pi = pi;
    if (pmax > this.bruit + 4) {
      this.actif = 2;
      const m = Float64Array.from(fm).sort()[fm.length >> 1];
      this.decalage = this.decalage === null ? m : 0.8 * this.decalage + 0.2 * m;
    }
    const sortie = [];
    if (this.actif > 0) {
      this.actif--;
      if (this.attente) sortie.push(...this.demod.traiter(this.attente));
      sortie.push(...this.demod.traiter(fm)); this.attente = null;
    } else {
      if (this.attente) this.demod.sauter(this.attente.length);
      this.attente = fm;
    }
    this.messages += sortie.length;
    return sortie;
  }
}

/** IQ de la clé → [canal, octets] des canaux AIS qui sont dans la bande reçue. */
export class MoteurAIS {
  constructor(centre, taux) {
    const lim = 0.42 * taux - 15000;
    this.dedans = CANAUX.filter(([f]) => Math.abs(f - centre) < lim);
    this.dehors = CANAUX.filter(([f]) => Math.abs(f - centre) >= lim);
    const d1 = Math.round(taux / 240_000);
    this.fs = taux / d1;
    this.nco = new NCO(taux); this.nco.freq = MILIEU - centre;
    this.dec = d1 > 1 ? new FirComplexe(passeBas(taux, 45_000, d1 * 6 + 1), d1) : null;
    this.canaux = this.dedans.map(([f, n]) => new Canal(f, n, this.fs));
  }
  traiter(re, im) {
    if (!this.canaux.length) return [];
    let [a, b] = this.nco.melanger(re, im);
    if (this.dec) [a, b] = this.dec.traiter(a, b);
    return this.canaux.flatMap(c => c.traiter(a, b, MILIEU).map(o => [c.nom, o]));
  }
}

// ---------------------------------------------------------------- messages
const TEXTE6 = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?";
export const STATUTS = { 0: "en route au moteur", 1: "au mouillage", 2: "non maître de sa manœuvre", 3: "manœuvrabilité restreinte",
  4: "handicapé par son tirant d'eau", 5: "amarré", 6: "échoué", 7: "en pêche", 8: "en route à la voile", 14: "AIS-SART (détresse)" };
const AIDES = { 1: "point de référence", 3: "feu", 5: "feu", 6: "feu", 9: "bouée cardinale nord", 10: "bouée cardinale est",
  11: "bouée cardinale sud", 12: "bouée cardinale ouest", 13: "bouée bâbord", 14: "bouée tribord", 19: "bouée de danger isolé",
  20: "bouée d'eaux saines", 21: "bouée spéciale", 22: "balise", 24: "bouée cardinale nord", 25: "bouée cardinale est",
  26: "bouée cardinale sud", 27: "bouée cardinale ouest", 28: "bouée bâbord", 29: "bouée tribord", 30: "bouée de danger isolé",
  31: "bouée d'eaux saines" };
const u = (b, a, n) => { let v = 0; for (let k = a; k < a + n; k++) v = v * 2 + (k < b.length ? b[k] : 0); return v; };
const s = (b, a, n) => { const v = u(b, a, n); return v >= 2 ** (n - 1) ? v - 2 ** n : v; };
const t6 = (b, a, n) => { let r = ""; for (let k = 0; k + 6 <= n && a + k + 6 <= b.length; k += 6) r += TEXTE6[u(b, a + k, 6)]; return r.split("@")[0].trim(); };
const position = (b, al, aa) => { const lon = s(b, al, 28) / 600000, lat = s(b, aa, 27) / 600000; return Math.abs(lon) > 180 || Math.abs(lat) > 90 ? null : [lat, lon]; };

export function categorie(t = 0) {
  if (t === 30) return "pêche";
  if (t === 36 || t === 37) return "voilier ou plaisance";
  if ([31, 32, 33, 34, 35, 50, 51, 52, 53, 54, 55, 58].includes(t)) return "service, remorqueur, pilote";
  if (t >= 40 && t <= 49) return "grande vitesse";
  if (t >= 60 && t <= 69) return "passagers";
  if (t >= 70 && t <= 79) return "cargo";
  if (t >= 80 && t <= 89) return "pétrolier, citerne";
  return "autre ou inconnu";
}
const COULEURS = { "pêche": "#ffb74d", "voilier ou plaisance": "#d98cff", "service, remorqueur, pilote": "#00e5ff",
  "grande vitesse": "#ffd166", "passagers": "#4fc3f7", "cargo": "#39ff9f", "pétrolier, citerne": "#ff5c5c", "autre ou inconnu": "#9fb3c8" };
export const couleurNavire = n => n.classe === "station de base" ? "#ffffff" : n.classe === "aide à la navigation" ? "#ffd166" : COULEURS[categorie(n.type_navire)];
export const nomNavire = n => n.nom || String(n.mmsi).padStart(9, "0");

/** Bits du message (poids fort d'abord) → champs ; null si trop court. */
export function decoderMessage(b) {
  if (b.length < 38) return null;
  const t = u(b, 0, 6), m = { type: t, mmsi: u(b, 8, 30) };
  if ([1, 2, 3].includes(t) && b.length >= 149)
    Object.assign(m, { statut: u(b, 38, 4), vitesse: u(b, 50, 10), position: position(b, 61, 89), route: u(b, 116, 12), cap: u(b, 128, 9), classe: "A" });
  else if ((t === 18 || t === 19) && b.length >= 142) {
    Object.assign(m, { vitesse: u(b, 46, 10), position: position(b, 57, 85), route: u(b, 112, 12), cap: u(b, 124, 9), classe: "B" });
    if (t === 19 && b.length >= 271) Object.assign(m, { nom: t6(b, 143, 120), type_navire: u(b, 263, 8) });
  } else if (t === 4 && b.length >= 134) {
    const p2 = (v, n = 2) => String(v).padStart(n, "0");
    Object.assign(m, { position: position(b, 79, 107), classe: "station de base",
      heure_utc: `${p2(u(b, 38, 14), 4)}-${p2(u(b, 52, 4))}-${p2(u(b, 56, 5))} ${p2(u(b, 61, 5))}:${p2(u(b, 66, 6))}:${p2(u(b, 72, 6))}` });
  } else if (t === 5 && b.length >= 422)
    Object.assign(m, { imo: u(b, 40, 30), indicatif: t6(b, 70, 42), nom: t6(b, 112, 120), type_navire: u(b, 232, 8),
      longueur: u(b, 240, 9) + u(b, 249, 9), largeur: u(b, 258, 6) + u(b, 264, 6), tirant: u(b, 294, 8) / 10,
      destination: t6(b, 302, 120), classe: "A" });
  else if (t === 24 && b.length >= 160) {
    if (u(b, 38, 2) === 0) Object.assign(m, { nom: t6(b, 40, 120), classe: "B" });
    else if (b.length >= 162) Object.assign(m, { type_navire: u(b, 40, 8), indicatif: t6(b, 90, 42),
      longueur: u(b, 132, 9) + u(b, 141, 9), largeur: u(b, 150, 6) + u(b, 156, 6), classe: "B" });
  } else if (t === 21 && b.length >= 219)
    Object.assign(m, { aide: u(b, 38, 5), nom: t6(b, 43, 120), position: position(b, 164, 192), classe: "aide à la navigation" });
  return m;
}

const virgule = x => String(x).replace(".", ",");
export function texteMessage(m, canal) {
  const r = [`${canal}  MMSI ${String(m.mmsi).padStart(9, "0")}`, `type ${m.type}`];
  if (m.nom) r.push(m.nom);
  if (m.indicatif) r.push(m.indicatif);
  if (m.vitesse !== undefined && m.vitesse !== 1023) r.push(`${virgule((m.vitesse / 10).toFixed(1))} nd`);
  if (m.route !== undefined && m.route < 3600) r.push(`route ${Math.round(m.route / 10)}°`);
  if (STATUTS[m.statut]) r.push(STATUTS[m.statut]);
  if (m.position) {
    const [la, lo] = m.position;
    r.push(`${virgule(Math.abs(la).toFixed(4))} ${la >= 0 ? "N" : "S"} ${virgule(Math.abs(lo).toFixed(4))} ${lo >= 0 ? "E" : "O"}`);
  }
  if (m.type_navire) r.push(categorie(m.type_navire));
  if (m.destination) r.push(`→ ${m.destination}`);
  if (AIDES[m.aide]) r.push(AIDES[m.aide]);
  if (m.heure_utc) r.push(`${m.heure_utc} UTC`);
  return r.join(" · ");
}

/** Charge utile d'une phrase !AIVDM → bits du message (essais). */
export function bitsDepuisNmea(charge, remplissage = 0) {
  const bits = [];
  for (const c of charge) { let v = c.charCodeAt(0) - 48; if (v > 40) v -= 8; for (let k = 5; k >= 0; k--) bits.push((v >> k) & 1); }
  return remplissage ? bits.slice(0, bits.length - remplissage) : bits;
}
