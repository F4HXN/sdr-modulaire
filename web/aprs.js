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

// ------------------------------------------------------------------ positions APRS
const NON_COMPRESSE = /^(\d{2})([0-9 ]{2}\.[0-9 ]{2})([NS])(.)(\d{3})([0-9 ]{2}\.[0-9 ]{2})([EW])(.)/;
function nonCompresse(p) {
  const m = NON_COMPRESSE.exec(p); if (!m) return null;
  const lat = +m[1] + parseFloat(m[2].replace(/ /g, "0")) / 60, lon = +m[5] + parseFloat(m[6].replace(/ /g, "0")) / 60;
  return [m[3] === "S" ? -lat : lat, m[7] === "W" ? -lon : lon, m[4] + m[8], p.slice(19)];
}
function compresse(p) {
  if (p.length < 10 || !/^[\/\\A-Za-j]/.test(p)) return null;
  for (let i = 1; i < 9; i++) { const c = p.charCodeAt(i); if (c < 33 || c > 124) return null; }
  const b91 = s => [...s].reduce((v, c, i) => v + (c.charCodeAt(0) - 33) * 91 ** (3 - i), 0);
  return [90 - b91(p.slice(1, 5)) / 380926, -180 + b91(p.slice(5, 9)) / 190463, p[0] + p[9], p.slice(13)];
}
const positionDe = p => /^\d/.test(p) ? nonCompresse(p) : compresse(p);
function micE(dest, info) {
  const d = dest.split("-")[0]; if (d.length !== 6 || info.length < 9) return null;
  const ch = [];
  for (const c of d) {
    if (c >= "0" && c <= "9") ch.push(+c);
    else if (c >= "A" && c <= "J") ch.push(c.charCodeAt(0) - 65);
    else if (c >= "P" && c <= "Y") ch.push(c.charCodeAt(0) - 80);
    else if ("KLZ".includes(c)) ch.push(0);
    else return null;
  }
  const lat = ch[0] * 10 + ch[1] + (ch[2] * 10 + ch[3] + (ch[4] * 10 + ch[5]) / 100) / 60;
  const haut = c => c >= "P" && c <= "Z", nord = haut(d[3]), decale = haut(d[4]), ouest = haut(d[5]);
  let deg = info.charCodeAt(1) - 28 + (decale ? 100 : 0);
  if (deg >= 180 && deg <= 189) deg -= 80; else if (deg >= 190 && deg <= 199) deg -= 190;
  let mn = info.charCodeAt(2) - 28; if (mn >= 60) mn -= 60;
  const lon = deg + (mn + (info.charCodeAt(3) - 28) / 100) / 60;
  // commentaire : sans l'indicateur de type d'appareil Kenwood (« ] » TH-D7, « > » TM-D700)
  return [nord ? lat : -lat, ouest ? -lon : lon, info[8] + info[7], info.slice(9).replace(/^[\]>]/, "")];
}
/** Position d'une trame TNC2 : { id, lat, lon, symbole, commentaire, source }, ou null (comme position_aprs). */
export function positionAPRS(texte) {
  const deux = texte.indexOf(":"), chevron = texte.indexOf(">");
  if (deux < 0 || chevron < 0 || chevron > deux) return null;
  const source = texte.slice(0, chevron), dest = texte.slice(chevron + 1, deux).split(",")[0], info = texte.slice(deux + 1);
  let id = source, r = null; const t = info[0] ?? "";
  if ("!=".includes(t) && t) r = positionDe(info.slice(1));
  else if ("/@".includes(t) && t) r = positionDe(info.slice(8));
  else if (t === ";" && info.length >= 18) { id = info.slice(1, 10).trim(); r = positionDe(info.slice(18)); }
  else if (t === ")") { const m = /^\)([^!_]{3,9})[!_]/.exec(info); if (m) { id = m[1].trim(); r = positionDe(info.slice(m[0].length)); } }
  else if ("`'\x1c\x1d".includes(t) && t) r = micE(dest, info);
  if (!r || !(r[0] >= -90 && r[0] <= 90 && r[1] >= -180 && r[1] <= 180)) return null;
  return { id, lat: r[0], lon: r[1], symbole: r[2], commentaire: r[3].trim(), source };
}
