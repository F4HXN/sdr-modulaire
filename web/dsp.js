// SPDX-License-Identifier: MIT
// SDR Modulaire (version web) : traitement du signal.
// Même principe que la version Python : NCO → décimation 1 (~240 kHz) → [WFM]
// ou décimation 2 (48 kHz) → [AM, NFM, USB, LSB, CW] → AGC / squelch → audio.
// Tous les filtres gardent leur état d'un bloc à l'autre (continuité de phase).

export const TAUX_AUDIO = 48000;
export const TAUX_INTER = 240000;
export const TAUX = [960000, 1440000, 1920000, 2400000];   // multiples de 240 kHz
export const MODES = ["WFM", "NFM", "AM", "USB", "LSB", "CW"];
export const LARGEURS = { WFM: 180000, NFM: 12500, AM: 8000, USB: 2700, LSB: 2700, CW: 500 };

// ---------------------------------------------------------------- filtres
function kaiser(n, beta) {
  const i0 = x => { let s = 1, t = 1; for (let k = 1; k < 30; k++) { t *= (x / (2 * k)) ** 2; s += t; } return s; };
  const w = new Float64Array(n), d = i0(beta);
  for (let i = 0; i < n; i++) { const r = 2 * i / (n - 1) - 1; w[i] = i0(beta * Math.sqrt(1 - r * r)) / d; }
  return w;
}

/** Passe-bas à phase linéaire (sinus cardinal fenêtré), gain unitaire en continu. */
export function passeBas(fs, coupure, n) {
  if (n % 2 === 0) n++;
  const h = new Float32Array(n), w = kaiser(n, 7), fc = coupure / fs, m = (n - 1) / 2;
  let s = 0;
  for (let i = 0; i < n; i++) {
    const x = i - m;
    h[i] = (x === 0 ? 2 * fc : Math.sin(2 * Math.PI * fc * x) / (Math.PI * x)) * w[i];
    s += h[i];
  }
  for (let i = 0; i < n; i++) h[i] /= s;
  return h;
}

/** Filtre FIR complexe décimant, à état. Coefficients réels (hr) ou complexes (hr, hi). */
export class FirComplexe {
  constructor(hr, decim = 1, hi = null) {
    this.hr = hr; this.hi = hi; this.d = decim; this.n = hr.length;
    this.xr = new Float32Array(0); this.xi = new Float32Array(0); this.phase = 0;
  }
  traiter(ir, ii) {
    const n = this.n, d = this.d, hr = this.hr, hi = this.hi;
    const ancien = this.xr.length, total = ancien + ir.length;
    const br = new Float32Array(total), bi = new Float32Array(total);
    br.set(this.xr); br.set(ir, ancien); bi.set(this.xi); bi.set(ii, ancien);
    let k = this.phase + n - 1;                       // index du dernier échantillon de la fenêtre
    const nOut = k < total ? Math.floor((total - 1 - k) / d) + 1 : 0;
    const or = new Float32Array(nOut), oi = new Float32Array(nOut);
    for (let o = 0; o < nOut; o++, k += d) {
      let sr = 0, si = 0;
      const base = k - n + 1;
      if (hi) {
        for (let j = 0; j < n; j++) {
          const a = br[base + j], b = bi[base + j], c = hr[n - 1 - j], e = hi[n - 1 - j];
          sr += a * c - b * e; si += a * e + b * c;
        }
      } else {
        for (let j = 0; j < n; j++) { const c = hr[n - 1 - j]; sr += br[base + j] * c; si += bi[base + j] * c; }
      }
      or[o] = sr; oi[o] = si;
    }
    // premier échantillon dont la prochaine sortie aura besoin : on garde tout à partir de lui
    const debut = k - (n - 1);
    if (debut >= total) { this.xr = new Float32Array(0); this.xi = new Float32Array(0); this.phase = debut - total; }
    else { this.xr = br.slice(debut); this.xi = bi.slice(debut); this.phase = 0; }
    return [or, oi];
  }
}

/** Filtre FIR réel non décimant, à état. */
export class FirReel {
  constructor(h) { this.h = h; this.n = h.length; this.x = new Float32Array(h.length - 1); }
  traiter(entree) {
    const n = this.n, h = this.h, b = new Float32Array(this.x.length + entree.length);
    b.set(this.x); b.set(entree, this.x.length);
    const s = new Float32Array(entree.length);
    for (let o = 0; o < entree.length; o++) {
      let acc = 0;
      for (let j = 0; j < n; j++) acc += b[o + j] * h[n - 1 - j];
      s[o] = acc;
    }
    this.x = b.slice(b.length - (n - 1));
    return s;
  }
}

/** Oscillateur numérique : multiplie par exp(-j·2π·f·t), phase continue entre blocs. */
export class NCO {
  constructor(fs) { this.fs = fs; this.freq = 0; this.cr = 1; this.ci = 0; }
  melanger(ir, ii) {
    const n = ir.length, or = new Float32Array(n), oi = new Float32Array(n);
    const w = -2 * Math.PI * this.freq / this.fs, dr = Math.cos(w), di = Math.sin(w);
    let cr = this.cr, ci = this.ci;
    for (let k = 0; k < n; k++) {
      const a = ir[k], b = ii[k];
      or[k] = a * cr - b * ci; oi[k] = a * ci + b * cr;
      const t = cr * dr - ci * di; ci = cr * di + ci * dr; cr = t;
    }
    const m = 1 / Math.hypot(cr, ci); this.cr = cr * m; this.ci = ci * m;   // renormalisation
    return [or, oi];
  }
}

// ---------------------------------------------------------------- FFT (radix 2)
export class FFT {
  constructor(n) {
    this.n = n; this.cos = new Float32Array(n / 2); this.sin = new Float32Array(n / 2);
    for (let i = 0; i < n / 2; i++) { this.cos[i] = Math.cos(2 * Math.PI * i / n); this.sin[i] = -Math.sin(2 * Math.PI * i / n); }
    this.rev = new Uint32Array(n);
    const bits = Math.log2(n);
    for (let i = 0; i < n; i++) { let r = 0; for (let b = 0; b < bits; b++) r |= ((i >> b) & 1) << (bits - 1 - b); this.rev[i] = r; }
    this.fen = new Float32Array(n);
    for (let i = 0; i < n; i++) this.fen[i] = 0.5 - 0.5 * Math.cos(2 * Math.PI * i / (n - 1));
  }
  /** Spectre en dBFS, fréquence nulle au centre. */
  spectre(ir, ii, debut = 0) {
    const n = this.n, re = new Float32Array(n), im = new Float32Array(n);
    for (let i = 0; i < n; i++) { const j = this.rev[i]; re[j] = ir[debut + i] * this.fen[i]; im[j] = ii[debut + i] * this.fen[i]; }
    for (let taille = 2; taille <= n; taille *= 2) {
      const demi = taille / 2, pas = n / taille;
      for (let i = 0; i < n; i += taille) {
        for (let j = 0, k = 0; j < demi; j++, k += pas) {
          const a = i + j, b = a + demi, c = this.cos[k], s = this.sin[k];
          const tr = re[b] * c - im[b] * s, ti = re[b] * s + im[b] * c;
          re[b] = re[a] - tr; im[b] = im[a] - ti; re[a] += tr; im[a] += ti;
        }
      }
    }
    const db = new Float32Array(n), norme = 2 / (n * 0.5);
    for (let i = 0; i < n; i++) {
      const j = (i + n / 2) % n, p = (re[j] * re[j] + im[j] * im[j]) * norme * norme;
      db[i] = 10 * Math.log10(p + 1e-20);
    }
    return db;
  }
}

// ---------------------------------------------------------------- démodulateurs
function puissanceDb(ir, ii) {
  let s = 0;
  for (let k = 0; k < ir.length; k++) s += ir[k] * ir[k] + ii[k] * ii[k];
  return 10 * Math.log10(s / Math.max(1, ir.length) + 1e-20);
}

// ------------------------------------------------------------ mesure de la modulation
// Chaque démodulateur renseigne this.modulation = { type, valeur, nominal, max, unite } :
//   FM  : excursion crête en kHz (avant désaccentuation) ; nominal ±2,5 / ±5 kHz en NFM, ±75 kHz en WFM
//   AM  : taux de modulation crête en % (100 % = nominal, au-delà : surmodulation)
//   BLU, CW : niveau au-dessus du plancher de bruit en dB (pas de porteuse à mesurer)
// Crêtes prises au 99e centile pour ne pas suivre les craquements isolés.
function centiles(x, bas, haut, pas = 1) {
  const t = new Float32Array(Math.ceil(x.length / pas));
  for (let k = 0, j = 0; k < x.length; k += pas, j++) t[j] = x[k];
  t.sort();
  return [t[Math.floor(bas * (t.length - 1))] || 0, t[Math.floor(haut * (t.length - 1))] || 0];
}
/** Excursion crête (Hz) d'une sortie de discriminateur (radians par échantillon). */
function excursion(d, fs, pas) {
  let m = 0; for (let k = 0; k < d.length; k += pas) m += d[k];
  m /= Math.ceil(d.length / pas);                      // désaccord : décalage moyen retiré
  const [a, b] = centiles(d, 0.01, 0.99, pas);
  return Math.max(b - m, m - a) * fs / (2 * Math.PI);
}

// Bruit seul : le discriminateur FM et l'enveloppe AM donnent une fausse « modulation » énorme.
// On estime la présence d'un vrai signal (0 = bruit seul, 1 = signal net) et, sans signal,
// l'aiguille est plafonnée à 40 % de l'échelle (PLAFOND_BRUIT).
const PLAFOND_BRUIT = 0.4;
/** FM : enveloppe constante pour un vrai signal ; pour du bruit (loi de Rayleigh), écart-type/moyenne ≈ 0,52. */
function presenceFM(cr, ci, pas) {
  let s = 0, s2 = 0, n = 0;
  for (let k = 0; k < cr.length; k += pas) { const e = Math.hypot(cr[k], ci[k]); s += e; s2 += e * e; n++; }
  const m = s / Math.max(1, n), cv = Math.sqrt(Math.max(0, s2 / Math.max(1, n) - m * m)) / (m || 1);
  return Math.max(0, Math.min(1, (0.42 - cv) / 0.3));
}
/** AM : raie de porteuse au centre du canal, comparée au plancher (médiane) du canal. */
function presenceAM(fft, cr, ci, largeur) {
  if (cr.length < fft.n) return null;
  const db = fft.spectre(cr, ci, 0), c = fft.n / 2, binHz = TAUX_AUDIO / fft.n;
  const demi = Math.max(12, Math.min(c - 1, Math.round(largeur / 2 / binHz)));
  let porteuse = -Infinity; const reste = [];
  for (let k = c - demi; k <= c + demi; k++) { if (Math.abs(k - c) <= 8) porteuse = Math.max(porteuse, db[k]); else reste.push(db[k]); }
  reste.sort((a, b) => a - b);
  const ecart = porteuse - reste[reste.length >> 1];
  return Math.max(0, Math.min(1, (ecart - 12) / 12));
}
/** Applique la présence (lissée) à la mesure : plafond de 40 % de l'échelle sans signal. */
function plafonner(mesure, q) {
  if (q < 1) mesure.valeur = Math.min(mesure.valeur, mesure.max * (PLAFOND_BRUIT + (1 - PLAFOND_BRUIT) * q));
  mesure.presence = q;
  return mesure;
}
const lisser = (a, b) => a === undefined ? b : 0.7 * a + 0.3 * b;

class Discriminateur {
  constructor() { this.pr = 1; this.pi = 0; }
  traiter(ir, ii) {
    const n = ir.length, s = new Float32Array(n);
    let pr = this.pr, pi = this.pi;
    for (let k = 0; k < n; k++) {
      const a = ir[k], b = ii[k];
      s[k] = Math.atan2(b * pr - a * pi, a * pr + b * pi);   // arg(x[k]·conj(x[k-1]))
      pr = a; pi = b;
    }
    this.pr = pr; this.pi = pi;
    return s;
  }
}

class Desaccentuation {   // passe-bas du premier ordre (50 µs en Europe)
  constructor(fs, tau = 50e-6) { this.a = Math.exp(-1 / (fs * tau)); this.y = 0; }
  traiter(x) { const a = this.a, s = new Float32Array(x.length); let y = this.y; for (let k = 0; k < x.length; k++) { y = a * y + (1 - a) * x[k]; s[k] = y; } this.y = y; return s; }
}

class ReductionContinue {
  constructor() { this.m = 0; }
  traiter(x) { const s = new Float32Array(x.length); let m = this.m; for (let k = 0; k < x.length; k++) { m = 0.999 * m + 0.001 * x[k]; s[k] = x[k] - m; } this.m = m; return s; }
}

/** Démodulateur : entrée complexe au taux de son domaine, sortie audio à 48 kHz. */
export function creerDemod(mode, largeur) {
  if (mode === "WFM") {
    const canal = new FirComplexe(passeBas(TAUX_INTER, Math.min(largeur, 200000) / 2, 63));
    const disc = new Discriminateur(), desac = new Desaccentuation(TAUX_INTER);
    const vers48 = new FirComplexe(passeBas(TAUX_INTER, 15000, 101), 5);
    return {
      domaine: "intermediaire", decalage: 0,
      traiter(ir, ii, surMultiplex) {
        const [cr, ci] = canal.traiter(ir, ii);
        this.niveau = puissanceDb(cr, ci);
        const multiplex = disc.traiter(cr, ci);
        this.presence = lisser(this.presence, presenceFM(cr, ci, 4));
        this.modulation = plafonner({ type: "FM", valeur: excursion(multiplex, TAUX_INTER, 4) / 1000, nominal: 75, max: 100, unite: "kHz" }, this.presence);
        this.stereo = surMultiplex?.(multiplex);
        const fm = desac.traiter(multiplex);
        const [a] = vers48.traiter(fm, new Float32Array(fm.length));
        for (let k = 0; k < a.length; k++) a[k] *= 0.6;
        return a;
      },
    };
  }
  const canal = new FirComplexe(passeBas(TAUX_AUDIO, Math.min(largeur, 40000) / 2, 129));
  if (mode === "NFM") {
    const disc = new Discriminateur(), desac = new Desaccentuation(TAUX_AUDIO, 75e-6), dc = new ReductionContinue();
    return {
      domaine: "audio", decalage: 0,
      traiter(ir, ii) {
        const [cr, ci] = canal.traiter(ir, ii); this.niveau = puissanceDb(cr, ci);
        const d = disc.traiter(cr, ci), nominal = largeur >= 20000 ? 5 : 2.5;
        this.presence = lisser(this.presence, presenceFM(cr, ci, 1));
        this.modulation = plafonner({ type: "FM", valeur: excursion(d, TAUX_AUDIO, 1) / 1000, nominal, max: nominal * 1.6, unite: "kHz" }, this.presence);
        const a = dc.traiter(desac.traiter(d));
        const g = TAUX_AUDIO / (2 * Math.PI * 2500);   // ±2,5 kHz → ±1
        for (let k = 0; k < a.length; k++) a[k] *= g;
        return a;
      },
    };
  }
  if (mode === "AM") {
    const dc = new ReductionContinue(), fftAM = new FFT(2048);
    return {
      domaine: "audio", decalage: 0,
      traiter(ir, ii) {
        const [cr, ci] = canal.traiter(ir, ii); this.niveau = puissanceDb(cr, ci);
        const e = new Float32Array(cr.length);
        let porteuse = 0;
        for (let k = 0; k < cr.length; k++) { e[k] = Math.hypot(cr[k], ci[k]); porteuse += e[k]; }
        porteuse /= Math.max(1, cr.length);
        const [a, b] = centiles(e, 0.01, 0.99);
        const taux = porteuse > 0 ? 100 * Math.max(b - porteuse, porteuse - a) / porteuse : 0;
        const q = presenceAM(fftAM, cr, ci, largeur);
        if (q !== null) this.presence = lisser(this.presence, q);
        this.modulation = plafonner({ type: "AM", valeur: taux, nominal: 100, max: 125, unite: "%" }, this.presence ?? 0);
        return dc.traiter(e);
      },
    };
  }
  // BLU et CW : filtre complexe ne gardant qu'une bande latérale, puis partie réelle.
  const cw = mode === "CW", bfo = cw ? 700 : 0;
  const bas = cw ? 700 - largeur / 2 : (mode === "USB" ? 300 : -largeur);
  const haut = cw ? 700 + largeur / 2 : (mode === "USB" ? largeur : -300);
  const centre = (bas + haut) / 2, demi = (haut - bas) / 2;
  const h = passeBas(TAUX_AUDIO, demi, 255), hr = new Float32Array(h.length), hi = new Float32Array(h.length);
  const m = (h.length - 1) / 2;
  for (let k = 0; k < h.length; k++) { const w = 2 * Math.PI * centre * (k - m) / TAUX_AUDIO; hr[k] = h[k] * Math.cos(w); hi[k] = h[k] * Math.sin(w); }
  const blu = new FirComplexe(hr, 1, hi);
  return {
    domaine: "audio", decalage: -bfo,   // CW : la porteuse est entendue à 700 Hz
    traiter(ir, ii) {
      const [cr, ci] = blu.traiter(ir, ii); this.niveau = puissanceDb(cr, ci);
      // Plancher de bruit : suit aussitôt les baisses, remonte de 1 dB/s environ.
      this.plancher = this.plancher === undefined ? this.niveau : Math.min(this.niveau, this.plancher + 0.05);
      this.modulation = { type: cw ? "CW" : "BLU", valeur: Math.max(0, this.niveau - this.plancher), nominal: null, max: 40, unite: "dB" };
      return cr;
    },
  };
}

/** AGC audio : vise une crête de 0,5 ; attaque rapide, retour lent. */
export class AGC {
  constructor() { this.g = 1; }
  traiter(x, stereo) {
    let crete = 1e-6;
    for (let k = 0; k < x.length; k++) crete = Math.max(crete, Math.abs(x[k]));
    if (stereo) {
      for (const canal of stereo) {
        for (const echantillon of canal) crete = Math.max(crete, Math.abs(echantillon));
      }
    }
    const cible = 0.5 / crete;
    this.g = cible < this.g ? cible : this.g + (cible - this.g) * 0.05;
    this.g = Math.min(this.g, 1000);
    const s = new Float32Array(x.length);
    for (let k = 0; k < x.length; k++) s[k] = x[k] * this.g;
    return s;
  }
}

/** Chaîne complète pour un taux d'échantillonnage donné. */
export class Chaine {
  constructor(taux) {
    this.taux = taux;
    this.nco = new NCO(taux);
    const d1 = taux / TAUX_INTER;
    this.dec1 = new FirComplexe(passeBas(taux, 110000, d1 * 12 + 1), d1);
    this.dec2 = new FirComplexe(passeBas(TAUX_INTER, 20000, 61), TAUX_INTER / TAUX_AUDIO);
    this.dc = [0, 0];
    this.agc = new AGC();
    this.agcActif = true;
    this.squelch = -120;
    this.regler("NFM", LARGEURS.NFM);
  }
  regler(mode, largeur) { this.mode = mode; this.largeur = largeur; this.demod = creerDemod(mode, largeur); }
  /** ir, ii : IQ complexes au taux de la clé (composante continue déjà retirée). */
  traiter(ir, ii, decalage, surMultiplex) {
    const d = this.demod;
    this.nco.freq = decalage + d.decalage;
    let [r, i] = this.nco.melanger(ir, ii);
    [r, i] = this.dec1.traiter(r, i);
    if (d.domaine !== "intermediaire") [r, i] = this.dec2.traiter(r, i);
    let audio = d.traiter(r, i, surMultiplex);
    this.niveau = d.niveau;
    this.stereo = d.stereo;
    audio = this.agcActif ? this.agc.traiter(audio, this.stereo) : audio;
    const muet = this.niveau < this.squelch;
    this.modulation = d.modulation && { ...d.modulation, valeur: muet ? 0 : d.modulation.valeur };
    const gainStereo = this.agcActif ? this.agc.g : 1;
    for (const canal of this.stereo || []) {
      for (let k = 0; k < canal.length; k++) {
        canal[k] = muet ? 0 : Math.max(-1, Math.min(1, canal[k] * gainStereo));
      }
    }
    for (let k = 0; k < audio.length; k++) audio[k] = muet ? 0 : Math.max(-1, Math.min(1, audio[k]));
    return audio;
  }
}

/** Octets bruts de la clé (I, Q non signés) → deux tableaux flottants, composante continue retirée. */
export function octetsVersIQ(octets, etat) {
  const n = octets.length >> 1, ir = new Float32Array(n), ii = new Float32Array(n);
  let mr = 0, mi = 0;
  for (let k = 0; k < n; k++) { ir[k] = (octets[2 * k] - 127.4) / 128; ii[k] = (octets[2 * k + 1] - 127.4) / 128; mr += ir[k]; mi += ii[k]; }
  etat.dr = 0.9 * etat.dr + 0.1 * (mr / n); etat.di = 0.9 * etat.di + 0.1 * (mi / n);
  for (let k = 0; k < n; k++) { ir[k] -= etat.dr; ii[k] -= etat.di; }
  return [ir, ii];
}
