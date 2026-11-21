// SPDX-License-Identifier: MIT
// SDR Web : sortie audio (AudioWorklet). Tampon circulaire d'environ 1 s ;
// en cas de manque on joue du silence, en cas de trop-plein on rattrape le retard.

class SortieSDR extends AudioWorkletProcessor {
  constructor() {
    super();
    this.tampons = [new Float32Array(48000), new Float32Array(48000)];
    this.lu = 0; this.ecrit = 0; this.dispo = 0;
    this.port.onmessage = e => this.ajouter(e.data);
  }
  ajouter([gauche, droite = gauche]) {
    const [g, d] = this.tampons, n = g.length;
    for (let k = 0; k < gauche.length; k++) {
      g[this.ecrit] = gauche[k];
      d[this.ecrit] = droite[k];
      this.ecrit = (this.ecrit + 1) % n;
    }
    this.dispo += gauche.length;
    if (this.dispo > n * 0.5) {            // plus d'une demi-seconde de retard : on le rattrape
      const saut = this.dispo - Math.floor(n * 0.15);
      this.lu = (this.lu + saut) % n; this.dispo -= saut;
    }
  }
  process(_entrees, sorties) {
    const [gauche, droite] = sorties[0], [g, d] = this.tampons;
    for (let k = 0; k < gauche.length; k++) {
      if (this.dispo) {
        gauche[k] = droite ? g[this.lu] : (g[this.lu] + d[this.lu]) * 0.5;
        if (droite) droite[k] = d[this.lu];
        this.lu = (this.lu + 1) % g.length;
        this.dispo--;
      } else {
        gauche[k] = 0;
        if (droite) droite[k] = 0;
      }
    }
    return true;
  }
}
registerProcessor("sortie-sdr", SortieSDR);
