// SPDX-License-Identifier: MIT
// Enregistreur IQ de SDR Web : WAV IQ 8 bits (deux canaux I et Q non signés, comme la clé), nom à la SDR#
// (« …_145500000Hz_IQ.wav ») et bloc « auxi » (HDSDR / SDR#) avec la fréquence centrale et l'heure UTC.
// Même format que modules-a-telecharger/enregistreur_iq.py ; relecture des WAV 8 et 16 bits.

export const TAILLE_ENTETE = 220;
export const LIMITE_WAV = 0xFFFF0000;

function systemtime(v, o, d) {
  for (const x of [d.getUTCFullYear(), d.getUTCMonth() + 1, d.getUTCDay(), d.getUTCDate(), d.getUTCHours(),
    d.getUTCMinutes(), d.getUTCSeconds(), d.getUTCMilliseconds()]) { v.setUint16(o, x, true); o += 2; }
}

/** En-tête WAV IQ 8 bits (fmt + auxi de 168 octets + data), 220 octets. */
export function enteteWav(taux, centre, debut, fin, octets) {
  const b = new ArrayBuffer(TAILLE_ENTETE), v = new DataView(b), t = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  t(0, "RIFF"); v.setUint32(4, TAILLE_ENTETE - 8 + octets, true); t(8, "WAVE");
  t(12, "fmt "); v.setUint32(16, 16, true);
  v.setUint16(20, 1, true); v.setUint16(22, 2, true); v.setUint32(24, taux, true); v.setUint32(28, taux * 2, true);
  v.setUint16(32, 2, true); v.setUint16(34, 8, true);
  t(36, "auxi"); v.setUint32(40, 168, true);
  systemtime(v, 44, debut); systemtime(v, 60, fin);
  v.setUint32(76, Math.round(centre) >>> 0, true); v.setUint32(80, taux, true);
  t(212, "data"); v.setUint32(216, octets, true);
  return new Uint8Array(b);
}

/** IQ flottants (comme octetsVersIQ : (octet − 127,4) / 128) → octets I, Q entrelacés. */
export function iqVersOctets(re, im) {
  const o = new Uint8Array(2 * re.length);
  for (let k = 0; k < re.length; k++) {
    o[2 * k] = Math.max(0, Math.min(255, Math.round(re[k] * 128 + 127.4)));
    o[2 * k + 1] = Math.max(0, Math.min(255, Math.round(im[k] * 128 + 127.4)));
  }
  return o;
}

export const nomFichier = (d, centre) =>
  `SDRWeb_${d.toISOString().slice(0, 19).replace(/-/g, "").replace(/:/g, "").replace("T", "_")}Z_${Math.round(centre)}Hz_IQ.wav`;

/** Décrit un fichier WAV IQ : { bits, taux, centre, debut (octet), n (échantillons), heure } ; lève une Error sinon. */
export async function lireEnteteWav(fichier) {
  const b = await fichier.slice(0, Math.min(fichier.size, 1 << 20)).arrayBuffer(), v = new DataView(b);
  const txt = (o, n = 4) => String.fromCharCode(...new Uint8Array(b, o, n));
  if (b.byteLength < 12 || !["RIFF", "RF64"].includes(txt(0)) || txt(8) !== "WAVE") throw new Error("ce n'est pas un fichier WAV");
  const info = { centre: null, heure: null };
  let o = 12;
  for (;;) {
    if (o + 8 > b.byteLength) throw new Error("bloc « data » introuvable");
    const id = txt(o), taille = v.getUint32(o + 4, true);
    if (id === "data") {
      info.debut = o + 8;
      const reste = fichier.size - info.debut;
      info.octets = taille === 0 || taille === 0xFFFFFFFF || taille > reste ? reste : taille;
      break;
    }
    if (id === "fmt ") {
      let code = v.getUint16(o + 8, true);
      const canaux = v.getUint16(o + 10, true), bits = v.getUint16(o + 22, true);
      if (code === 0xFFFE && taille >= 26) code = v.getUint16(o + 32, true);
      if (canaux !== 2) throw new Error(`${canaux} canal(aux) : un fichier IQ en a deux (I et Q)`);
      if (code !== 1 || ![8, 16].includes(bits)) throw new Error(`WAV de ${bits} bits (code ${code}) non pris en charge ici : 8 ou 16 bits`);
      info.bits = bits; info.taux = v.getUint32(o + 12, true);
    } else if (id === "auxi" && taille >= 36) {
      const c = v.getUint32(o + 8 + 32, true);
      if (c) info.centre = c;
      const a = [0, 1, 3, 4, 5, 6].map(i => v.getUint16(o + 8 + 2 * i, true));
      if (a[0] > 1900) info.heure = `${a[0]}-${String(a[1]).padStart(2, "0")}-${String(a[2]).padStart(2, "0")} à ${a.slice(3).map(x => String(x).padStart(2, "0")).join(":")} UTC`;
    }
    o += 8 + taille + (taille & 1);
  }
  if (!info.bits) throw new Error("bloc « fmt » introuvable");
  info.n = Math.floor(info.octets / (info.bits / 4));
  if (info.centre === null) {
    const m = /_(\d{4,11})Hz/i.exec(fichier.name);
    if (m) info.centre = Number(m[1]);
  }
  if (info.centre === null) throw new Error("fréquence centrale inconnue (ni bloc « auxi », ni « …_145500000Hz… » dans le nom)");
  return info;
}

/**
 * Source « fichier » pour app.js : même interface que la clé (ouvrir, demarrerLecture, fermer, infos), à vitesse
 * réelle, en octets I/Q non signés. centreFixe : la page ne recentre pas, le VFO reste dans la bande.
 */
export class SourceFichierIQ {
  constructor(fichier, info) {
    this.fichier = fichier; this.info = info; this.taux = info.taux; this.centre = info.centre;
    this.nom = `Fichier ${fichier.name}`; this.centreFixe = true; this.boucle = true;
    this.pos = 0; this.fini = false; this.saut = null;
  }
  async ouvrir(taux) {
    if (taux !== this.taux) throw new Error(`l'enregistrement est à ${this.taux / 1e6} MS/s, la réception à ${taux / 1e6} MS/s`);
  }
  gains() { return []; }
  infos() { return { modele: "Fichier IQ", tuner: this.fichier.name }; }
  aller(fraction) { this.saut = Math.floor(Math.max(0, Math.min(1, fraction)) * (this.info.n - 1)); }

  demarrerLecture(taille, rappel) {        // taille : octets par bloc (2 par échantillon)
    const n = taille / 2;
    let t0 = performance.now(), livres = 0, occupe = false;
    this.minuterie = setInterval(async () => {
      if (occupe) return;
      occupe = true;
      try {
        if (this.saut !== null) { this.pos = this.saut; this.saut = null; this.fini = false; }
        let dus = Math.floor((performance.now() - t0) / 1000 * this.taux / n) - livres;
        if (dus > 10) { t0 = performance.now(); livres = 0; dus = 1; }      // retard (onglet en arrière-plan) : on repart
        for (; dus > 0; dus--, livres++) {
          const sortie = new Uint8Array(taille).fill(127);
          let k = 0;
          while (k < n && !this.fini) {
            const m = Math.min(n - k, this.info.n - this.pos), a = this.info.debut + this.pos * this.info.bits / 4;
            const brut = new Uint8Array(await this.fichier.slice(a, a + m * this.info.bits / 4).arrayBuffer());
            if (this.info.bits === 8) sortie.set(brut, 2 * k);
            else { const s = new Int16Array(brut.buffer, 0, 2 * m); for (let i = 0; i < 2 * m; i++) sortie[2 * k + i] = (s[i] >> 8) + 128; }
            k += m; this.pos += m;
            if (this.pos >= this.info.n) { if (this.boucle) this.pos = 0; else this.fini = true; }
          }
          if (!this.minuterie) return;
          rappel(sortie);
        }
      } catch (e) { this.surErreur?.(e); } finally { occupe = false; }
    }, 25);
  }
  async fermer() { clearInterval(this.minuterie); this.minuterie = null; }
}
