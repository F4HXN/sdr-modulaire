// SPDX-License-Identifier: MIT
// Source simulée : quelques émetteurs fictifs autour de la fréquence centrale, pour essayer sans clé.

const EMETTEURS = [
  // fréquence (Hz), mode, niveau (amplitude), tonalité (Hz)
  { f: 144_800_000, mode: "NFM", a: 0.05, ton: 1200 },
  { f: 145_500_000, mode: "NFM", a: 0.20, ton: 1000 },
  { f: 145_237_500, mode: "AM", a: 0.06, ton: 600 },
  { f: 145_712_500, mode: "USB", a: 0.04, ton: 1500 },
  { f: 145_050_000, mode: "CW", a: 0.03, ton: 0 },
  { f: 96_400_000, mode: "WFM", a: 0.30, ton: 1000 },
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
        if (e.mode === "NFM") inst = df + 2500 * m;
        else if (e.mode === "WFM") inst = df + 50000 * m;
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
