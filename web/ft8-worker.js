// SPDX-License-Identifier: MIT
// Décodeur ft8_lib compilé en WebAssembly. Ce fil traite un créneau UTC à la fois.
importScripts("./ft8-lib/ft8.js");
const moteur = createFT8Module({ locateFile: nom => new URL(`./ft8-lib/${nom}`, self.location).href });

moteur.then(mod => {
  mod._ft8_init();
  postMessage({ pret: true });
}).catch(e => postMessage({ erreur: String(e) }));

self.onmessage = async ({ data: { audio, taux, mode, sequence, utc } }) => {
  try {
    const mod = await moteur;
    const adresse = mod._malloc(audio.byteLength);
    if (!adresse) throw new Error("mémoire indisponible");
    try {
      mod.HEAPF32.set(audio, adresse / Float32Array.BYTES_PER_ELEMENT);
      const resultat = mod._ft8_decode(adresse, audio.length, taux, Number(mode === "FT4"));
      if (!resultat) throw new Error("résultat indisponible");
      postMessage({ sequence, utc, messages: JSON.parse(mod.UTF8ToString(resultat)) });
    } finally {
      mod._free(adresse);
    }
  } catch (e) {
    postMessage({ sequence, erreur: String(e) });
  }
};
