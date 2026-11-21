// SPDX-License-Identifier: MIT
// SDR Web : sortie audio (AudioWorklet). Tampon circulaire d'environ 1 s ;
// en cas de manque on joue du silence, en cas de trop-plein on rattrape le retard.

class SortieSDR extends AudioWorkletProcessor {
  constructor() {
    super();
    this.tampon = new Float32Array(48000);
    this.lu = 0; this.ecrit = 0; this.dispo = 0;
    this.port.onmessage = e => this.ajouter(e.data);
  }
  ajouter(bloc) {
    const t = this.tampon, n = t.length;
    for (let k = 0; k < bloc.length; k++) { t[this.ecrit] = bloc[k]; this.ecrit = (this.ecrit + 1) % n; }
    this.dispo += bloc.length;
    if (this.dispo > n * 0.5) {            // plus d'une demi-seconde de retard : on le rattrape
      const saut = this.dispo - Math.floor(n * 0.15);
      this.lu = (this.lu + saut) % n; this.dispo -= saut;
    }
  }
  process(_entrees, sorties) {
    const s = sorties[0][0], t = this.tampon, n = t.length;
    for (let k = 0; k < s.length; k++) {
      if (this.dispo > 0) { s[k] = t[this.lu]; this.lu = (this.lu + 1) % n; this.dispo--; } else s[k] = 0;
    }
    for (let c = 1; c < sorties[0].length; c++) sorties[0][c].set(s);
    return true;
  }
}
registerProcessor("sortie-sdr", SortieSDR);
