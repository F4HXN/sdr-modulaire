// SPDX-License-Identifier: MIT
// Décodeur DMR (données) : portage du module decodeur_dmr.py de SDR Modulaire, même algorithme.
// discriminateur FM à 48 kHz (Hz) → racine de cosinus surélevé (α = 0,2) → synchros de 24 symboles
// par corrélation normalisée → niveaux lus sur la synchro → salve de 132 symboles → codes ETSI
// TS 102 361-1 : Golay (20,8), BPTC (196,96), Reed-Solomon (12,9), CRC-CCITT, QR (16,7), LC intégré,
// treillis 3/4, CRC-32. Les salves vocales B à F sont lues 60 à 300 ms après la synchro A du slot.

export const TAUX_DMR = 48000;
const SPS = 10, SLOT = 2880, SEUIL = 0.9;

const DIBIT_SYMBOLE = [1, 3, -1, -3];                 // dibit 00 → +1, 01 → +3, 10 → −1, 11 → −3
const symboleDibit = s => (s === 3 ? 1 : s === 1 ? 0 : s === -1 ? 2 : 3);
const symbolesDe = hex => [...hex].flatMap(c => { const v = parseInt(c, 16); return [DIBIT_SYMBOLE[v >> 2], DIBIT_SYMBOLE[v & 3]]; });

const SYNCS = [
  ["755FD7DF75F7", "relais", true, 0], ["DFF57D75DF5D", "relais", false, 0],
  ["7F7D5DD57DFD", "mobile", true, 0], ["D5D7F77FD757", "mobile", false, 0],
  ["5D577F7757FF", "direct", true, 1], ["F7FDD5DDFD55", "direct", false, 1],
  ["7DFFD5F55D5F", "direct", true, 2], ["D7557F5FF7F5", "direct", false, 2],
];
const MOTIFS = SYNCS.map(([hex, source, voix, slot]) => {
  const m = Float32Array.from(symbolesDe(hex), s => s / 3);
  return { m, somme: m.reduce((a, b) => a + b, 0), source, voix, slot };
});

// ---------------------------------------------------------------- codes
const GOLAY = [0xB08E, 0xE093, 0x70A9, 0x60DC, 0x7036, 0xD06C, 0x90D9, 0xA03D];
const QR = [0x0273, 0x04E5, 0x09C9, 0x11E2, 0x21B7, 0x411E, 0x804F];
const combine = (base, v) => base.reduce((x, b, i) => (v >> i & 1 ? x ^ b : x), 0);
const golayMot = d => { const c = combine(GOLAY, d); return d << 12 | (c & 0xFF) << 4 | (c >> 12) & 0xF; };
const GOLAY_MOTS = Array.from({ length: 256 }, (_, d) => golayMot(d));
const QR_MOTS = Array.from({ length: 128 }, (_, v) => combine(QR, v));
const popcount = x => { let n = 0; while (x) { x &= x - 1; n++; } return n; };
function plusProche(mots, w, max) {
  let best = -1, dist = 99;
  for (let i = 0; i < mots.length; i++) { const d = popcount(mots[i] ^ w); if (d < dist) { dist = d; best = i; } }
  return dist <= max ? best : null;
}

const H15 = [[0, 1, 2, 3, 5, 7, 8], [1, 2, 3, 4, 6, 8, 9], [2, 3, 4, 5, 7, 9, 10], [0, 1, 2, 4, 6, 7, 10]];
const H13 = [[0, 1, 3, 5, 6], [0, 1, 2, 4, 6, 7], [0, 1, 2, 3, 5, 7, 8], [0, 2, 4, 5, 8]];
const H16 = [...H15, [0, 2, 5, 6, 8, 9, 10]];
function syndromes(eqs, k) {
  const t = new Map();
  for (let p = 0; p < k; p++) { let s = 0; eqs.forEach((e, j) => { if (e.includes(p)) s |= 1 << j; }); t.set(s, p); }
  eqs.forEach((_, j) => t.set(1 << j, k + j));
  return t;
}
const S15 = syndromes(H15, 11), S13 = syndromes(H13, 9), S16 = syndromes(H16, 11);
/** Corrige une erreur en place dans d (tableau). 0 : juste, 1 : corrigé, −1 : incorrigible. */
function hamming(d, eqs, table) {
  const k = d.length - eqs.length;
  let s = 0;
  eqs.forEach((e, j) => { let x = d[k + j]; for (const i of e) x ^= d[i]; if (x & 1) s |= 1 << j; });
  if (!s) return 0;
  const p = table.get(s);
  if (p === undefined) return -1;
  d[p] ^= 1;
  return 1;
}

export function bitsOctets(bits) {
  const o = new Uint8Array(bits.length >> 3);
  for (let i = 0; i < o.length; i++) { let v = 0; for (let k = 0; k < 8; k++) v = v << 1 | bits[8 * i + k]; o[i] = v; }
  return o;
}

/** BPTC (196,96) : 196 bits reçus → 12 octets. */
export function bptcDecoder(brut, corriger = true) {
  const m = new Array(196);
  for (let a = 0; a < 196; a++) m[a] = brut[(a * 181) % 196];
  if (corriger) {
    for (let passe = 0; passe < 5; passe++) {
      let fait = false;
      for (let r = 0; r < 9; r++) {
        const l = m.slice(r * 15 + 1, r * 15 + 16);
        if (hamming(l, H15, S15) === 1) { for (let i = 0; i < 15; i++) m[r * 15 + 1 + i] = l[i]; fait = true; }
      }
      for (let c = 0; c < 15; c++) {
        const col = Array.from({ length: 13 }, (_, k) => m[c + 1 + 15 * k]);
        if (hamming(col, H13, S13) === 1) { for (let k = 0; k < 13; k++) m[c + 1 + 15 * k] = col[k]; fait = true; }
      }
      if (!fait) break;
    }
  }
  const bits = m.slice(4, 12);
  for (let r = 1; r < 9; r++) bits.push(...m.slice(r * 15 + 1, r * 15 + 12));
  return bitsOctets(bits);
}

const EXP = new Uint8Array(512), LOG = new Uint8Array(256);
for (let i = 0, x = 1; i < 255; i++) { EXP[i] = x; LOG[x] = i; x <<= 1; if (x & 0x100) x ^= 0x11D; }
for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
const gmul = (a, b) => (a && b ? EXP[LOG[a] + LOG[b]] : 0);
function rsParite(o) {
  let r = [0, 0, 0];
  for (let i = 0; i < 9; i++) { const f = o[i] ^ r[0]; r = [r[1] ^ gmul(14, f), r[2] ^ gmul(56, f), gmul(64, f)]; }
  return r;
}
const lcValide = (o, masque) => { const p = rsParite(o); return p.every((v, i) => (o[9 + i] ^ masque) === v); };

function crcCCITT(o, n) {
  let c = 0;
  for (let i = 0; i < n; i++) { c ^= o[i] << 8; for (let k = 0; k < 8; k++) c = c & 0x8000 ? ((c << 1) ^ 0x1021) & 0xFFFF : (c << 1) & 0xFFFF; }
  return c ^ 0xFFFF;
}
const crc16Valide = (o, masque) => (crcCCITT(o, 10) ^ masque) === (o[10] << 8 | o[11]);

/** CRC-32 des paquets de données : octets pris par paires inversées, résultat octets inversés. */
export function crc32DMR(o, n) {
  let c = 0;
  for (let i = 0; i + 1 < n; i += 2) {
    for (const x of [o[i + 1], o[i]]) {
      for (let k = 7; k >= 0; k--) c = (((c >>> 31) ^ (x >> k)) & 1) ? ((c << 1) ^ 0x04C11DB7) >>> 0 : (c << 1) >>> 0;
    }
  }
  return ((c & 0xFF) << 24 | (c >>> 8 & 0xFF) << 16 | (c >>> 16 & 0xFF) << 8 | c >>> 24) >>> 0;
}

const LC_POS = [];
for (const [a, b] of [[0, 11], [16, 27], [32, 42], [48, 58], [64, 74], [80, 90], [96, 106]]) for (let i = a; i < b; i++) LC_POS.push(i);
const CRC_POS = [42, 58, 74, 90, 106];
const LC_ORDRE = []; for (let a = 0, b = 0; a < 128; a++) { LC_ORDRE.push(b); b += 16; if (b > 127) b -= 127; }
function lcIntegre(brut) {
  const m = new Array(128);
  brut.forEach((b, a) => { m[LC_ORDRE[a]] = b; });
  for (let r = 0; r < 7; r++) {
    const l = m.slice(r * 16, r * 16 + 16);
    if (hamming(l, H16, S16) < 0) return null;
    for (let i = 0; i < 16; i++) m[r * 16 + i] = l[i];
  }
  for (let c = 0; c < 16; c++) { let p = 0; for (let r = 0; r < 8; r++) p ^= m[c + 16 * r]; if (p) return null; }
  const o = bitsOctets(LC_POS.map(p => m[p]));
  const crc = CRC_POS.reduce((v, p, i) => v | m[p] << (4 - i), 0);
  return o.reduce((a, b) => a + b, 0) % 31 === crc ? o : null;
}

const ENTRELACEMENT = [];
for (let d = 0; d < 8; d += 2) for (let j = 0; j < (d ? 12 : 13); j++) ENTRELACEMENT.push(8 * j + d, 8 * j + d + 1);
const CODE_1 = [0, 8, 4, 12, 2, 10, 6, 14, 4, 12, 2, 10, 6, 14, 0, 8, 1, 9, 5, 13, 3, 11, 7, 15, 5, 13, 3, 11, 7, 15, 1, 9,
  3, 11, 7, 15, 1, 9, 5, 13, 7, 15, 3, 11, 5, 13, 1, 9, 2, 10, 6, 14, 0, 8, 4, 12, 6, 14, 0, 8, 4, 12, 2, 10];
// Ligne 5 : les sources diffèrent ; le message est décodé avec la seconde si son CRC-32 est faux.
const CODE_2 = [...CODE_1.slice(0, 40), 7, 15, 1, 9, 5, 13, 3, 11, ...CODE_1.slice(48)];
const POINTS = [[1, -1], [-1, -1], [3, -3], [-3, -3], [-3, -1], [3, -1], [-1, -3], [1, -3], [-3, 3], [3, 3], [-1, 1], [1, 1], [1, 3], [-1, 3], [3, 1], [-3, 1]];
/** Treillis 3/4 : 98 symboles (valeurs souples) → 18 octets, par Viterbi. */
function treillis(souples, code) {
  const d = new Array(98);
  souples.forEach((v, i) => { d[ENTRELACEMENT[i]] = v; });
  let cout = [0, Infinity, Infinity, Infinity, Infinity, Infinity, Infinity, Infinity];
  let chemins = Array.from({ length: 8 }, () => []);
  for (let j = 0; j < 49; j++) {
    const r1 = d[2 * j], r2 = d[2 * j + 1], nouv = new Array(8).fill(Infinity), prec = new Array(8).fill(-1);
    for (let s = 0; s < 8; s++) {
      if (cout[s] === Infinity) continue;
      for (let t = 0; t < (j < 48 ? 8 : 1); t++) {
        const [p1, p2] = POINTS[code[s * 8 + t]], c = cout[s] + (r1 - p1) ** 2 + (r2 - p2) ** 2;
        if (c < nouv[t]) { nouv[t] = c; prec[t] = s; }
      }
    }
    chemins = prec.map((s, t) => (s >= 0 ? [...chemins[s], t] : []));
    cout = nouv;
  }
  if (cout[0] === Infinity || cout[0] / 98 > 1) return null;
  return bitsOctets(chemins[0].slice(0, 48).flatMap(t => [t >> 2 & 1, t >> 1 & 1, t & 1]));
}

// ---------------------------------------------------------------- interprétation
export const FID = { 0x04: "Flyde", 0x08: "Hytera", 0x10: "Motorola", 0x58: "Tait", 0x68: "Hytera", 0x77: "Vertex" };
export const SAP = { 0: "UDT", 2: "TCP comprimé", 3: "UDP comprimé", 4: "IP", 5: "ARP", 9: "propriétaire", 10: "données courtes" };
const PORTS = { 4001: "position (LRRP)", 4004: "XCMP", 4005: "ARS (enregistrement)", 4007: "SMS (TMS)", 4008: "télémétrie", 5016: "SMS Hytera", 3002: "position Hytera" };
const entier24 = (o, i) => o[i] << 16 | o[i + 1] << 8 | o[i + 2];

function lcVersAppel(o) {
  const flco = o[0] & 0x3F, so = o[2];
  if (flco !== 0 && flco !== 3) return null;
  return { groupe: flco === 0, dst: entier24(o, 3), src: entier24(o, 6), fid: o[1], chiffre: !!(so & 0x40), urgence: !!(so & 0x80) };
}

/** Plus long passage lisible en UTF-16 (LE ou BE) d'un paquet : le texte d'un SMS. */
function texteUtf16(o) {
  let meilleur = "";
  for (const le of [true, false]) {
    for (const depart of [0, 1]) {
      let courant = "";
      for (let k = depart; k + 1 < o.length; k += 2) {
        const c = le ? o[k] | o[k + 1] << 8 : o[k] << 8 | o[k + 1];
        if (c >= 0x20 && c !== 0x7F && c !== 0xFFFF && !(c >= 0xD800 && c <= 0xDFFF)) {
          courant += String.fromCharCode(c);
          if (courant.length > meilleur.length) meilleur = courant;
        } else courant = "";
      }
    }
  }
  if ([...meilleur].some(c => c >= " " && c < "ƀ")) {
    while (meilleur && !(meilleur[0] >= "!" && meilleur[0] < "ƀ" && meilleur[0] !== "\xa0")) meilleur = meilleur.slice(1);
  }
  meilleur = meilleur.trim();
  return meilleur.length >= 2 ? meilleur : "";
}

function paquetVersTexte(sap, o) {
  if (sap === 4 && o.length >= 28 && o[0] >> 4 === 4 && o[9] === 17) {
    const ihl = (o[0] & 15) * 4, sport = o[ihl] << 8 | o[ihl + 1], dport = o[ihl + 2] << 8 | o[ihl + 3];
    const nature = PORTS[dport] || PORTS[sport] || `UDP ${dport}`;
    if ([4007, 5016].includes(dport) || [4007, 5016].includes(sport)) {
      const t = texteUtf16(o.subarray(ihl + 8));
      return t ? `SMS : « ${t} »` : nature;
    }
    return nature;
  }
  const t = texteUtf16(o);
  if (t.length >= 3) return `texte : « ${t} »`;
  const a = [...o].filter(c => c >= 32 && c < 127).map(c => String.fromCharCode(c)).join("");
  return a.length >= Math.max(4, o.length / 2) ? `texte : « ${a} »` : `${o.length} octets (${SAP[sap] ?? sap})`;
}

// ---------------------------------------------------------------- démodulateur
function rrc(alpha, sps, portee) {
  const n = 2 * portee * sps + 1, h = new Float32Array(n);
  let somme = 0;
  for (let i = 0; i < n; i++) {
    const x = (i - portee * sps) / sps;
    let v;
    if (Math.abs(x) < 1e-9) v = 1 - alpha + 4 * alpha / Math.PI;
    else if (Math.abs(Math.abs(4 * alpha * x) - 1) < 1e-9) v = alpha / Math.SQRT2 * ((1 + 2 / Math.PI) * Math.sin(Math.PI / (4 * alpha)) + (1 - 2 / Math.PI) * Math.cos(Math.PI / (4 * alpha)));
    else v = (Math.sin(Math.PI * x * (1 - alpha)) + 4 * alpha * x * Math.cos(Math.PI * x * (1 + alpha))) / (Math.PI * x * (1 - (4 * alpha * x) ** 2));
    h[i] = v; somme += v;
  }
  return h.map(v => v / somme);
}

const nouveauSlot = () => ({ appel: null, frag: [], etape: 0, alias: null, donnees: null, blocs: [], chiffre: false, voixDebut: null });

export class DecodeurDMR {
  constructor() {
    this.h = rrc(0.2, SPS, 8);
    this.histo = new Float32Array(this.h.length - 1);
    this.buf = new Float32Array(0); this.t0 = 0; this.cherche = 0;
    this.prevues = [];
    this.slots = [nouveauSlot(), nouveauSlot(), nouveauSlot()];
    this.salves = 0; this.cc = null; this.source = null; this.csbk = 0; this.ev = [];
  }

  /** fm : discriminateur en Hz à 48 kHz → évènements. */
  traiter(fm) {
    const h = this.h, n = h.length, b = new Float32Array(this.histo.length + fm.length);
    b.set(this.histo); b.set(fm, this.histo.length);
    const y = new Float32Array(fm.length);
    for (let o = 0; o < fm.length; o++) { let acc = 0; for (let j = 0; j < n; j++) acc += b[o + j] * h[n - 1 - j]; y[o] = acc; }
    this.histo = b.slice(b.length - (n - 1));
    const nb = new Float32Array(this.buf.length + y.length); nb.set(this.buf); nb.set(y, this.buf.length); this.buf = nb;
    this.ev = [];
    const fin = this.t0 + this.buf.length, avant = 89 * SPS, apres = 54 * SPS + 8;
    const debut = Math.max(this.cherche, this.t0 + avant);
    if (fin - apres > debut) this.chercher(debut, fin - apres);
    this.prevues.sort((a, c) => a[0] - c[0]);
    const restent = [];
    for (const p of this.prevues) {
      if (p[0] + apres <= fin) { if (p[0] - avant >= this.t0) this.salveVocale(...p); } else restent.push(p);
    }
    this.prevues = restent;
    this.slots.forEach((etat, s) => { if (etat.appel && fin - etat.appel.dernier > TAUX_DMR) this.finAppel(s, etat.appel.dernier); });
    const garde = Math.min(this.cherche - avant - 16, ...this.prevues.map(p => p[0] - avant - 16));
    if (garde > this.t0) { this.buf = this.buf.slice(garde - this.t0); this.t0 = garde; }
    return this.ev;
  }

  chercher(a, b) {
    const y = this.buf, i0 = a - this.t0, n = b - a;
    const idx = Array.from({ length: 24 }, (_, k) => i0 - (23 - k) * SPS);
    const moyenne = new Float64Array(n), energie = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      let s = 0, q = 0;
      for (const j of idx) { const v = y[j + i]; s += v; q += v * v; }
      moyenne[i] = s / 24; energie[i] = Math.max(q - 24 * moyenne[i] ** 2, 1e-9);
    }
    const meilleur = new Float64Array(n).fill(-1), quel = new Int8Array(n);
    MOTIFS.forEach(({ m, somme }, k) => {
      const norme = 24 - somme * somme / 24;
      for (let i = 0; i < n; i++) {
        let s = 0;
        for (let q = 0; q < 24; q++) s += m[q] * y[idx[q] + i];
        const c = (s - moyenne[i] * somme) / Math.sqrt(energie[i] * norme);
        if (c > meilleur[i]) { meilleur[i] = c; quel[i] = k; }   // signé : voix et données sont des motifs inverses
      }
    });
    let k = 0;
    while (k < n) {
      if (meilleur[k] < SEUIL) { k++; continue; }
      let j = k;
      for (let q = k; q < Math.min(n, k + 2 * SPS); q++) if (meilleur[q] > meilleur[j]) j = q;
      this.salve(a + j, quel[j]);
      k = j + 120 * SPS;
    }
    this.cherche = Math.max(b, a + k);
  }

  lire(n, premier, dernier, [milieu, pas]) {
    const out = new Float32Array(dernier - premier + 1), base = n - this.t0;
    for (let s = premier; s <= dernier; s++) out[s - premier] = (this.buf[base + (s - 77) * SPS] - milieu) / pas;
    return out;
  }

  static bits(sym) {
    const bits = [];
    for (const v of sym) { const s = [3, 1, -1, -3].reduce((m, c) => (Math.abs(c - v) < Math.abs(m - v) ? c : m)); const d = symboleDibit(s); bits.push(d >> 1, d & 1); }
    return bits;
  }

  salve(n, k) {
    const { m, source, voix } = MOTIFS[k];
    let { slot } = MOTIFS[k];
    let haut = 0, bas = 0, nh = 0, nbas = 0;
    for (let s = 0; s < 24; s++) {
      const v = this.buf[n - this.t0 + (54 + s - 77) * SPS];
      if (m[s] > 0) { haut += v; nh++; } else { bas += v; nbas++; }
    }
    haut /= nh; bas /= nbas;
    if (haut - bas < 1e-3) return;
    const niveaux = [(haut + bas) / 2, (haut - bas) / 6];
    this.salves++; this.source = source;
    if (source === "relais") slot = DecodeurDMR.bits(this.lire(n, -12, -1, niveaux))[4] + 1;   // CACH : bit TC
    if (voix) {
      this.voix(slot, n);
      for (let q = 1; q < 6; q++) this.prevues.push([n + q * SLOT, slot, q, niveaux]);
      return;
    }
    const sym = this.lire(n, 0, 131, niveaux), bits = DecodeurDMR.bits(sym);
    const st = plusProche(GOLAY_MOTS, parseInt([...bits.slice(98, 108), ...bits.slice(156, 166)].join(""), 2), 3);
    const info = [...bits.slice(0, 98), ...bits.slice(166, 264)];
    const souples = [...sym.slice(0, 49), ...sym.slice(83, 132)];
    const types = st !== null ? [st & 15] : [];
    if (st !== null) this.cc = st >> 4;
    for (const dt of [...types, ...[1, 2, 3, 6].filter(t => !types.includes(t))]) {
      if (this.donnee(slot, dt, info, souples, st !== null && dt === types[0])) return;
    }
  }

  bptc(info, controle) {
    for (const corr of [true, false]) { const o = bptcDecoder(info, corr); if (controle(o)) return o; }
    return null;
  }

  donnee(slot, dt, info, souples, sur) {
    const etat = this.slots[slot];
    if (dt === 1) {
      const o = this.bptc(info, o => lcValide(o, 0x96)); if (!o) return false;
      const a = lcVersAppel(o); if (a) this.appel(slot, a, "en-tête");
      return true;
    }
    if (dt === 2) {
      const o = this.bptc(info, o => lcValide(o, 0x99)); if (!o) return false;
      const a = lcVersAppel(o);
      if (etat.appel) this.finAppel(slot, etat.appel.dernier);
      else if (a) { this.appel(slot, a, "fin"); this.finAppel(slot, null); }
      return true;
    }
    if (dt === 3) {
      const o = this.bptc(info, o => crc16Valide(o, 0xA5A5)); if (!o) return false;
      this.csbk++;
      const ev = { type: "csbk", slot, cc: this.cc, opcode: o[0] & 0x3F, fid: o[1] };
      if (ev.opcode === 0x3D) Object.assign(ev, { groupe: !!(o[2] & 0x40), dst: entier24(o, 4), src: entier24(o, 7), blocs: o[3] });
      this.ev.push(ev);
      return true;
    }
    if (dt === 6) {
      const o = this.bptc(info, o => crc16Valide(o, 0xCCCC)); if (!o) return false;
      const dpf = o[0] & 15;
      const h = { type: "donnees", slot, groupe: !!(o[0] & 0x80), dpf, sap: o[1] >> 4, dst: entier24(o, 2), src: entier24(o, 5), blocs: 0, bourrage: 0, confirme: false };
      if (dpf === 2 || dpf === 3) Object.assign(h, { blocs: o[8] & 0x7F, bourrage: (o[0] >> 4 & 1) << 4 | o[1] & 15, confirme: dpf === 3 });
      else if (dpf === 13 || dpf === 14) h.blocs = (o[0] >> 4 & 3) << 4 | o[1] & 15;
      this.ev.push({ ...h });
      etat.donnees = h.blocs ? h : null; etat.blocs = [];
      return true;
    }
    if ((dt === 7 || dt === 8) && sur && etat.donnees) {
      etat.blocs.push([dt, info, souples]);
      if (etat.blocs.length >= etat.donnees.blocs) { this.paquet(slot, etat.donnees, etat.blocs); etat.donnees = null; }
      return true;
    }
    if (dt === 0 && sur) { etat.chiffre = true; return true; }
    return sur;
  }

  paquet(slot, h, blocs) {
    let o = null, juste = false;
    for (const code of [CODE_1, CODE_2]) {
      const morceaux = blocs.map(([dt, info, s]) => (dt === 7 ? bptcDecoder(info) : treillis(s, code)));
      if (morceaux.some(x => !x)) continue;
      const parties = morceaux.map(x => (h.confirme ? x.subarray(2) : x));
      const essai = new Uint8Array(parties.reduce((t, x) => t + x.length, 0));
      let p = 0; for (const x of parties) { essai.set(x, p); p += x.length; }
      if (essai.length < 4) continue;
      const n = essai.length - 4, ok = crc32DMR(essai, n) === ((essai[n] << 24 | essai[n + 1] << 16 | essai[n + 2] << 8 | essai[n + 3]) >>> 0);
      if (ok || !o) { o = essai; juste = ok; }
      if (ok || blocs.every(([dt]) => dt === 7)) break;
    }
    if (!o) return;
    const utile = o.subarray(0, Math.max(0, o.length - 4 - (h.bourrage || 0)));
    this.ev.push({ type: "sms", slot, groupe: h.groupe, dst: h.dst, src: h.src, texte: paquetVersTexte(h.sap, utile), crc: juste });
  }

  salveVocale(n, slot, _k, niveaux) {
    const bits = DecodeurDMR.bits(this.lire(n, 54, 77, niveaux));
    const emb = plusProche(QR_MOTS, parseInt([...bits.slice(0, 8), ...bits.slice(40, 48)].join(""), 2), 2);
    if (emb === null) return;
    const etat = this.slots[slot];
    this.voix(slot, n);
    const lcss = emb & 3, frag = bits.slice(8, 40);
    if (lcss === 1) { etat.frag = frag; etat.etape = 1; }
    else if (lcss === 3 && (etat.etape === 1 || etat.etape === 2)) { etat.frag = [...etat.frag, ...frag]; etat.etape++; }
    else if (lcss === 2 && etat.etape === 3) {
      const lc = lcIntegre([...etat.frag, ...frag]); etat.etape = 0;
      if (lc) this.lcIntegre(slot, lc);
    } else etat.etape = 0;
  }

  lcIntegre(slot, o) {
    const flco = o[0] & 0x3F, fid = o[1], a = lcVersAppel(o);
    if (a) { this.appel(slot, a, "en cours"); return; }
    if (flco >= 4 && flco <= 7 && (fid === 0 || fid === 0x68)) {
      const etat = this.slots[slot], bits = [...o].flatMap(x => Array.from({ length: 8 }, (_, i) => x >> (7 - i) & 1));
      if (flco === 4) {
        const format = bits[16] << 1 | bits[17], longueur = parseInt(bits.slice(18, 23).join(""), 2);
        etat.alias = { format, longueur, morceaux: [format === 0 ? bits.slice(23, 72) : bits.slice(24, 72)] };
      } else if (etat.alias) etat.alias.morceaux[flco - 4] = bits.slice(16, 72);
      this.alias(slot);
    }
  }

  alias(slot) {
    const etat = this.slots[slot], al = etat.alias;
    if (!al) return;
    const bits = [];
    for (let k = 0; k < 4 && al.morceaux[k]; k++) bits.push(...al.morceaux[k]);
    const taille = [7, 8, 8, 16][al.format], n = Math.min(al.longueur, Math.floor(bits.length / taille));
    let texte = "";
    for (let i = 0; i < n; i++) { const c = parseInt(bits.slice(i * taille, (i + 1) * taille).join(""), 2); if (c >= 0x20 && c !== 0x7F) texte += String.fromCharCode(c); }
    texte = texte.trim();
    if (texte && etat.appel && etat.appel.alias !== texte) {
      etat.appel.alias = texte;
      this.ev.push({ type: "alias", slot, texte, dst: etat.appel.dst, src: etat.appel.src });
    }
  }

  voix(slot, n) {
    const etat = this.slots[slot];
    if (etat.appel) etat.appel.dernier = n;
    else if (etat.voixDebut === null) etat.voixDebut = n;
  }

  appel(slot, a, origine) {
    const etat = this.slots[slot], c = etat.appel, maintenant = this.t0 + this.buf.length;
    if (c && c.dst === a.dst && c.src === a.src && c.groupe === a.groupe) { c.dernier = maintenant; return; }
    if (c) this.finAppel(slot, c.dernier);
    const debut = origine === "en cours" && etat.voixDebut !== null ? etat.voixDebut : maintenant;
    Object.assign(a, { debut, dernier: maintenant, cc: this.cc, chiffre: a.chiffre || etat.chiffre });
    etat.appel = a; etat.chiffre = false; etat.voixDebut = null;
    this.ev.push({ type: "appel", slot, origine, ...a });
    if (etat.alias) this.alias(slot);                 // alias reçu avant que l'appel soit connu
  }

  finAppel(slot, dernier) {
    const etat = this.slots[slot], a = etat.appel;
    etat.appel = null; etat.etape = 0; etat.alias = null; etat.voixDebut = null;
    if (!a) return;
    const duree = Math.max(0, ((dernier ?? a.dernier) - a.debut) / TAUX_DMR);
    this.ev.push({ type: "fin", slot, duree, dst: a.dst, src: a.src, groupe: a.groupe, alias: a.alias || "" });
  }
}

export const nomSlot = s => (s ? `TS${s}` : "—");
export function texteAppel(a) {
  const m = [`${a.groupe ? "TG" : "privé"} ${a.dst} ← ${a.src}`];
  if (a.alias) m.push(a.alias);
  if (a.chiffre) m.push("chiffré");
  if (a.urgence) m.push("URGENCE");
  if (FID[a.fid]) m.push(FID[a.fid]);
  return m.join(" · ");
}
