// SPDX-License-Identifier: MIT
// Fil de calcul du Décodeur CW : le réseau tourne ici pour ne pas ralentir l'affichage.
// Reçoit { fenetre: Float32Array (6 s à 8 kHz), premiere }, renvoie le résultat de decoderFenetre.
import { chargerPoids, decoderFenetre } from "./cw.js?v=20261122-cw";

const pret = fetch(new URL("cw-poids.bin?v=20261122-cw", import.meta.url))
  .then(r => { if (!r.ok) throw new Error(`poids du réseau introuvables (${r.status})`); return r.arrayBuffer(); })
  .then(chargerPoids);

onmessage = async e => {
  try {
    const poids = await pret;
    const t0 = performance.now();
    const r = decoderFenetre(poids, e.data.fenetre, e.data.premiere);
    postMessage({ ...r, duree: performance.now() - t0 });
  } catch (err) {
    postMessage({ erreur: err.message });
  }
};
pret.then(() => postMessage({ pret: true }), err => postMessage({ erreur: err.message }));
