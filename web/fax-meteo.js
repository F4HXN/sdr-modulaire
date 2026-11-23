// SPDX-License-Identifier: MIT
// Fax météo (WEFAX) : 120 lignes/min, IOC 576, ±400 Hz autour de la fréquence annoncée. Même algorithme que
// modules-a-telecharger/fax_meteo.py (voir sa documentation) : signal de départ à 300 Hz, phasage (bord
// gauche et pente), lignes, signal d'arrêt à 450 Hz.

export const TAUX_FAX = 12000, ECART_HZ = 400, LPM = 120, LARGEUR = Math.round(Math.PI * 576);
export const LIGNE = TAUX_FAX * 60 / LPM, LIGNES_MAX = 1600, LIGNES_PHASAGE = 50, DECALAGE_USB = 1900;
const DEPART_HZ = 300, ARRET_HZ = 450, TONALITE_DB = 23, N_FFT = 16384;

export const STATIONS = [
  ["DWD Pinneberg (Allemagne)", 3_855_000], ["DWD Pinneberg (Allemagne)", 7_880_000], ["DWD Pinneberg (Allemagne)", 13_882_500],
  ["Northwood (Royaume-Uni)", 2_618_500], ["Northwood (Royaume-Uni)", 4_610_000], ["Northwood (Royaume-Uni)", 8_040_000],
  ["Northwood (Royaume-Uni)", 11_086_500], ["Athènes (Grèce)", 4_481_000], ["Athènes (Grèce)", 8_105_000],
  ["Boston NMF (États-Unis)", 4_235_000], ["Boston NMF (États-Unis)", 6_340_500], ["Boston NMF (États-Unis)", 9_110_000],
  ["Boston NMF (États-Unis)", 12_750_000],
];

function passeBasHamming(coupure, taux, n) {
  const h = new Float64Array(n); let s = 0;
  for (let i = 0; i < n; i++) {
    const k = i - (n - 1) / 2, x = 2 * coupure / taux * k;
    h[i] = (k === 0 ? 1 : Math.sin(Math.PI * x) / (Math.PI * x)) * (0.54 - 0.46 * Math.cos(2 * Math.PI * i / (n - 1)));
    s += h[i];
  }
  return h.map(v => v / s);
}

/** Module du spectre réel (FFT radix 2 de taille N_FFT, entrée complétée de zéros). */
const FFT_COS = new Float64Array(N_FFT / 2), FFT_SIN = new Float64Array(N_FFT / 2), FFT_REV = new Uint32Array(N_FFT);
for (let i = 0; i < N_FFT / 2; i++) { FFT_COS[i] = Math.cos(2 * Math.PI * i / N_FFT); FFT_SIN[i] = -Math.sin(2 * Math.PI * i / N_FFT); }
for (let i = 0, b = Math.log2(N_FFT); i < N_FFT; i++) { let r = 0; for (let k = 0; k < b; k++) r |= ((i >> k) & 1) << (b - 1 - k); FFT_REV[i] = r; }
function moduleSpectre(x) {
  const re = new Float64Array(N_FFT), im = new Float64Array(N_FFT);
  for (let i = 0; i < x.length; i++) re[FFT_REV[i]] = x[i];
  for (let t = 2; t <= N_FFT; t *= 2) {
    const d = t / 2, pas = N_FFT / t;
    for (let i = 0; i < N_FFT; i += t) for (let j = 0, k = 0; j < d; j++, k += pas) {
      const a = i + j, b = a + d, c = FFT_COS[k], s = FFT_SIN[k];
      const tr = re[b] * c - im[b] * s, ti = re[b] * s + im[b] * c;
      re[b] = re[a] - tr; im[b] = im[a] - ti; re[a] += tr; im[a] += ti;
    }
  }
  const m = new Float64Array(N_FFT / 2 + 1);
  for (let i = 0; i <= N_FFT / 2; i++) m[i] = Math.hypot(re[i], im[i]);
  return m;
}
const HANN = Float64Array.from({ length: TAUX_FAX }, (_, i) => 0.5 - 0.5 * Math.cos(2 * Math.PI * i / (TAUX_FAX - 1)));
const mediane = a => { const b = Float64Array.from(a).sort(); const n = b.length; return n % 2 ? b[(n - 1) / 2] : (b[n / 2 - 1] + b[n / 2]) / 2; };

export class DecodeurFax {
  constructor() {
    this.fir = passeBasHamming(1000, TAUX_FAX, 63);
    this.er = new Float64Array(62); this.ei = new Float64Array(62);
    this.pr = null; this.pi = 0; this.inverse = false; this.terminees = []; this.version = 0; this.frequence = null;
    this.reinitialiser();
  }
  reinitialiser() {
    this.valeurs = new Float32Array(1 << 20); this.n = 0;
    this.verifie = 0; this.nDepart = 0; this.nArret = 0; this.enCours = false; this.lignes = [];
    this.debut = 0; this.ligne = LIGNE; this.cale = false; this.attenteFinDepart = false; this.version++; this.tout = true;
  }
  statut() {
    if (this.attenteFinDepart) return "Signal de départ reçu…";
    if (!this.enCours) return "En attente d'une carte (signal de départ à 300 Hz)…";
    const etat = !this.cale && this.lignes.length < LIGNES_PHASAGE ? "phasage" : this.cale ? "calée" : "sans phasage";
    return `Carte en cours · ligne ${this.lignes.length} · ${etat}`;
  }
  demarrerMaintenant() { this.commencer(this.n, false); }
  terminerMaintenant() { if (this.enCours) this.terminer(0, true); }
  reglerBordGauche(fraction) {
    if (!this.enCours) return;
    const l = Math.trunc(this.ligne);
    this.debut += ((Math.round(fraction * this.ligne) % l) + l) % l;
    this.cale = true; this.toutRetracer();
  }
  get pentePpm() { return (this.ligne / LIGNE - 1) * 1e6; }
  reglerPente(ppm) { this.ligne = LIGNE * (1 + ppm * 1e-6); if (this.enCours) this.toutRetracer(); }

  /** Canal complexe à 12 kHz centré sur la fréquence annoncée. */
  traiter(re, im) {
    const n = re.length, h = this.fir, L = h.length;
    const xr = new Float64Array(L - 1 + n), xi = new Float64Array(L - 1 + n);
    xr.set(this.er); xi.set(this.ei);
    for (let i = 0; i < n; i++) { xr[L - 1 + i] = re[i]; xi[L - 1 + i] = im[i]; }
    this.er = xr.slice(n); this.ei = xi.slice(n);
    const v = new Float32Array(n), f = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      let a = 0, b = 0;
      for (let j = 0; j < L; j++) { const c = h[L - 1 - j]; a += xr[i + j] * c; b += xi[i + j] * c; }
      if (this.pr === null) { this.pr = a; this.pi = b; }
      f[i] = Math.atan2(b * this.pr - a * this.pi, a * this.pr + b * this.pi) * TAUX_FAX / (2 * Math.PI);
      this.pr = a; this.pi = b;
      const x = Math.min(1, Math.max(0, (f[i] + ECART_HZ) / (2 * ECART_HZ)));
      v[i] = this.inverse ? 1 - x : x;
    }
    this.frequence = mediane(f);
    this.ajouter(v);
    this.tonalites();
    if (this.enCours) this.decoderLignes(); else this.rogner();
  }
  ajouter(v) {
    if (this.n + v.length > this.valeurs.length) {
      const t = new Float32Array(Math.max(this.valeurs.length * 2, this.n + v.length)); t.set(this.valeurs.subarray(0, this.n)); this.valeurs = t;
    }
    this.valeurs.set(v, this.n); this.n += v.length;
  }
  garderFin(k) { const d = this.n - k; if (d <= 0) return 0; this.valeurs.copyWithin(0, d, this.n); this.n = k; return d; }
  rogner() { const d = this.garderFin(3 * TAUX_FAX); this.verifie = Math.max(0, this.verifie - d); }

  niveauTonalite(fin, hz) {
    const p = this.valeurs.subarray(fin - TAUX_FAX, fin);
    let m = 0; for (const x of p) m += x; m /= p.length;
    const x = new Float64Array(TAUX_FAX); for (let i = 0; i < TAUX_FAX; i++) x[i] = (p[i] - m) * HANN[i];
    const s = moduleSpectre(x), parHz = N_FFT / TAUX_FAX, k = Math.round(hz * parHz);
    let pic = 0; for (let i = k - 4; i <= k + 4; i++) pic = Math.max(pic, s[i]);
    let maxi = 0; for (let i = Math.trunc(20 * parHz); i < Math.trunc(1500 * parHz); i++) maxi = Math.max(maxi, s[i]);
    if (pic < maxi) return 0;              // le fondamental d'une vraie alternance est la raie la plus forte
    const plancher = mediane(s.subarray(Math.trunc(100 * parHz), Math.trunc(1500 * parHz))) + 1e-9;
    return 20 * Math.log10(pic / plancher + 1e-9);
  }
  tonalites() {
    const pas = TAUX_FAX / 4;
    while (this.verifie + pas <= this.n) {
      this.verifie += pas;
      const fin = this.verifie;
      if (fin < TAUX_FAX) continue;
      if (!this.enCours) {
        if (this.niveauTonalite(fin, DEPART_HZ) > TONALITE_DB) { if (++this.nDepart >= 4) this.attenteFinDepart = true; }
        else if (this.attenteFinDepart) { this.attenteFinDepart = false; this.nDepart = 0; this.commencer(fin - pas, true); }
        else this.nDepart = 0;
      } else {
        this.nArret = this.niveauTonalite(fin, ARRET_HZ) > TONALITE_DB ? this.nArret + 1 : 0;
        if (this.nArret >= 6) { this.terminer(Math.round(2.5 * LPM / 60)); return; }
      }
    }
  }

  commencer(debut, phasage) {
    this.enCours = true; this.lignes = []; this.debut = debut; this.ligne = LIGNE;
    this.cale = !phasage; this.nArret = 0; this.version++; this.tout = true;
  }
  rangee(k) {
    const a = this.debut + k * this.ligne, r = new Uint8Array(LARGEUR), pas = this.ligne / LARGEUR;
    for (let i = 0; i < LARGEUR; i++) r[i] = Math.trunc(this.valeurs[Math.min(this.n - 1, Math.max(0, Math.round(a + i * pas)))] * 255);
    return r;
  }
  toutRetracer() {
    const n = this.lignes.length; this.lignes = [];
    for (let k = 0; k < n && this.debut + (k + 1) * this.ligne <= this.n; k++) this.lignes.push(this.rangee(k));
    this.version++; this.tout = true;
  }
  decoderLignes() {
    while (this.debut + (this.lignes.length + 1) * this.ligne <= this.n) {
      this.lignes.push(this.rangee(this.lignes.length)); this.version++;
      if (!this.cale && this.lignes.length === LIGNES_PHASAGE) this.phasage();
      if (this.lignes.length >= LIGNES_MAX) { this.terminer(); return; }
    }
  }
  phasage() {
    const ligne = Math.trunc(LIGNE), large = Math.trunc(ligne / 20);
    const lignes = [];
    for (let k = 4; k < LIGNES_PHASAGE; k++) lignes.push(this.valeurs.slice(this.debut + k * ligne, this.debut + (k + 1) * ligne));
    const profil = new Float64Array(ligne);
    for (const l of lignes) for (let i = 0; i < ligne; i++) profil[i] += l[i] / lignes.length;
    const lisser = p => {          // moyenne glissante de `large` points, en bouclant sur la ligne
      const s = new Float64Array(ligne); let acc = 0;
      for (let i = 0; i < large; i++) acc += p[i];
      for (let i = 0; i < ligne; i++) { s[i] = acc / large; acc += p[(i + large) % ligne] - p[i]; }
      return s;
    };
    const med = mediane(profil), lisse = lisser(profil);
    let mx = -Infinity, mn = Infinity;
    for (const v of lisse) { mx = Math.max(mx, v - med); mn = Math.min(mn, v - med); }
    const signe = mx > -mn ? 1 : -1;
    let centre = 0;
    for (let i = 1; i < ligne; i++) if (signe * (lisse[i] - med) > signe * (lisse[centre] - med)) centre = i;
    if (signe * (lisse[centre] - med) < 0.35) { this.cale = true; return; }
    const positions = lignes.map(r => {
      const m = mediane(r), s = lisser(Float64Array.from(r, v => signe * (v - m)));
      let best = null, bv = -Infinity;
      for (let i = 0; i < ligne; i++) {
        const pres = ((i - centre + ligne / 2) % ligne + ligne) % ligne - ligne / 2;
        if (Math.abs(pres) < ligne / 10 && s[i] > bv) { bv = s[i]; best = centre + pres; }
      }
      return best;
    });
    const n = positions.length, mk = (n - 1) / 2, mp = positions.reduce((a, b) => a + b, 0) / n;
    let num = 0, den = 0;
    positions.forEach((p, k) => { num += (k - mk) * (p - mp); den += (k - mk) ** 2; });
    let pente = num / den, premiere = mp - pente * mk;
    if (Math.abs(pente) > LIGNE * 0.002) { pente = 0; premiere = mediane(positions); }
    this.ligne = LIGNE + pente;
    this.debut += ((Math.round(premiere - 4 * pente) % ligne) + ligne) % ligne;
    this.cale = true; this.toutRetracer();
  }
  terminer(enTrop = 0, partielle = false) {
    const r = enTrop ? this.lignes.slice(0, this.lignes.length - enTrop) : this.lignes;
    if (r.length > LIGNES_PHASAGE || (partielle && r.length)) this.terminees.push({ lignes: r, partielle });
    this.enCours = false; this.lignes = [];
    this.garderFin(TAUX_FAX); this.verifie = this.n; this.nArret = 0; this.version++; this.tout = true;
  }
}
