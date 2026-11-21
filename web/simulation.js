// SPDX-License-Identifier: MIT
// Source simulée : quelques émetteurs fictifs autour de la fréquence centrale, pour essayer sans clé.
import { encoderIndicatif, encoderPosition, encoderVitesse, impulsion } from "./adsb.js?v=20261121-adsb";

// Capteur Nexus-TH fictif : ID 42, canal 1, 23,5 °C et 56 %.
const MOTIF_CAPTEUR = [...BigInt("0x2a80ebf38").toString(2).padStart(36, "0")];
const IMPULSIONS_CAPTEUR = [];
for (const bit of [...MOTIF_CAPTEUR, "synchro"]) {
  const silence = bit === "synchro" ? 40 : bit === "1" ? 20 : 10;
  IMPULSIONS_CAPTEUR.push(...Array(5).fill(0.10), ...Array(silence).fill(0));
}

function amplitudeCapteur(t) {
  const instant = Math.floor((t % 3) * 10_000);
  const duree = IMPULSIONS_CAPTEUR.length;
  return instant < duree * 8 ? IMPULSIONS_CAPTEUR[instant % duree] : 0;
}

// Symboles différentiels d’un PS « SDR DEMO » et d’un radiotexte « Stéréo et RDS ».
const RDS_DEMO = "01011000110000010011001111111111111111111110111011111111111111111111100100111110011101100001110001100100" +
  "10100111001111101100110000000000000000000111110000011111111111111111100100111110011100001111110111000101" +
  "01011000110000010011001111111111111111110011010011000000000000000000011011000001111000011110010010100000" +
  "10100111001111101100110000000000000000001010011000100000000000000000011011000001110110011101011000000111" +
  "01011000110000010011001111110000000000000011110110100110001001011000101010001100000011101000110001111011" +
  "01011000110000010011001111110000000000000101000010110000001110110101110111110000111111101110010000001010" +
  "10100111001111101100110000001111111111110001100001100101100000111111001110100001100011100001110011110011" +
  "01011000110000010011001111110000000000001000101010000110001000001001111111101000111111110000000010010111";

function multiplexDemo(t, gauche) {
  const pilote = 2 * Math.PI * 19000 * t;
  const droite = Math.sin(2 * Math.PI * 1800 * t);
  const bit = 1187.5 * t;
  const signe = RDS_DEMO[Math.floor(bit) % RDS_DEMO.length] === "1" ? 1 : -1;
  const biphase = bit % 1 < 0.5 ? signe : -signe;
  return 0.18 * (gauche + droite) + 0.18 * (gauche - droite) * Math.sin(2 * pilote) +
    0.085 * Math.sin(pilote) + 0.035 * biphase * Math.cos(3 * pilote);
}

// Avions fictifs autour du centre de la France, émettant en ADS-B sur 1090 MHz.
const AVIONS_DEMO = [
  { icao: 0x3c6586, indicatif: "AFR1234", lat: 46.9, lon: 1.6, alt: 36000, kt: 460, cap: 135, vz: 0 },
  { icao: 0x4ca7b5, indicatif: "EZY48TP", lat: 45.8, lon: 3.6, alt: 24000, kt: 390, cap: 340, vz: 1500 },
  { icao: 0x39856a, indicatif: "TVF82LM", lat: 47.6, lon: 2.9, alt: 11000, kt: 280, cap: 210, vz: -1800 },
  { icao: 0x40762f, indicatif: "BAW306", lat: 46.2, lon: 0.9, alt: 38000, kt: 480, cap: 20, vz: 0 },
  { icao: 0x3944ef, indicatif: "FGKXS", lat: 46.6, lon: 2.7, alt: 3500, kt: 110, cap: 270, vz: 0 },
];
const CRENEAU = 0.04;   // un message toutes les 40 ms, à tour de rôle
let adsbCreneau = -1, adsbOctets = null;
function amplitudeAdsb(t) {
  const c = Math.floor(t / CRENEAU), dt = (t - c * CRENEAU) * 1e6;
  if (dt >= 120) return 0;
  if (c !== adsbCreneau) {
    adsbCreneau = c;
    const a = AVIONS_DEMO[c % AVIONS_DEMO.length], genre = Math.floor(c / AVIONS_DEMO.length) % 4;
    const ts = c * CRENEAU % 1800, km = a.kt * 1.852 / 3600 * ts, r = a.cap * Math.PI / 180;
    const lat = a.lat + km * Math.cos(r) / 111.32, lon = a.lon + km * Math.sin(r) / (111.32 * Math.cos(a.lat * Math.PI / 180));
    const alt = Math.max(500, Math.min(41000, a.alt + a.vz * ts / 60));
    adsbOctets = genre === 3 ? encoderIndicatif(a.icao, a.indicatif) : genre === 1 ? encoderVitesse(a.icao, a.kt, a.cap, a.vz)
      : encoderPosition(a.icao, lat, lon, Math.round(alt / 25) * 25, genre === 2);
  }
  return impulsion(adsbOctets, dt);
}

const EMETTEURS = [
  // fréquence (Hz), mode, niveau (amplitude), tonalité (Hz)
  { f: 433_920_000, mode: "OOK" },
  { f: 1_090_000_000, mode: "ADSB", a: 0.25 },
  { f: 144_800_000, mode: "NFM", a: 0.05, ton: 1200 },
  { f: 145_500_000, mode: "NFM", a: 0.20, ton: 1000 },
  { f: 145_237_500, mode: "AM", a: 0.06, ton: 600 },
  { f: 145_712_500, mode: "USB", a: 0.04, ton: 1500 },
  { f: 145_050_000, mode: "CW", a: 0.03, ton: 0 },
  { f: 96_400_000, mode: "WFM", a: 0.30, ton: 1000, stereo: true },
  { f: 98_200_000, mode: "WFM", a: 0.15, ton: 3000 },
];

export class SourceSimulation {
  constructor() { this.nom = "Simulation"; this.t = 0; this.phases = EMETTEURS.map(() => 0); }
  async ouvrir(taux, centre) { this.taux = taux; this.centre = centre; }
  async fermer() {}
  regler(centre) { this.centre = centre; }
  gains() { return []; }
  infos() { return { modele: "Source simulée", tuner: "aucun", couverture: "tout" }; }

  /** Renvoie n échantillons sous forme d'octets bruts, comme une vraie clé. */
  lire(n) {
    const fs = this.taux, re = new Float32Array(n), im = new Float32Array(n);
    for (let k = 0; k < n; k++) { re[k] = (Math.random() - 0.5) * 0.02; im[k] = (Math.random() - 0.5) * 0.02; }
    EMETTEURS.forEach((e, i) => {
      const df = e.f - this.centre;
      if (Math.abs(df) > fs / 2) return;
      let ph = this.phases[i];
      for (let k = 0; k < n; k++) {
        const t = this.t + k / fs;
        const m = Math.sin(2 * Math.PI * e.ton * t);
        let amp = e.a, inst = df;
        if (e.mode === "OOK") amp = amplitudeCapteur(t);
        else if (e.mode === "ADSB") amp = e.a * amplitudeAdsb(t);
        else if (e.mode === "NFM") inst = df + 2500 * m;
        else if (e.mode === "WFM") inst = df + (e.stereo ? 75000 * multiplexDemo(t, m) : 50000 * m);
        else if (e.mode === "AM") amp = e.a * (1 + 0.7 * m);
        else if (e.mode === "USB") inst = df + e.ton;
        else if (e.mode === "CW") amp = Math.sin(2 * Math.PI * 0.8 * t) > 0 ? e.a : 0;
        ph += 2 * Math.PI * inst / fs;
        re[k] += amp * Math.cos(ph); im[k] += amp * Math.sin(ph);
      }
      this.phases[i] = ph % (2 * Math.PI);
    });
    this.t += n / fs;
    const o = new Uint8Array(2 * n);
    for (let k = 0; k < n; k++) {
      o[2 * k] = Math.max(0, Math.min(255, Math.round(127.4 + re[k] * 128)));
      o[2 * k + 1] = Math.max(0, Math.min(255, Math.round(127.4 + im[k] * 128)));
    }
    return o;
  }
}
