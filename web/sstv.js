// SPDX-License-Identifier: MIT
// Décodeur SSTV pour SDR Web : même algorithme que le module sstv.py de SDR Modulaire.
//
// L'audio (48 kHz) est ramené autour de 1 900 Hz ; la fréquence instantanée en est tirée
// (1 500 Hz = noir, 2 300 Hz = blanc, 1 200 Hz = synchro) et moyennée à 12 kHz. Le mode est
// reconnu par l'en-tête VIS ; chaque ligne est calée sur son top de synchro (gabarit top +
// palier), et une droite ajustée sur les tops suit l'écart d'horloge de la clé.
// Modes : Martin M1, M2 ; Scottie S1, S2, DX ; Robot 36, 72 ; PD50 à PD290.

export const TAUX_SSTV = 12000;
const CENTRE = 1900;
const ms = x => x * TAUX_SSTV / 1000;

function martin(nom, vis, b) {
  const s = 4.862, p = 0.572, g = s + p;
  return { nom, vis, largeur: 320, hauteur: 256, ligne: g + 3 * (b + p), synchro: s, posSynchro: 0, couleur: "RVB",
    balayages: [["V", g, b, 320], ["B", g + b + p, b, 320], ["R", g + 2 * (b + p), b, 320]], debut: 0, palier: p };
}
function scottie(nom, vis, b) {
  const p = 1.5, s = 9;
  return { nom, vis, largeur: 320, hauteur: 256, ligne: 3 * b + 3 * p + s, synchro: s, posSynchro: p + 2 * b + p, couleur: "RVB",
    balayages: [["V", p, b, 320], ["B", 2 * p + b, b, 320], ["R", 3 * p + 2 * b + s, b, 320]], debut: s, palier: p };
}
function pd(nom, vis, largeur, hauteur, pixel) {
  const b = largeur * pixel, d = 20 + 2.08;
  return { nom, vis, largeur, hauteur, ligne: d + 4 * b, synchro: 20, posSynchro: 0, couleur: "PD",
    balayages: [["Y1", d, b, largeur], ["Cr", d + b, b, largeur], ["Cb", d + 2 * b, b, largeur], ["Y2", d + 3 * b, b, largeur]],
    debut: 0, palier: 2.08 };
}

export const MODES_SSTV = [
  martin("Martin M1", 44, 146.432), martin("Martin M2", 40, 73.216),
  scottie("Scottie S1", 60, 138.24), scottie("Scottie S2", 56, 88.064), scottie("Scottie DX", 76, 345.6),
  { nom: "Robot 36", vis: 8, largeur: 320, hauteur: 240, ligne: 150, synchro: 9, posSynchro: 0, couleur: "R36",
    balayages: [["Y", 12, 88, 320], ["sep", 100, 4.5, 1], ["C", 106, 44, 160]], debut: 0, palier: 3 },
  { nom: "Robot 72", vis: 12, largeur: 320, hauteur: 240, ligne: 300, synchro: 9, posSynchro: 0, couleur: "R72",
    balayages: [["Y", 12, 138, 320], ["Cr", 156, 69, 160], ["Cb", 231, 69, 160]], debut: 0, palier: 3 },
  pd("PD50", 93, 320, 256, 0.286), pd("PD90", 99, 320, 256, 0.532), pd("PD120", 95, 640, 496, 0.19),
  pd("PD160", 98, 512, 400, 0.382), pd("PD180", 96, 640, 496, 0.286), pd("PD240", 97, 640, 496, 0.382),
  pd("PD290", 94, 800, 616, 0.286),
];
for (const m of MODES_SSTV) m.lignes = m.couleur === "PD" ? m.hauteur / 2 : m.hauteur;
const PAR_VIS = new Map(MODES_SSTV.map(m => [m.vis, m]));

/** Filtre passe-bas à fenêtre de Hamming, gain 1 en continu (comme scipy.signal.firwin). */
function passeBasFir(n, coupure, taux) {
  const h = new Float64Array(n), fc = coupure / taux, m = (n - 1) / 2;
  let s = 0;
  for (let i = 0; i < n; i++) {
    const x = i - m;
    h[i] = (x === 0 ? 2 * fc : Math.sin(2 * Math.PI * fc * x) / (Math.PI * x)) * (0.54 - 0.46 * Math.cos(2 * Math.PI * i / (n - 1)));
    s += h[i];
  }
  for (let i = 0; i < n; i++) h[i] /= s;
  return h;
}

/** YCbCr (0–255) → RVB dans une ligne RGBA. */
function versRvb(img, decalage, y, cb, cr, largeur) {
  for (let i = 0; i < largeur; i++) {
    const Y = y[i], U = cb[i] - 128, V = cr[i] - 128, o = decalage + 4 * i;
    img[o] = Y + 1.402 * V; img[o + 1] = Y - 0.344136 * U - 0.714136 * V; img[o + 2] = Y + 1.772 * U; img[o + 3] = 255;
  }
}

function etirer(c, n) {
  const r = new Float64Array(n), k = (c.length - 1) / (n - 1);
  for (let i = 0; i < n; i++) { const x = i * k, j = Math.min(Math.floor(x), c.length - 2), t = x - j; r[i] = c[j] * (1 - t) + c[j + 1] * t; }
  return r;
}

const mediane = v => { const s = [...v].sort((a, b) => a - b), n = s.length; return n % 2 ? s[n >> 1] : (s[n / 2 - 1] + s[n / 2]) / 2; };

function droite(l, x) {      // moindres carrés : x = o + p·l
  const n = l.length; let sl = 0, sx = 0, sll = 0, slx = 0;
  for (let i = 0; i < n; i++) { sl += l[i]; sx += x[i]; sll += l[i] * l[i]; slx += l[i] * x[i]; }
  const p = (n * slx - sl * sx) / (n * sll - sl * sl);
  return [p, (sx - p * sl) / n];
}

export class DecodeurSSTV {
  constructor(tauxAudio = 48000) {
    this.tauxAudio = tauxAudio;
    this.facteur = Math.max(1, Math.round(tauxAudio / TAUX_SSTV));
    this.h = passeBasFir(63, 1100, tauxAudio);
    this.histRe = new Float64Array(this.h.length); this.histIm = new Float64Array(this.h.length); this.pos = 0;
    this.phase = 0; this.dernierRe = 1; this.dernierIm = 0;
    this.reste = [0, 0];                                  // somme et compte de la moyenne par 4 en cours
    this.donnees = new Float32Array(1 << 16); this.n = 0; this.base = 0;
    this.image = null; this.mode = null; this.etat = "veille";
    this.message = "En attente d'un signal SSTV (en-tête VIS)…";
    this.version = 0; this.terminees = []; this.tonalite = 0; this.visDepuis = 0;
  }

  // ------------------------------------------------------------ tampon (indices absolus)
  get fin() { return this.base + this.n; }
  ajouter(x) {
    if (this.n + x.length > this.donnees.length) {
      const plus = new Float32Array(Math.max(this.donnees.length * 2, this.n + x.length));
      plus.set(this.donnees.subarray(0, this.n)); this.donnees = plus;
    }
    this.donnees.set(x, this.n); this.n += x.length;
  }
  morceau(a, b) { return this.donnees.subarray(a - this.base, b - this.base); }
  garderDepuis(a) {
    const k = Math.max(a, this.base) - this.base;
    if (k > 0) { this.donnees.copyWithin(0, k, this.n); this.n -= k; this.base += k; }
  }

  // ------------------------------------------------------------ fréquence instantanée
  frequences(audio) {
    const h = this.h, L = h.length, w = 2 * Math.PI * CENTRE / this.tauxAudio, k = this.tauxAudio / (2 * Math.PI);
    const sortie = [];
    let [somme, compte] = this.reste;
    for (let i = 0; i < audio.length; i++) {
      const ph = this.phase + w * i;
      this.histRe[this.pos] = audio[i] * Math.cos(ph); this.histIm[this.pos] = -audio[i] * Math.sin(ph);
      let re = 0, im = 0;
      for (let j = 0, p = this.pos; j < L; j++) { re += h[j] * this.histRe[p]; im += h[j] * this.histIm[p]; p = p === 0 ? L - 1 : p - 1; }
      this.pos = this.pos === L - 1 ? 0 : this.pos + 1;
      // angle(z · conj(z précédent))
      const d = Math.atan2(im * this.dernierRe - re * this.dernierIm, re * this.dernierRe + im * this.dernierIm);
      this.dernierRe = re; this.dernierIm = im;
      somme += Math.min(2700, Math.max(900, CENTRE + d * k));
      if (++compte === this.facteur) { sortie.push(somme / compte); somme = 0; compte = 0; }
    }
    this.phase = (this.phase + w * audio.length) % (2 * Math.PI);
    this.reste = [somme, compte];
    return Float32Array.from(sortie);
  }

  traiter(audio) {
    const f = this.frequences(audio);
    if (!f.length) return;
    this.ajouter(f);
    this.tonalite = mediane(f);
    if (this.etat === "veille") this.chercherVis(); else this.decoderLignes();
  }

  // ------------------------------------------------------------ en-tête VIS
  chercherVis() {
    const premier = Math.max(this.base + Math.trunc(ms(260)), this.visDepuis);
    const dernier = this.fin - Math.trunc(ms(300)) - 1;
    if (dernier - premier < 1) { this.garderDepuis(this.fin - Math.trunc(ms(1500))); return; }
    const a = premier - Math.trunc(ms(260)), seg = this.morceau(a, this.fin);
    const c = new Float64Array(seg.length + 1);
    for (let i = 0; i < seg.length; i++) c[i + 1] = c[i] + seg[i];
    const moy = (s, d, f) => { const i = s + Math.trunc(ms(d)), j = s + Math.trunc(ms(f)); return (c[j] - c[i]) / (j - i); };
    const n = dernier - premier, valide = new Uint8Array(n), ecart = new Float64Array(n).fill(Infinity), bitsDe = [];
    for (let q = 0; q < n; q++) {
      const s = premier + q - a;
      const meneur = moy(s, -250, -10), depart = moy(s, 3, 27), arret = moy(s, 273, 297);
      if (Math.abs(meneur - 1900) >= 100 || Math.abs(depart - 1200) >= 100 || Math.abs(arret - 1200) >= 100) continue;
      const bits = []; let e = Math.abs(depart - 1200) + Math.abs(arret - 1200), bon = true;
      for (let k = 0; k < 8; k++) {
        const b = moy(s, 30 + 30 * k + 4, 60 + 30 * k - 4);
        if (b <= 1000 || b >= 1400) { bon = false; break; }
        bits.push(b); e += Math.min(Math.abs(b - 1100), Math.abs(b - 1300));
      }
      if (!bon) continue;
      valide[q] = 1; ecart[q] = e; bitsDe[q] = bits;
    }
    this.visDepuis = dernier;
    const i0 = valide.indexOf(1);
    if (i0 < 0) { this.garderDepuis(this.fin - Math.trunc(ms(1500))); return; }
    const largeur = Math.trunc(ms(15));
    if (i0 + largeur >= n) { this.visDepuis = premier + i0; return; }   // grappe incomplète
    let meilleur = i0;
    for (let q = i0; q < i0 + largeur; q++) if (ecart[q] < ecart[meilleur]) meilleur = q;
    const b = bitsDe[meilleur].map(x => x < 1200 ? 1 : 0);
    let code = 0; for (let k = 0; k < 7; k++) code |= b[k] << k;
    const finVis = premier + meilleur + Math.round(ms(300));
    this.visDepuis = finVis;
    if (b.reduce((s, x) => s + x, 0) % 2) this.message = `En-tête VIS reçu, mais parité fausse (code ${code}) : ignoré.`;
    else if (!PAR_VIS.has(code)) this.message = `Mode SSTV non pris en charge (code VIS ${code}).`;
    else this.demarrer(PAR_VIS.get(code), finVis);
  }

  // ------------------------------------------------------------ image
  demarrer(mode, debut = null) {
    this.mode = mode; this.manuel = debut === null;
    this.depart = (debut === null ? this.fin : debut) + ms(mode.debut);
    this.points = []; this.ligne = 0; this.manques = 0;
    this.cb = new Float64Array(mode.largeur).fill(128); this.cr = new Float64Array(mode.largeur).fill(128); this.yPrec = null;
    this.image = new Uint8ClampedArray(mode.largeur * mode.hauteur * 4);
    for (let i = 3; i < this.image.length; i += 4) this.image[i] = 255;
    this.version++; this.etat = "image"; this.message = `${mode.nom} : réception…`;
  }

  arreter(raison = "") {
    if (this.etat === "image" && this.mode && this.ligne > this.mode.lignes / 3) this.terminer(true);
    this.etat = "veille"; this.visDepuis = this.fin;
    this.message = raison || "En attente d'un signal SSTV (en-tête VIS)…";
  }

  terminer(partielle = false) {
    if (this.image) this.terminees.push({ image: this.image.slice(), largeur: this.mode.largeur, hauteur: this.mode.hauteur,
      mode: this.mode.nom, date: new Date(), partielle });
  }

  prevision(ligne) {
    const m = this.mode, pente = ms(m.ligne), P = this.points;
    if (!P.length) return this.depart + ms(m.posSynchro) + ligne * pente;
    const l = P.map(p => p[0]), x = P.map(p => p[1]);
    if (l.length >= 6 && l[l.length - 1] > l[0]) {
      let [p, o] = droite(l, x);
      if (Math.abs(p / pente - 1) < 0.003) {
        const r = x.map((v, i) => Math.abs(v - (o + p * l[i]))), lim = Math.max(mediane(r) * 3, ms(0.5));
        const lb = l.filter((_, i) => r[i] < lim), xb = x.filter((_, i) => r[i] < lim);
        if (lb.length >= 4) {
          [p, o] = droite(lb, xb);
          if (Math.abs(p / pente - 1) < 0.003) return o + p * ligne;
        }
      }
    }
    return mediane(x.map((v, i) => v - pente * l[i])) + ligne * pente;
  }

  decoderLignes() {
    const m = this.mode, dl = ms(m.ligne), ds = ms(m.synchro);
    while (this.etat === "image") {
      if (this.ligne >= m.lignes) { this.terminer(); this.etat = "fini"; this.arreter(`Image ${m.nom} reçue.`); return; }
      const large = this.manuel && !this.points.length;
      const demi = large ? dl / 2 : Math.max(ms(3), ds * 0.6);
      const prevu = this.prevision(this.ligne) + (large ? dl / 2 : 0);
      const debutLigne = prevu - ms(m.posSynchro);
      if (this.fin < Math.max(prevu + demi + ds, debutLigne + dl + demi) + ms(2)) return;
      // Top cherché par gabarit : 1 200 Hz pendant le top, puis 1 500 Hz pendant le palier.
      const a = Math.trunc(prevu - demi), n = Math.round(ds), p = Math.max(3, Math.round(ms(m.palier)));
      const seg = this.morceau(a, Math.trunc(prevu + demi) + n + p + 1);
      const c1 = new Float64Array(seg.length + 1), c2 = new Float64Array(seg.length + 1);
      for (let i = 0; i < seg.length; i++) { c1[i + 1] = c1[i] + Math.abs(seg[i] - 1200); c2[i + 1] = c2[i] + Math.abs(seg[i] - 1500); }
      let k = 0, mini = Infinity;
      for (let d = 0; d + n + p <= seg.length; d++) {
        const e = (c1[d + n] - c1[d]) / n + (c2[d + n + p] - c2[d + n]) / p;
        if (e < mini) { mini = e; k = d; }
      }
      if (mini < 220) { this.points.push([this.ligne, a + k]); if (this.points.length > 400) this.points.shift(); this.manques = 0; }
      else this.manques++;
      if (this.manques * m.ligne > 10000) { this.arreter(`${m.nom} : signal perdu à la ligne ${this.ligne}.`); return; }
      this.decoderLigne(this.ligne, this.prevision(this.ligne) - ms(m.posSynchro));
      this.ligne++;
      this.garderDepuis(Math.trunc(this.prevision(this.ligne) - ms(m.posSynchro) - dl));
      this.message = `${m.nom} : ligne ${this.ligne}/${m.lignes} (${Math.floor(100 * this.ligne / m.lignes)} %)`;
    }
  }

  /** Moyennes de la fréquence entre bords fractionnaires (aires exactes par interpolation du cumul). */
  moyennes(debut, duree, px) {
    const a = Math.floor(debut), seg = this.morceau(a, Math.ceil(debut + duree) + 2);
    const c = new Float64Array(seg.length + 1);
    for (let i = 0; i < seg.length; i++) c[i + 1] = c[i] + seg[i];
    const cumul = x => { const j = Math.min(Math.max(Math.floor(x), 0), c.length - 2), t = x - j; return c[j] * (1 - t) + c[j + 1] * t; };
    const r = new Float64Array(px), pas = duree / px;
    let prec = cumul(debut - a);
    for (let i = 0; i < px; i++) { const suiv = cumul(debut - a + (i + 1) * pas); r[i] = (suiv - prec) / pas; prec = suiv; }
    return r;
  }

  decoderLigne(ligne, debut) {
    const m = this.mode, L = m.largeur, img = this.image, ch = {};
    for (const [nom, d, duree, px] of m.balayages) {
      const f = this.moyennes(debut + ms(d), ms(duree), px);
      ch[nom] = nom === "sep" ? f : f.map(v => Math.min(255, Math.max(0, (v - 1500) / 800 * 255)));
    }
    if (m.couleur === "RVB") {
      for (let i = 0, o = ligne * L * 4; i < L; i++, o += 4) { img[o] = ch.R[i]; img[o + 1] = ch.V[i]; img[o + 2] = ch.B[i]; }
    } else if (m.couleur === "PD") {
      versRvb(img, 2 * ligne * L * 4, ch.Y1, ch.Cb, ch.Cr, L);
      versRvb(img, (2 * ligne + 1) * L * 4, ch.Y2, ch.Cb, ch.Cr, L);
    } else if (m.couleur === "R72") {
      versRvb(img, ligne * L * 4, ch.Y, etirer(ch.Cb, L), etirer(ch.Cr, L), L);
    } else {                                   // Robot 36 : Cr et Cb une ligne sur deux
      const s = ch.sep[0], impaire = Math.abs(s - 1900) > 200 ? s > 1900 : ligne % 2 === 1;
      if (impaire) this.cb = etirer(ch.C, L); else this.cr = etirer(ch.C, L);
      versRvb(img, ligne * L * 4, ch.Y, this.cb, this.cr, L);
      if (impaire && this.yPrec && ligne > 0) versRvb(img, (ligne - 1) * L * 4, this.yPrec, this.cb, this.cr, L);
      this.yPrec = ch.Y;
    }
    this.version++;
  }
}
