// SPDX-License-Identifier: MIT
import { FirComplexe, passeBas, TAUX_INTER } from "./dsp.js?v=20261122-bruit";

const TAU = 2 * Math.PI;
const PILOTE = TAU * 19000 / TAUX_INTER;
const OFFSETS_RDS = [0x0fc, 0x198, 0x168, 0x1b4];

function resteRds(mot) {
  for (let i = 25; i >= 10; i--) {
    if (mot & (1 << i)) mot ^= 0x5b9 << (i - 10);
  }
  return mot;
}

class BlocsRds {
  constructor(emettre) {
    this.emettre = emettre;
    this.mot = 0;
    this.bits = 0;
    this.groupe = [];
  }

  ajouter(bit) {
    this.mot = ((this.mot << 1) | bit) & 0x3ffffff;
    if (++this.bits < 26) return;
    const reste = resteRds(this.mot);
    const position = this.groupe.length;
    if (reste === OFFSETS_RDS[position] || (position === 2 && reste === 0x350)) {
      this.groupe.push(this.mot >>> 10);
      this.bits = 0;
      if (this.groupe.length === 4) {
        this.emettre(this.groupe);
        this.groupe = [];
      }
    } else {
      this.groupe = reste === OFFSETS_RDS[0] ? [this.mot >>> 10] : [];
      if (this.groupe.length) this.bits = 0;
    }
  }
}

// Alphabet G0 du RDS (EN 50067, annexe E), codes 0x80 à 0xFF.
const G0 = "áàéèíìóòúùÑÇŞβ¡Ĳâäêëîïôöûüñçşǧıĳ" +
  "ªα©‰Ǧěňőπ€£$←↑→↓º¹²³±İńűµ¿÷°¼½¾§" +
  "ÁÀÉÈÍÌÓÒÚÙŘČŠŽÐĿÂÄÊËÎÏÔÖÛÜřčšžđŀ" +
  "ÃÅÆŒŷÝÕØÞŊŔĆŚŹŦðãåæœŵýõøþŋŕćśźŧ ";
const G0_SPECIAUX = { 36: "¤", 94: "―", 96: "‖", 126: "¯" };
function texteRds(octets) {
  let texte = "";
  for (const code of octets) {
    if (code === 13) break;
    texte += code >= 128 ? G0[code - 128] :
      G0_SPECIAUX[code] ?? (code >= 32 && code < 127 ? String.fromCharCode(code) : " ");
  }
  return texte.trim();
}

class DonneesRds {
  constructor() { this.pi = null; }

  recevoir([pi, b, c, d]) {
    this.dernierGroupe = performance.now();
    if (pi !== this.pi) {
      this.pi = pi;
      this.segmentsPS = [];
      this.nom = "";
      this.message = "";
      this.versionRT = null;
    }
    const type = b >>> 12;
    if (type === 0) {
      this.segmentsPS[b & 3] = [d >>> 8, d & 255];
      if ([0, 1, 2, 3].every(i => this.segmentsPS[i])) this.nom = texteRds(this.segmentsPS.flat());
    }
    if (type !== 2) return;
    const version = b & 0x0810; // version A/B et indicateur de changement du texte
    if (version !== this.versionRT) {
      this.versionRT = version;
      this.segmentsRT = [];
      this.message = "";
    }
    const court = Boolean(b & 0x0800);
    this.segmentsRT[b & 15] = court ? [d >>> 8, d & 255] : [c >>> 8, c & 255, d >>> 8, d & 255];
    const texte = [];
    for (let i = 0; i < 16 && this.segmentsRT[i]; i++) {
      texte.push(...this.segmentsRT[i]);
      if (texte.includes(13) || i === 15) {
        this.message = texteRds(texte);
        break;
      }
    }
  }
}

class DemodRds {
  constructor(emettre) {
    this.branches = Array.from({ length: 8 }, (_, i) => ({
      phase: i / 8, re: 0, im: 0, ancienRe: 0, ancienIm: 0, blocs: new BlocsRds(emettre),
    }));
    this.re1 = this.re2 = this.re3 = this.im1 = this.im2 = this.im3 = 0;
    this.compteur = 0;
  }

  traiter(re, im) {
    // Trois étages passe-bas isolent la sous-porteuse descendue à 0 Hz.
    this.re1 += 0.065 * (re - this.re1);
    this.re2 += 0.065 * (this.re1 - this.re2);
    this.re3 += 0.065 * (this.re2 - this.re3);
    this.im1 += 0.065 * (im - this.im1);
    this.im2 += 0.065 * (this.im1 - this.im2);
    this.im3 += 0.065 * (this.im2 - this.im3);
    if (++this.compteur !== 10) return;
    this.compteur = 0;
    for (const b of this.branches) {
      const signe = b.phase < 0.5 ? 1 : -1;
      b.re += signe * this.re3;
      b.im += signe * this.im3;
      b.phase += 1187.5 / 24000;
      if (b.phase < 1) continue;
      b.phase -= 1;
      // Décodage différentiel complexe : insensible à la phase absolue à 57 kHz.
      b.blocs.ajouter(Number(b.re * b.ancienRe + b.im * b.ancienIm < 0));
      b.ancienRe = b.re;
      b.ancienIm = b.im;
      b.re = b.im = 0;
    }
  }
}

export class StereoRds {
  constructor() {
    this.phase = 0;
    this.i1 = this.i2 = this.i3 = this.q1 = this.q2 = this.q3 = 0;
    this.correction = 0;
    this.compteur = 0;
    this.melange = 0;
    this.pilote = 0;
    this.deaccentG = this.deaccentD = 0;
    this.filtre = new FirComplexe(passeBas(TAUX_INTER, 15000, 121), 5);
    this.donnees = new DonneesRds();
    this.rds = new DemodRds(groupe => this.donnees.recevoir(groupe));
  }

  traiter(mpx) {
    const difference = new Float32Array(mpx.length);
    for (let i = 0; i < mpx.length; i++) {
      const x = mpx[i], s = Math.sin(this.phase), c = Math.cos(this.phase);
      // Pilote 19 kHz en quadrature, duquel on retrouve la phase du 38 kHz.
      this.i1 += 0.012 * (2 * x * s - this.i1);
      this.i2 += 0.012 * (this.i1 - this.i2);
      this.i3 += 0.012 * (this.i2 - this.i3);
      this.q1 += 0.012 * (2 * x * c - this.q1);
      this.q2 += 0.012 * (this.q1 - this.q2);
      this.q3 += 0.012 * (this.q2 - this.q3);
      if (++this.compteur === 48) {
        this.compteur = 0;
        this.pilote = Math.hypot(this.i3, this.q3);
        this.correction = this.pilote > 0.025 ? Math.atan2(this.q3, this.i3) : 0;
      }
      const phase = this.phase + this.correction;
      const cible = Math.max(0, Math.min(1, (this.pilote - 0.032) / 0.055));
      this.melange += 0.00012 * (cible - this.melange);
      difference[i] = x * 2 * Math.sin(2 * phase) * this.melange;
      this.rds.traiter(x * Math.cos(3 * this.phase), x * Math.sin(3 * this.phase));
      this.phase += PILOTE;
      if (this.phase >= TAU) this.phase -= TAU;
    }

    const [somme, diff] = this.filtre.traiter(mpx, difference);
    const a = Math.exp(-1 / (48000 * 50e-6));
    for (let i = 0; i < somme.length; i++) {
      this.deaccentG = a * this.deaccentG + (1 - a) * (somme[i] + diff[i]);
      this.deaccentD = a * this.deaccentD + (1 - a) * (somme[i] - diff[i]);
      somme[i] = 0.6 * this.deaccentG;
      diff[i] = 0.6 * this.deaccentD;
    }
    return [somme, diff];
  }
}
