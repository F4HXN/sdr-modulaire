// SPDX-License-Identifier: MIT
// ADS-B (Mode S étendu, 1090 MHz) : démodulation PPM, contrôle CRC-24, décodage des
// positions CPR, de l'indicatif, de l'altitude et de la vitesse, suivi des avions.
// Un encodeur est fourni pour la source simulée et les tests.

const POLY = 0xfff409;
const TABLE_CRC = new Int32Array(256).map((_, i) => {
  let c = i << 16;
  for (let k = 0; k < 8; k++) c = (c & 0x800000 ? (c << 1) ^ POLY : c << 1) & 0xffffff;
  return c;
});
/** Reste CRC-24 Mode S des n premiers octets. */
function crc(octets, n) {
  let c = 0;
  for (let i = 0; i < n; i++) c = ((c << 8) & 0xffffff) ^ TABLE_CRC[((c >> 16) ^ octets[i]) & 255];
  return c;
}
/** Syndrome d'un message de 112 bits : 0 si intact. */
function syndrome(o) { return crc(o, 11) ^ ((o[11] << 16) | (o[12] << 8) | o[13]); }

// Syndromes des erreurs d'un seul bit (bits 5 à 111 : le type DF n'est jamais corrigé).
const UN_BIT = new Map();
{
  const o = new Uint8Array(14);
  for (let b = 5; b < 112; b++) { o.fill(0); o[b >> 3] = 0x80 >> (b & 7); UN_BIT.set(syndrome(o), b); }
}

const bitsDe = (o, debut, n) => { let v = 0; for (let b = debut; b < debut + n; b++) v = v * 2 + ((o[b >> 3] >> (7 - (b & 7))) & 1); return v; };

/**
 * Démodulateur Mode S : reçoit les IQ bruts, appelle emettre(octets[14], corrige) pour
 * chaque message DF17/DF18 dont le CRC est bon. Fonctionne à partir de 1,92 MS/s ;
 * 2,4 MS/s donne les meilleurs résultats.
 */
export class DemodModeS {
  constructor(taux, emettre) {
    this.taux = taux; this.emettre = emettre;
    this.s = taux / 1e6;                         // échantillons par microseconde
    this.garde = Math.ceil(122 * this.s) + 2;    // préambule + 112 bits
    this.reste = new Float32Array(0);
    this.bruit = 0.01;
    this.messages = 0; this.corriges = 0;
    // Diagnostic (panneau du module) : préambules vus, messages Mode S d'autres types, CRC faux, crête.
    this.preambules = 0; this.autresDF = 0; this.crcFaux = 0; this.crete = 0;
  }

  traiter(re, im) {
    const r = this.reste, n = r.length + re.length;
    const m = new Float32Array(n); m.set(r);
    for (let k = 0; k < re.length; k++) m[r.length + k] = Math.hypot(re[k], im[k]);
    const C = new Float64Array(n + 1);
    for (let k = 0; k < n; k++) C[k + 1] = C[k] + m[k];
    // Somme des amplitudes sur [a, b[ en échantillons fractionnaires.
    const F = x => { const i = Math.floor(x); return C[i] + (x - i) * m[i]; };
    const E = (a, b) => F(b) - F(a);

    // Plancher de bruit : médiane approchée (moyenne des 3/4 les plus faibles d'un échantillonnage).
    const ech = []; for (let k = 0; k < n; k += 97) ech.push(m[k]);
    ech.sort((a, b) => a - b);
    const plancher = ech.length ? ech[Math.floor(ech.length / 2)] : 0;
    this.bruit = 0.9 * this.bruit + 0.1 * plancher;
    let crete = 0; for (let k = 0; k < n; k += 7) if (m[k] > crete) crete = m[k];
    this.crete = 0.8 * this.crete + 0.2 * crete;

    const s = this.s, demi = s / 2, seuil = this.bruit * 2.2, fin = n - this.garde;
    const phases = s > 2.2 ? [0, 0.4, 0.8] : [0, 0.5];
    const o = new Uint8Array(14);
    let k = 0;
    while (k < fin) {
      // Test rapide : une impulsion au début, au-dessus du bruit.
      if (m[k] < seuil && m[k + 1] < seuil) { k++; continue; }
      let trouve = false, vu = false;
      for (const ph of phases) {
        const p = k + ph;
        const i1 = E(p, p + demi), i2 = E(p + s, p + s + demi), i3 = E(p + 3.5 * s, p + 4 * s), i4 = E(p + 4.5 * s, p + 5 * s);
        const haut = Math.min(i1, i2, i3, i4) / demi;
        // Silences loin des impulsions (le filtre de la clé étale les fronts).
        const calme = (E(p + 2 * s, p + 3 * s) + E(p + 5.75 * s, p + 7.5 * s)) / (2.75 * s);
        if (haut < seuil || haut < 2 * calme) continue;
        if (!vu) { this.preambules++; vu = true; }   // une seule fois par position, quelle que soit la phase
        // 112 bits : un 1 met l'énergie dans la première moitié de la microseconde.
        o.fill(0);
        let faibles = 0;
        for (let b = 0; b < 112; b++) {
          const d = p + (8 + b) * s, a = E(d, d + demi), c = E(d + demi, d + s);
          if (a > c) o[b >> 3] |= 0x80 >> (b & 7);
          if (Math.abs(a - c) < 0.2 * haut * demi) faibles++;
        }
        const df = o[0] >> 3;
        if (df !== 17 && df !== 18) {
          // DF11 (squitter court, 56 bits) : CRC des 32 premiers bits, à l'identifiant d'interrogateur près
          if (df === 11 && ((crc(o, 4) ^ ((o[4] << 16) | (o[5] << 8) | o[6])) & ~0x7f) === 0) this.autresDF++;
          continue;
        }
        if (faibles > 40) continue;
        let corrige = false;
        const syn = syndrome(o);
        if (syn) {
          if (faibles > 10) continue;          // correction réservée aux messages nets
          const b = UN_BIT.get(syn);
          if (b === undefined) { this.crcFaux++; continue; }
          o[b >> 3] ^= 0x80 >> (b & 7); corrige = true; this.corriges++;
        }
        this.messages++;
        this.emettre(o.slice(), corrige);
        trouve = true; break;
      }
      k += trouve ? Math.floor(120 * s) : 1;
    }
    this.reste = m.slice(Math.max(0, Math.min(k, fin)));
  }
}

// ------------------------------------------------------------------ CPR
const NZ = 15;
export function NL(lat) {
  const a = Math.abs(lat);
  if (a >= 87) return a > 87 ? 1 : 2;
  if (a < 1e-9) return 59;
  return Math.floor(2 * Math.PI / Math.acos(1 - (1 - Math.cos(Math.PI / (2 * NZ))) / Math.cos(Math.PI / 180 * a) ** 2));
}
const mod = (a, b) => a - b * Math.floor(a / b);

/** Décodage global à partir d'une paire paire/impaire (champs CPR bruts sur 17 bits). */
export function cprGlobal(pair, impair, dernierImpair) {
  const latE = pair.lat / 131072, lonE = pair.lon / 131072, latO = impair.lat / 131072, lonO = impair.lon / 131072;
  const j = Math.floor(59 * latE - 60 * latO + 0.5);
  let rlatE = 360 / 60 * (mod(j, 60) + latE), rlatO = 360 / 59 * (mod(j, 59) + latO);
  if (rlatE >= 270) rlatE -= 360;
  if (rlatO >= 270) rlatO -= 360;
  if (NL(rlatE) !== NL(rlatO)) return null;
  const lat = dernierImpair ? rlatO : rlatE;
  const nl = NL(lat), ni = Math.max(nl - (dernierImpair ? 1 : 0), 1);
  const m = Math.floor(lonE * (nl - 1) - lonO * nl + 0.5);
  let lon = 360 / ni * (mod(m, ni) + (dernierImpair ? lonO : lonE));
  if (lon >= 180) lon -= 360;
  return [lat, lon];
}

/** Décodage local autour d'une position de référence (à moins de 180 NM). */
export function cprLocal(cpr, impair, latRef, lonRef) {
  const dlat = 360 / (60 - (impair ? 1 : 0)), yz = cpr.lat / 131072, xz = cpr.lon / 131072;
  const j = Math.floor(latRef / dlat) + Math.floor(0.5 + mod(latRef, dlat) / dlat - yz);
  const lat = dlat * (j + yz);
  const ni = Math.max(NL(lat) - (impair ? 1 : 0), 1), dlon = 360 / ni;
  const m = Math.floor(lonRef / dlon) + Math.floor(0.5 + mod(lonRef, dlon) / dlon - xz);
  return [lat, dlon * (m + xz)];
}

export function distanceKm(lat1, lon1, lat2, lon2) {
  const r = Math.PI / 180, a = Math.sin((lat2 - lat1) * r / 2) ** 2 + Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin((lon2 - lon1) * r / 2) ** 2;
  return 12742 * Math.asin(Math.sqrt(a));
}

// ------------------------------------------------------------------ messages
const CARACTERES = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######";

/** Décode un message DF17/18 en un objet simple. */
export function decoder(o) {
  const icao = bitsDe(o, 8, 24).toString(16).toUpperCase().padStart(6, "0");
  const tc = bitsDe(o, 32, 5), r = { icao, tc };
  if (tc >= 1 && tc <= 4) {
    let t = ""; for (let i = 0; i < 8; i++) t += CARACTERES[bitsDe(o, 40 + 6 * i, 6)];
    r.indicatif = t.replace(/#/g, "").trim();
  } else if ((tc >= 9 && tc <= 18) || (tc >= 20 && tc <= 22)) {
    const alt = bitsDe(o, 40, 12);
    if (tc <= 18 && alt && (alt & 0x10)) r.altitude = (((alt & 0xfe0) >> 1) | (alt & 0xf)) * 25 - 1000;
    if (tc >= 20) r.altitude = Math.round(alt * 3.28084);   // altitude GNSS en mètres
    r.impair = bitsDe(o, 53, 1) === 1;
    r.cpr = { lat: bitsDe(o, 54, 17), lon: bitsDe(o, 71, 17) };
  } else if (tc === 19) {
    const st = bitsDe(o, 37, 3);
    if (st === 1 || st === 2) {
      const f = st === 2 ? 4 : 1;
      const vew = bitsDe(o, 46, 10), vns = bitsDe(o, 57, 10);
      if (vew && vns) {
        const x = (vew - 1) * f * (bitsDe(o, 45, 1) ? -1 : 1), y = (vns - 1) * f * (bitsDe(o, 56, 1) ? -1 : 1);
        r.vitesse = Math.round(Math.hypot(x, y));
        r.cap = Math.round(mod(Math.atan2(x, y) * 180 / Math.PI, 360));
      }
    }
    const vr = bitsDe(o, 69, 9);
    if (vr) r.vz = (vr - 1) * 64 * (bitsDe(o, 68, 1) ? -1 : 1);
  }
  return r;
}

/** Suivi des avions : position, indicatif, altitude, vitesse, trace. */
export class Trafic {
  constructor() { this.avions = new Map(); this.station = null; }

  recevoir(octets, maintenant = Date.now()) {
    const d = decoder(octets);
    let a = this.avions.get(d.icao);
    if (!a) { a = { icao: d.icao, messages: 0, trace: [] }; this.avions.set(d.icao, a); }
    a.messages++; a.vu = maintenant;
    if (d.indicatif) a.indicatif = d.indicatif;
    if (d.altitude !== undefined) a.altitude = d.altitude;
    if (d.vitesse !== undefined) { a.vitesse = d.vitesse; a.cap = d.cap; }
    if (d.vz !== undefined) a.vz = d.vz;
    if (d.cpr) this.position(a, d, maintenant);
    return a;
  }

  position(a, d, t) {
    a[d.impair ? "cprO" : "cprE"] = { ...d.cpr, t };
    let pos = null;
    if (a.lat !== undefined && t - a.tPos < 30000) pos = cprLocal(d.cpr, d.impair, a.lat, a.lon);
    else if (a.cprE && a.cprO && Math.abs(a.cprE.t - a.cprO.t) < 10000) pos = cprGlobal(a.cprE, a.cprO, d.impair);
    else if (this.station) pos = cprLocal(d.cpr, d.impair, this.station.lat, this.station.lon);
    if (!pos || !Number.isFinite(pos[0])) return;
    // Rejette les sauts impossibles (décodage ambigu).
    if (a.lat !== undefined && distanceKm(a.lat, a.lon, pos[0], pos[1]) > 2 + (t - a.tPos) / 1000 * 0.4) return;
    if (this.station && a.lat === undefined && distanceKm(this.station.lat, this.station.lon, pos[0], pos[1]) > 600) return;
    [a.lat, a.lon] = pos; a.tPos = t;
    const der = a.trace[a.trace.length - 1];
    if (!der || t - der[2] > 2000) { a.trace.push([pos[0], pos[1], t]); if (a.trace.length > 400) a.trace.shift(); }
  }

  nettoyer(maintenant = Date.now(), delai = 60000) {
    for (const [k, a] of this.avions) if (maintenant - a.vu > delai) this.avions.delete(k);
  }
}

// ------------------------------------------------------------------ encodeur (simulation, tests)
function versOctets(bits) {
  const o = new Uint8Array(14);
  for (let b = 0; b < 88; b++) if (bits[b]) o[b >> 3] |= 0x80 >> (b & 7);
  const c = crc(o, 11); o[11] = c >> 16; o[12] = (c >> 8) & 255; o[13] = c & 255;
  return o;
}
function entete(icao) { const b = []; const pousser = (v, n) => { for (let i = n - 1; i >= 0; i--) b.push(Math.floor(v / 2 ** i) % 2); }; pousser(17, 5); pousser(5, 3); pousser(icao, 24); return [b, pousser]; }

export function encoderIndicatif(icao, indicatif) {
  const [b, p] = entete(icao); p(4, 5); p(0, 3);
  const t = indicatif.toUpperCase().padEnd(8, " ").slice(0, 8);
  for (const c of t) p(Math.max(0, CARACTERES.indexOf(c)), 6);
  return versOctets(b);
}

export function encoderPosition(icao, lat, lon, altitude, impair) {
  const [b, p] = entete(icao); p(11, 5); p(0, 2); p(0, 1);
  const n = Math.round((altitude + 1000) / 25);
  p(((n & 0x7f0) << 1) | 0x10 | (n & 0xf), 12); p(0, 1); p(impair ? 1 : 0, 1);
  const i = impair ? 1 : 0, dlat = 360 / (60 - i);
  const yz = Math.floor(131072 * mod(lat, dlat) / dlat + 0.5);
  const rlat = dlat * (yz / 131072 + Math.floor(lat / dlat));
  const dlon = 360 / Math.max(NL(rlat) - i, 1);
  const xz = Math.floor(131072 * mod(lon, dlon) / dlon + 0.5);
  p(yz & 0x1ffff, 17); p(xz & 0x1ffff, 17);
  return versOctets(b);
}

export function encoderVitesse(icao, vitesse, cap, vz) {
  const [b, p] = entete(icao); p(19, 5); p(1, 3); p(0, 1); p(0, 1); p(0, 3);
  const x = vitesse * Math.sin(cap * Math.PI / 180), y = vitesse * Math.cos(cap * Math.PI / 180);
  p(x < 0 ? 1 : 0, 1); p(Math.round(Math.abs(x)) + 1, 10);
  p(y < 0 ? 1 : 0, 1); p(Math.round(Math.abs(y)) + 1, 10);
  p(0, 1); p(vz < 0 ? 1 : 0, 1); p(Math.round(Math.abs(vz) / 64) + 1, 9);
  p(0, 2); p(0, 1); p(0, 7);
  return versOctets(b);
}

/** Durée d'un message en secondes (préambule 8 µs + 112 µs). */
export const DUREE_MESSAGE = 120e-6;
/** Niveau (0 ou 1) d'un message à l'instant dt (µs) depuis son début. */
export function impulsion(octets, dt) {
  if (dt < 8) return (dt < 0.5 || (dt >= 1 && dt < 1.5) || (dt >= 3.5 && dt < 4) || (dt >= 4.5 && dt < 5)) ? 1 : 0;
  const b = Math.floor(dt - 8); if (b >= 112) return 0;
  const un = (octets[b >> 3] >> (7 - (b & 7))) & 1;
  return (dt - 8 - b < 0.5) === (un === 1) ? 1 : 0;
}
