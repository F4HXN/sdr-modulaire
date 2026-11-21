// SPDX-License-Identifier: MIT
// Capteurs Nexus et Rubicson : OOK, codage de la durée entre impulsions.
export const TAUX_CAPTEURS = 120_000;

function crcRubicson(octets) {
  let crc = 0x6c;
  for (const octet of octets) {
    crc ^= octet;
    for (let i = 0; i < 8; i++) crc = ((crc << 1) ^ (crc & 0x80 ? 0x31 : 0)) & 255;
  }
  return crc === 0;
}

function lireMesures(bits) {
  if (bits.length < 36 || bits.length > 38) return null;
  const trame = bits.slice(0, 36);
  const identifiant = parseInt(trame.slice(0, 8), 2);
  const indicateurs = parseInt(trame.slice(8, 12), 2);
  const canal = (indicateurs & 3) + 1;
  if (trame.slice(24, 28) !== "1111" || canal > 3) return null;

  const temperatureBrute = parseInt(trame.slice(12, 24), 2);
  const dernierOctet = parseInt(trame.slice(28), 2);
  const rubicson = crcRubicson([
    identifiant, parseInt(trame.slice(8, 16), 2), parseInt(trame.slice(16, 24), 2), 0xf0, dernierOctet,
  ]);
  if (!rubicson && (dernierOctet > 100 || bits.length > 37)) return null;
  const humidite = !rubicson && dernierOctet > 0 ? dernierOctet : null;
  return {
    trame,
    identifiant,
    canal,
    modele: rubicson ? "Rubicson" : humidite === null ? "Nexus-T" : "Nexus-TH",
    temperature: (temperatureBrute >= 2048 ? temperatureBrute - 4096 : temperatureBrute) / 10,
    humidite,
    pile: Boolean(indicateurs & 8),
  };
}

export class DecodeurOOK433 {
  constructor(emettre) {
    this.emettre = emettre;
    this.crete = 0;
    this.enveloppe = 0;
    this.haut = false;
    this.duree = 0;
    this.instant = 0;
    this.impulsionValide = false;
    this.bits = "";
    this.derniere = null;
  }

  traiter(re, im) {
    for (let i = 0; i < re.length; i++) {
      const amplitude = Math.hypot(re[i], im[i]);
      this.enveloppe += 0.12 * (amplitude - this.enveloppe);
      this.crete = Math.max(this.enveloppe, this.crete * 0.99975);
      const haut = this.enveloppe > Math.max(0.0015, this.crete * (this.haut ? 0.27 : 0.48));
      this.duree++;
      this.instant++;
      if (haut !== this.haut) {
        if (haut && this.impulsionValide) {
          // Une période basse d'environ 1 ms représente 0, environ 2 ms représente 1.
          if (this.duree >= 66 && this.duree <= 174) this.bits += "0";
          else if (this.duree >= 186 && this.duree <= 312) this.bits += "1";
          else this.bits = "";
          if (this.bits.length > 38) this.bits = "";
        } else if (!haut) {
          this.impulsionValide = this.duree >= 30 && this.duree <= 100;
          if (!this.impulsionValide) this.bits = "";
        }
        this.haut = haut;
        this.duree = 0;
      }
      // Un silence prolonge la dernière impulsion et marque la fin de la trame.
      if (!this.haut && this.duree === 360) {
        if (this.impulsionValide) this.finTrame();
        this.bits = "";
        this.impulsionValide = false;
      }
    }
  }

  finTrame() {
    const mesure = lireMesures(this.bits);
    if (!mesure) return;
    if (this.derniere?.trame !== mesure.trame || this.instant - this.derniere.instant > TAUX_CAPTEURS / 3) {
      this.derniere = { trame: mesure.trame, repetitions: 0 };
    }
    this.derniere.instant = this.instant;
    if (++this.derniere.repetitions === 3) this.emettre(mesure);
  }
}
