// SPDX-License-Identifier: GPL-2.0-or-later
// SDR Modulaire (version web) : pilote WebUSB pour clé RTL2832U, tuners Fitipower FC0013 et
// Rafael R820T / R820T2 / R828D.
//
// Portage en JavaScript de librtlsdr (projet osmocom rtl-sdr) :
//   librtlsdr.c   © 2012-2014 Steve Markgraf, Dimitri Stolnikov, Hoernchen, Kyle Keen
//   tuner_fc0013.c © 2012 Hans-Frieder Vogt, © 2010 Fitipower Integrated Technology Inc,
//                  © 2012 Steve Markgraf
//   tuner_r82xx.c © 2013 Mauro Carvalho Chehab, © 2013 Steve Markgraf, d'après le pilote de
//                  Rafael Microelectronics ; ajouts RTL-SDR Blog V4
// Ce fichier est un logiciel libre : vous pouvez le redistribuer et/ou le modifier selon les termes
// de la licence publique générale GNU (GPL) version 2, ou (à votre choix) toute version ultérieure.

// Identifiants USB des clés RTL2832U courantes (extrait de la liste de librtlsdr)
export const FILTRES_USB = [
  { vendorId: 0x0bda, productId: 0x2832 }, { vendorId: 0x0bda, productId: 0x2838 },
  { vendorId: 0x0413, productId: 0x6680 }, { vendorId: 0x0413, productId: 0x6f0f },
  { vendorId: 0x0458, productId: 0x707f }, { vendorId: 0x0ccd, productId: 0x00a9 },
  { vendorId: 0x0ccd, productId: 0x00b3 }, { vendorId: 0x0ccd, productId: 0x00d3 },
  { vendorId: 0x0ccd, productId: 0x00e0 }, { vendorId: 0x185b, productId: 0x0620 },
  { vendorId: 0x185b, productId: 0x0650 }, { vendorId: 0x1b80, productId: 0xd393 },
  { vendorId: 0x1b80, productId: 0xd394 }, { vendorId: 0x1b80, productId: 0xd395 },
  { vendorId: 0x1b80, productId: 0xd39d }, { vendorId: 0x1d19, productId: 0x1101 },
  { vendorId: 0x1d19, productId: 0x1102 }, { vendorId: 0x1d19, productId: 0x1103 },
  { vendorId: 0x1f4d, productId: 0xb803 }, { vendorId: 0x1f4d, productId: 0xc803 },
];

const XTAL = 28_800_000;
const BLOC = { DEMOD: 0, USB: 1, SYS: 2, IIC: 6 };
const REG = { USB_SYSCTL: 0x2000, USB_EPA_CTL: 0x2148, USB_EPA_MAXPKT: 0x2158, DEMOD_CTL: 0x3000, DEMOD_CTL_1: 0x300b };
const FIR = [-54, -36, -41, -40, -32, -14, 14, 53, 101, 156, 215, 273, 327, 372, 404, 421];

const TUNERS = {   // adresse I2C, registre et valeur d'identification
  E4000: [0xc8, 0x02, 0x40], FC0013: [0xc6, 0x00, 0xa3], R820T: [0x34, 0x00, 0x69],
  R828D: [0x74, 0x00, 0x69], FC2580: [0xac, 0x01, 0x56], FC0012: [0xc6, 0x00, 0xa1],
};
const NOMS_TUNERS = { E4000: "Elonics E4000", FC0013: "Fitipower FC0013", R820T: "Rafael R820T/R820T2",
  R828D: "Rafael R828D", FC2580: "FCI FC2580", FC0012: "Fitipower FC0012" };

// Gains du FC0013 en dixièmes de dB, et la valeur du registre 0x14 correspondante
const GAINS_FC0013 = [[-99, 0x02], [-73, 0x03], [-65, 0x05], [-63, 0x04], [-63, 0x00], [-60, 0x07], [-58, 0x01],
  [-54, 0x06], [58, 0x0f], [61, 0x0e], [63, 0x0d], [65, 0x0c], [67, 0x0b], [68, 0x0a], [70, 0x09], [71, 0x08],
  [179, 0x17], [181, 0x16], [182, 0x15], [184, 0x14], [186, 0x13], [188, 0x12], [191, 0x11], [197, 0x10]];

// ---- Rafael R82xx (tuner_r82xx.c)
const R82XX_IF = 3_570_000, R82XX_PREMIER = 5, R82XX_NB = 30;
const R82XX_INIT = [0x83, 0x32, 0x75, 0xc0, 0x40, 0xd6, 0x6c, 0xf5, 0x63, 0x75, 0x68, 0x6c, 0x83, 0x80, 0x00,
  0x0f, 0x00, 0xc0, 0x30, 0x48, 0xcc, 0x60, 0x00, 0x54, 0xae, 0x4a, 0xc0];
// [début MHz, open_d, rf_mux_ploy, tf_c, xtal_cap0p] (la clé utilise XTAL_HIGH_CAP_0P)
const R82XX_PLAGES = [[0, 0x08, 0x02, 0xdf], [50, 0x08, 0x02, 0xbe], [55, 0x08, 0x02, 0x8b], [60, 0x08, 0x02, 0x7b],
  [65, 0x08, 0x02, 0x69], [70, 0x08, 0x02, 0x58], [75, 0x00, 0x02, 0x44], [80, 0x00, 0x02, 0x44], [90, 0x00, 0x02, 0x34],
  [100, 0x00, 0x02, 0x34], [110, 0x00, 0x02, 0x24], [120, 0x00, 0x02, 0x24], [140, 0x00, 0x02, 0x14], [180, 0x00, 0x02, 0x13],
  [220, 0x00, 0x02, 0x13], [250, 0x00, 0x02, 0x11], [280, 0x00, 0x02, 0x00], [310, 0x00, 0x41, 0x00], [450, 0x00, 0x41, 0x00],
  [588, 0x00, 0x40, 0x00], [650, 0x00, 0x40, 0x00]];
const R82XX_GAINS = [0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229, 254, 280, 297, 328, 338, 364, 372, 386,
  402, 421, 434, 439, 445, 480, 496];   // dixièmes de dB
const R82XX_LNA = [0, 9, 13, 40, 38, 13, 31, 22, 26, 31, 26, 14, 19, 5, 35, 13];
const R82XX_MIX = [0, 5, 10, 10, 19, 9, 10, 25, 17, 10, 8, 16, 13, 6, 3, -8];
const R82XX_PASSE_BAS = [1700000, 1600000, 1550000, 1450000, 1200000, 900000, 700000, 550000, 450000, 350000];
const bitrev = o => { const t = [0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15]; return (t[o & 15] << 4) | t[o >> 4]; };

export class CleRtlSdr {
  constructor(usb) { this.usb = usb; this.nom = "RTL-SDR"; this.ppm = 0; this.freq = 0; this.taux = 0; this.lecture = false; this.direct = 0; }

  /** Demande la clé à l'utilisateur (fenêtre du navigateur), puis l'ouvre. */
  static async choisir() {
    if (!navigator.usb) throw new Error("Ce navigateur ne gère pas WebUSB : utilisez Chrome, Edge ou Opera.");
    const usb = await navigator.usb.requestDevice({ filters: FILTRES_USB });
    return new CleRtlSdr(usb);
  }

  // ------------------------------------------------ accès aux registres
  async _out(valeur, index, donnees) {
    const r = await this.usb.controlTransferOut({ requestType: "vendor", recipient: "device", request: 0, value: valeur, index }, donnees);
    if (r.status !== "ok") throw new Error(`Écriture USB refusée (${valeur.toString(16)}/${index.toString(16)})`);
  }
  async _in(valeur, index, n) {
    const r = await this.usb.controlTransferIn({ requestType: "vendor", recipient: "device", request: 0, value: valeur, index }, n);
    if (r.status !== "ok") throw new Error(`Lecture USB refusée (${valeur.toString(16)}/${index.toString(16)})`);
    return new Uint8Array(r.data.buffer, r.data.byteOffset, r.data.byteLength);
  }
  async ecrireReg(bloc, adr, val, n) {
    const d = n === 1 ? new Uint8Array([val & 0xff]) : new Uint8Array([val >> 8, val & 0xff]);
    await this._out(adr, (bloc << 8) | 0x10, d);
  }
  async lireReg(bloc, adr, n) { const d = await this._in(adr, bloc << 8, n); return n === 1 ? d[0] : d[0] | (d[1] << 8); }
  async ecrireDemod(page, adr, val, n) {
    const d = n === 1 ? new Uint8Array([val & 0xff]) : new Uint8Array([val >> 8, val & 0xff]);
    await this._out((adr << 8) | 0x20, 0x10 | page, d);
    await this._in((0x01 << 8) | 0x20, 0x0a, 1);      // lecture factice, comme librtlsdr
  }
  async repeteurI2C(actif) { await this.ecrireDemod(1, 0x01, actif ? 0x18 : 0x10, 1); }
  async i2cEcrire(adr, octets) { await this._out(adr, (BLOC.IIC << 8) | 0x10, new Uint8Array(octets)); }
  async i2cLire(adr, reg) {
    try { await this.i2cEcrire(adr, [reg]); return (await this._in(adr, BLOC.IIC << 8, 1))[0]; }
    catch { return -1; }
  }
  // tuner
  async tEcrire(reg, val) { await this.i2cEcrire(0xc6, [reg, val & 0xff]); }
  async tLire(reg) { return this.i2cLire(0xc6, reg); }

  // ------------------------------------------------ ouverture
  async ouvrir(taux, centre) {
    const u = this.usb;
    await u.open();
    if (u.configuration === null) await u.selectConfiguration(1);
    try { await u.claimInterface(0); }
    catch (e) {
      throw new Error("Impossible de prendre la main sur la clé : elle est utilisée par un autre programme ou par le pilote TNT "
        + "(Linux : sudo rmmod dvb_usb_rtl28xxu ; Windows : pilote WinUSB avec Zadig). Détail : " + e.message);
    }
    try { await this.ecrireReg(BLOC.USB, REG.USB_SYSCTL, 0x09, 1); } catch { await u.reset(); }
    await this._initBaseband();
    this.tuner = null;
    await this.repeteurI2C(true);
    for (const [nom, [adr, reg, val]] of Object.entries(TUNERS)) {
      if (await this.i2cLire(adr, reg) === val) { this.tuner = nom; break; }
    }
    this.modele = u.productName || "RTL2832U";
    this.fabricant = u.manufacturerName || "";
    this.serie = u.serialNumber || "";
    if (!["FC0013", "R820T", "R828D"].includes(this.tuner)) {
      await this.repeteurI2C(false);
      const nom = this.tuner ? NOMS_TUNERS[this.tuner] : "inconnu";
      await this.fermer();
      throw new Error(`Tuner ${nom} : la version web gère pour l'instant le Fitipower FC0013 et les Rafael R820T, `
        + "R820T2 et R828D. Les autres tuners viendront ensuite ; en attendant, la version Linux les gère tous.");
    }
    if (this.tuner === "FC0013") {
      await this._fc0013Init();
      this.plageTuner = [22e6, 1100e6];
    } else {
      // RTL-SDR Blog V4 : R828D à quartz 28,8 MHz, convertisseur HF intégré
      this.blogV4 = this.fabricant === "RTLSDRBlog" && this.modele === "Blog V4";
      this.xtalTuner = this.tuner === "R828D" && !this.blogV4 ? 16_000_000 : XTAL;
      this.plageTuner = this.blogV4 ? [500e3, 1766e6] : [24e6, 1766e6];
      await this.ecrireDemod(1, 0xb1, 0x1a, 1);   // pas de zéro-IF : le tuner sort sur une FI
      await this.ecrireDemod(0, 0x08, 0x4d, 1);   // entrée ADC « en phase » seulement
      await this._reglerFI(R82XX_IF);
      await this.ecrireDemod(1, 0x15, 0x01, 1);   // inversion du spectre
      await this._r82xxInit();
    }
    await this.repeteurI2C(false);
    await this.reglerTaux(taux);
    await this.reglerGain(null);
    await this.reglerFrequence(centre);
    await this.ecrireReg(BLOC.USB, REG.USB_EPA_CTL, 0x1002, 2);   // vide le tampon
    await this.ecrireReg(BLOC.USB, REG.USB_EPA_CTL, 0x0000, 2);
  }

  async _initBaseband() {
    await this.ecrireReg(BLOC.USB, REG.USB_SYSCTL, 0x09, 1);
    await this.ecrireReg(BLOC.USB, REG.USB_EPA_MAXPKT, 0x0002, 2);
    await this.ecrireReg(BLOC.USB, REG.USB_EPA_CTL, 0x1002, 2);
    await this.ecrireReg(BLOC.SYS, REG.DEMOD_CTL_1, 0x22, 1);
    await this.ecrireReg(BLOC.SYS, REG.DEMOD_CTL, 0xe8, 1);
    await this.ecrireDemod(1, 0x01, 0x14, 1);
    await this.ecrireDemod(1, 0x01, 0x10, 1);
    await this.ecrireDemod(1, 0x15, 0x00, 1);
    await this.ecrireDemod(1, 0x16, 0x0000, 2);
    for (let i = 0; i < 6; i++) await this.ecrireDemod(1, 0x16 + i, 0x00, 1);
    // filtre FIR du démodulateur (8 coefficients sur 8 bits, 8 sur 12 bits)
    const fir = new Uint8Array(20);
    for (let i = 0; i < 8; i++) fir[i] = FIR[i] & 0xff;
    for (let i = 0; i < 8; i += 2) {
      const a = FIR[8 + i], b = FIR[9 + i], j = 8 + (i * 3) / 2;
      fir[j] = (a >> 4) & 0xff; fir[j + 1] = ((a << 4) | ((b >> 8) & 0x0f)) & 0xff; fir[j + 2] = b & 0xff;
    }
    for (let i = 0; i < 20; i++) await this.ecrireDemod(1, 0x1c + i, fir[i], 1);
    await this.ecrireDemod(0, 0x19, 0x05, 1);   // mode SDR, AGC numérique coupée
    await this.ecrireDemod(1, 0x93, 0xf0, 1);
    await this.ecrireDemod(1, 0x94, 0x0f, 1);
    await this.ecrireDemod(1, 0x11, 0x00, 1);
    await this.ecrireDemod(1, 0x04, 0x00, 1);
    await this.ecrireDemod(0, 0x61, 0x60, 1);
    await this.ecrireDemod(0, 0x06, 0x80, 1);
    await this.ecrireDemod(1, 0xb1, 0x1b, 1);   // zéro-IF, suppression du continu, correction IQ
    await this.ecrireDemod(0, 0x0d, 0x83, 1);
  }

  // ------------------------------------------------ réglages
  xtalCorrige() { return Math.floor(XTAL * (1 + this.ppm / 1e6)); }

  async reglerTaux(taux) {
    let ratio = Math.floor((XTAL * 2 ** 22) / taux) & 0x0ffffffc;
    this.taux = taux;
    await this.ecrireDemod(1, 0x9f, (ratio >>> 16) & 0xffff, 2);
    await this.ecrireDemod(1, 0xa1, ratio & 0xffff, 2);
    await this._correctionTaux();
    await this.ecrireDemod(1, 0x01, 0x14, 1);
    await this.ecrireDemod(1, 0x01, 0x10, 1);
    if (this.r82 && !this.direct) {   // R82xx : filtre de FI adapté au taux, puis nouvelle FI et nouvel accord
      await this.repeteurI2C(true);
      try { this.r82.fi = await this._r82xxBande(taux); } finally { await this.repeteurI2C(false); }
      await this._reglerFI(this.r82.fi);
      if (this.freq) await this.reglerFrequence(this.freq);
    }
  }
  async _reglerFI(f) {
    const fi = -Math.floor((f * 2 ** 22) / this.xtalCorrige());
    await this.ecrireDemod(1, 0x19, (fi >> 16) & 0x3f, 1);
    await this.ecrireDemod(1, 0x1a, (fi >> 8) & 0xff, 1);
    await this.ecrireDemod(1, 0x1b, fi & 0xff, 1);
  }
  async _correctionTaux() {
    const offs = Math.trunc(-this.ppm * 2 ** 24 / 1e6);
    await this.ecrireDemod(1, 0x3f, offs & 0xff, 1);
    await this.ecrireDemod(1, 0x3e, (offs >> 8) & 0x3f, 1);
  }
  async reglerPpm(ppm) {
    ppm = Math.round(ppm);
    if (ppm === this.ppm) return;
    this.ppm = ppm;
    await this._correctionTaux();
    if (this.r82 && !this.direct) await this._reglerFI(this.r82.fi);
    if (this.freq) await this.reglerFrequence(this.freq);
  }
  /** Plage utilisable : celle du tuner, ou 0–28,8 MHz en échantillonnage direct. */
  get plage() { return this.direct ? [10e3, 28_800_000] : this.plageTuner; }

  /**
   * Échantillonnage direct (rtlsdr_set_direct_sampling) : 0 = tuner, 1 = branche I, 2 = branche Q
   * (entrée HF de la RTL-SDR Blog V3). Le tuner est mis en veille et le RTL2832U accorde seul la HF.
   */
  async reglerDirect(mode) {
    mode = Number(mode) || 0;
    if (mode === this.direct) return;
    if (mode) {
      if (this.r82) { await this.repeteurI2C(true); try { await this._r82xxVeille(); } finally { await this.repeteurI2C(false); } }
      await this.ecrireDemod(1, 0xb1, 0x1a, 1);             // pas de zéro-IF
      await this.ecrireDemod(1, 0x15, 0x00, 1);             // pas d'inversion du spectre
      await this.ecrireDemod(0, 0x08, 0x4d, 1);             // entrée ADC « en phase » seulement
      await this.ecrireDemod(0, 0x06, mode > 1 ? 0x90 : 0x80, 1);   // échange I/Q : choix de l'entrée
    } else {
      await this.repeteurI2C(true);
      try {
        if (this.r82) { await this._r82xxInit(); this.r82.fi = await this._r82xxBande(this.taux); }
        else await this._fc0013Init();
      } finally { await this.repeteurI2C(false); }
      if (this.r82) {
        await this._reglerFI(this.r82.fi);
        await this.ecrireDemod(1, 0x15, 0x01, 1);
      } else {
        await this._reglerFI(0);
        await this.ecrireDemod(0, 0x08, 0xcd, 1);
        await this.ecrireDemod(1, 0xb1, 0x1b, 1);
      }
      await this.ecrireDemod(0, 0x06, 0x80, 1);
    }
    this.direct = mode;
    if (this.freq) await this.reglerFrequence(this.freq);
  }

  async reglerFrequence(f) {
    f = Math.round(f);
    if (this.direct) { await this._reglerFI(f); this.freq = f; return; }   // le RTL2832U accorde seul
    await this.repeteurI2C(true);
    try { await (this.r82 ? this._r82xxFrequence(f) : this._fc0013Frequence(f)); } finally { await this.repeteurI2C(false); }
    this.freq = f;
  }
  gains() { return this.r82 ? R82XX_GAINS.map(g => g / 10) : [...new Set(GAINS_FC0013.map(g => g[0] / 10))]; }
  /** gain en dB, ou null pour l'AGC du tuner. */
  async reglerGain(gain) {
    if (this.direct) return;                       // tuner en veille : pas de gain
    if (this.r82) {
      await this.repeteurI2C(true);
      try { await this._r82xxGain(gain === null ? null : Math.round(gain * 10)); } finally { await this.repeteurI2C(false); }
      return;
    }
    await this.repeteurI2C(true);
    try {
      let t = await this.tLire(0x0d);
      t = gain === null ? (t & ~0x08) : (t | 0x08);
      await this.tEcrire(0x0d, t);
      await this.tEcrire(0x13, 0x0a);
      if (gain !== null) {
        const g = Math.round(gain * 10);
        let v = GAINS_FC0013[GAINS_FC0013.length - 1][1];
        for (const [gg, r] of GAINS_FC0013) if (gg >= g) { v = r; break; }
        const x = (await this.tLire(0x14)) & 0xe0;
        await this.tEcrire(0x14, x | v);
      }
    } finally { await this.repeteurI2C(false); }
  }
  infos() {
    return { modele: this.modele, fabricant: this.fabricant, serie: this.serie, tuner: NOMS_TUNERS[this.tuner],
             couverture: `${this.plage[0] / 1e6}–${this.plage[1] / 1e6} MHz`, gains: this.gains() };
  }

  // ------------------------------------------------ tuner FC0013
  async _fc0013Init() {
    const reg = [0x00, 0x09, 0x16, 0x00, 0x00, 0x17, 0x02, 0x0a, 0xff, 0x6e, 0xb8, 0x82, 0xfc, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x50, 0x01];
    reg[0x07] |= 0x20;
    reg[0x0c] |= 0x02;
    for (let i = 1; i < reg.length; i++) await this.tEcrire(i, reg[i]);
  }

  async _fc0013Frequence(freq) {
    const xtal2 = Math.floor(this.xtalCorrige() / 2);
    // piste VHF
    let t = (await this.tLire(0x1d)) & 0xe3;
    const piste = freq <= 177500000 ? 0x1c : freq <= 184500000 ? 0x18 : freq <= 191500000 ? 0x14 : freq <= 198500000 ? 0x10
      : freq <= 205500000 ? 0x0c : freq <= 219500000 ? 0x08 : freq < 300000000 ? 0x04 : 0x1c;
    await this.tEcrire(0x1d, t | piste);
    // filtres VHF / UHF
    t = await this.tLire(0x07);
    await this.tEcrire(0x07, freq < 300000000 ? (t | 0x10) : (t & 0xef));
    t = await this.tLire(0x14);
    await this.tEcrire(0x14, freq < 300000000 ? (t & 0x1f) : ((t & 0x1f) | 0x40));
    // diviseur et VCO
    const table = [[37084000, 96, 0x82, 0x00], [55625000, 64, 0x02, 0x02], [74167000, 48, 0x42, 0x00], [111250000, 32, 0x82, 0x02],
      [148334000, 24, 0x22, 0x00], [222500000, 16, 0x42, 0x02], [296667000, 12, 0x12, 0x00], [445000000, 8, 0x22, 0x02],
      [593334000, 6, 0x0a, 0x00], [950000000, 4, 0x12, 0x02], [Infinity, 2, 0x0a, 0x02]];
    const [, multi, r5, r6] = table.find(l => freq < l[0]);
    const reg = [0, 0, 0, 0, 0, r5, r6];
    const fvco = freq * multi;              // < 2^53 : calcul exact en nombres JavaScript
    let vcoSelect = false;
    if (fvco >= 3060000000) { reg[6] |= 0x08; vcoSelect = true; }
    let xdiv = Math.floor(fvco / xtal2);
    if (fvco - xdiv * xtal2 >= Math.floor(xtal2 / 2)) xdiv++;
    let pm = Math.floor(xdiv / 8), am = xdiv - 8 * pm;
    if (am < 2) { am += 8; pm--; }
    if (pm > 31) { reg[1] = am + 8 * (pm - 31); reg[2] = 31; } else { reg[1] = am; reg[2] = pm; }
    if (reg[1] > 15 || reg[2] < 0x0b) throw new Error(`FC0013 : pas de réglage possible pour ${(freq / 1e6).toFixed(3)} MHz`);
    reg[6] |= 0x20;
    let xin = Math.floor((fvco - Math.floor(fvco / xtal2) * xtal2) / 1000) & 0xffff;
    xin = Math.floor((xin * 32768) / Math.floor(xtal2 / 1000)) & 0xffff;
    if (xin >= 16384) xin = (xin + 32768) & 0xffff;
    reg[3] = xin >> 8; reg[4] = xin & 0xff;
    reg[6] = (reg[6] & 0x3f) | 0x80;        // bande de 6 MHz (valeur de librtlsdr)
    reg[5] |= 0x07;
    for (let i = 1; i <= 6; i++) await this.tEcrire(i, reg[i]);
    t = await this.tLire(0x11);
    await this.tEcrire(0x11, multi === 64 ? (t | 0x04) : (t & 0xfb));
    // calibration du VCO
    await this.tEcrire(0x0e, 0x80); await this.tEcrire(0x0e, 0x00); await this.tEcrire(0x0e, 0x00);
    t = (await this.tLire(0x0e)) & 0x3f;
    if ((vcoSelect && t > 0x3c) || (!vcoSelect && t < 0x02)) {
      reg[6] = vcoSelect ? (reg[6] & ~0x08) : (reg[6] | 0x08);
      await this.tEcrire(0x06, reg[6]); await this.tEcrire(0x0e, 0x80); await this.tEcrire(0x0e, 0x00);
    }
  }

  // ------------------------------------------------ tuner Rafael R82xx (R820T, R820T2, R828D)
  get _r82Adr() { return this.tuner === "R828D" ? 0x74 : 0x34; }
  /** Écrit des registres consécutifs (registres « fantômes » gardés en mémoire, 7 octets par message I2C). */
  async _rEcrire(reg, octets) {
    const r = this.r82, d = reg - R82XX_PREMIER;
    if (d >= 0 && d + octets.length <= R82XX_NB && octets.every((v, i) => r.regs[d + i] === v)) return;
    octets.forEach((v, i) => { if (d + i >= 0 && d + i < R82XX_NB) r.regs[d + i] = v & 0xff; });
    for (let pos = 0; pos < octets.length; pos += 7) {
      const morceau = octets.slice(pos, pos + 7);
      await this.i2cEcrire(this._r82Adr, [reg + pos, ...morceau]);
    }
  }
  async _rReg(reg, val) { await this._rEcrire(reg, [val & 0xff]); }
  async _rMasque(reg, val, masque) {
    const actuel = this.r82.regs[reg - R82XX_PREMIER];
    await this._rEcrire(reg, [(actuel & ~masque) | (val & masque)]);
  }
  /** Lit n registres à partir de 0 (le R82xx renvoie les bits inversés). */
  async _rLire(n) {
    await this.i2cEcrire(this._r82Adr, [0x00]);
    const d = await this._in(this._r82Adr, BLOC.IIC << 8, n);
    return Array.from(d, bitrev);
  }

  async _r82xxInit() {
    this.r82 = { regs: new Array(R82XX_NB).fill(0), fi: R82XX_IF, verrou: false, entree: -1, calib: 0 };
    await this._rEcrire(0x05, R82XX_INIT);
    // r82xx_set_tv_standard (6 MHz numérique)
    this.r82.regs.splice(0, R82XX_INIT.length, ...R82XX_INIT);
    await this._rMasque(0x0c, 0x00, 0x0f);
    await this._rMasque(0x13, 49, 0x3f);
    await this._rMasque(0x1d, 0x00, 0x38);
    let standard = true;
    for (let i = 0; i < 2; i++) {             // calibration du filtre de FI
      await this._rMasque(0x0b, 0x6b, 0x60);
      await this._rMasque(0x0f, 0x04, 0x04);
      await this._rMasque(0x10, 0x00, 0x03);
      await this._r82xxPll(56_000_000);
      if (!this.r82.verrou) { standard = false; break; }   // comme librtlsdr : on saute la suite du standard
      await this._rMasque(0x0b, 0x10, 0x10);
      await this._rMasque(0x0b, 0x00, 0x10);
      await this._rMasque(0x0f, 0x00, 0x04);
      const d = await this._rLire(5);
      this.r82.calib = d[4] & 0x0f;
      if (this.r82.calib && this.r82.calib !== 0x0f) break;
    }
    if (standard) {
      if (this.r82.calib === 0x0f) this.r82.calib = 0;
      await this._rMasque(0x0a, 0x10 | this.r82.calib, 0x1f);
      await this._rMasque(0x0b, 0x6b, 0xef);
      await this._rMasque(0x07, 0x00, 0x80);
      await this._rMasque(0x06, 0x10, 0x30);
      await this._rMasque(0x1e, 0x60, 0x60);
      await this._rMasque(0x05, 0x01, 0x80);
      await this._rMasque(0x1f, 0x00, 0x80);
      await this._rMasque(0x0f, 0x00, 0x80);
      await this._rMasque(0x19, 0x60, 0x60);
    }
    // r82xx_sysfreq_sel (DVB-T, tuner numérique)
    await this._rMasque(0x1d, 0xe5, 0xc7);
    await this._rMasque(0x1c, 0x24, 0xf8);
    await this._rReg(0x0d, 0x53);
    await this._rReg(0x0e, 0x75);
    this.r82.entree = 0x00;
    await this._rMasque(0x05, 0x00, 0x60);
    await this._rMasque(0x06, 0x00, 0x08);
    await this._rMasque(0x11, 0x38, 0x38);
    await this._rMasque(0x17, 0x30, 0x30);
    await this._rMasque(0x0a, 0x40, 0x60);
    await this._rMasque(0x1d, 0x00, 0x38);
    await this._rMasque(0x1c, 0x00, 0x04);
    await this._rMasque(0x06, 0x00, 0x40);
    await this._rMasque(0x1a, 0x30, 0x30);
    await this._rMasque(0x1d, 0x18, 0x38);
    await this._rMasque(0x1c, 0x24, 0x04);
    await this._rMasque(0x1e, 14, 0x1f);
    await this._rMasque(0x1a, 0x20, 0x30);
  }

  async _r82xxPll(freq) {
    const r = this.r82, fKhz = Math.floor((freq + 500) / 1000), ref = this.xtalTuner * (1 + this.ppm / 1e6);
    const vcoMin = 1_770_000, vcoMax = 2 * vcoMin;
    await this._rMasque(0x1a, 0x00, 0x0c);
    const regs = r.regs.slice(0x10 - R82XX_PREMIER, 0x10 - R82XX_PREMIER + 7);
    const m8 = (reg, val, masque) => (reg & ~masque) | (val & masque);
    regs[0] = m8(regs[0], 0, 0x10);
    regs[2] = m8(regs[2], 0x80, 0xe0);
    let mixDiv = 2, divNum = 0;
    while (mixDiv <= 64) {
      if (fKhz * mixDiv >= vcoMin && fKhz * mixDiv < vcoMax) { let b = mixDiv; while (b > 2) { b >>= 1; divNum++; } break; }
      mixDiv <<= 1;
    }
    const d = await this._rLire(5);
    const vcoRef = this.tuner === "R828D" ? 1 : 2, fin = (d[4] & 0x30) >> 4;
    if (fin > vcoRef) divNum--; else if (fin < vcoRef) divNum++;
    regs[0] = m8(regs[0], divNum << 5, 0xe0);
    // vco_div = (ref + 65536 · vco) / (2 · ref) ; BigInt pour rester exact
    const vco = BigInt(freq) * BigInt(mixDiv), refB = BigInt(Math.floor(ref));
    const vcoDiv = (refB + 65536n * vco) / (2n * refB);
    const nint = Number(vcoDiv / 65536n), sdm = Number(vcoDiv % 65536n);
    if (nint > 128 / vcoRef - 1) throw new Error(`R82xx : pas de réglage possible pour ${(freq / 1e6).toFixed(3)} MHz`);
    const ni = Math.floor((nint - 13) / 4), si = nint - 4 * ni - 13;
    regs[4] = (ni + (si << 6)) & 0xff;
    regs[2] = m8(regs[2], sdm === 0 ? 0x08 : 0x00, 0x08);
    regs[5] = sdm & 0xff; regs[6] = sdm >> 8;
    await this._rEcrire(0x10, regs);
    let e = null;
    for (let i = 0; i < 2; i++) {
      e = await this._rLire(3);
      if (e[2] & 0x40) break;
      if (!i) await this._rMasque(0x12, 0x60, 0xe0);   // pas verrouillée : plus de courant au VCO
    }
    r.verrou = !!(e[2] & 0x40);
    if (r.verrou) await this._rMasque(0x1a, 0x08, 0x08);
  }

  async _r82xxFrequence(freq) {
    const r = this.r82, v4 = this.blogV4;
    const entree = v4 && freq < 28_800_000 ? freq + 28_800_000 : freq;   // V4 : convertisseur HF intégré
    const lo = entree + r.fi;
    // multiplexeur et filtre de suivi
    const mhz = Math.floor(lo / 1e6);
    let i = 0; while (i < R82XX_PLAGES.length - 1 && mhz >= R82XX_PLAGES[i + 1][0]) i++;
    const [, openD, mux, tfc] = R82XX_PLAGES[i];
    await this._rMasque(0x17, openD, 0x08);
    await this._rMasque(0x1a, mux, 0xc3);
    await this._rReg(0x1b, tfc);
    await this._rMasque(0x10, 0x00, 0x0b);   // XTAL_HIGH_CAP_0P
    await this._rMasque(0x08, 0x00, 0x3f);
    await this._rMasque(0x09, 0x00, 0x3f);
    await this._r82xxPll(lo);
    if (!r.verrou) return;
    if (v4) {
      const MHZ = x => x * 1e6;
      await this._rMasque(0x17, (freq <= MHZ(2.2) || (freq >= MHZ(85) && freq <= MHZ(112)) || (freq >= MHZ(172) && freq <= MHZ(242))) ? 0x00 : 0x08, 0x08);
      const bande = freq <= MHZ(28.8) ? 1 : freq < MHZ(250) ? 2 : 3;   // HF, VHF, UHF
      if (bande === 1) { await this._rMasque(0x1a, 0x40, 0xc3); await this._rReg(0x1b, 0x00); }
      if (bande !== r.entree) {
        r.entree = bande;
        const cable2 = bande === 1 ? 0x08 : 0x00;
        await this._rMasque(0x06, cable2, 0x08);
        await this._gpio(5, !cable2);
        await this._rMasque(0x05, bande === 2 ? 0x40 : 0x00, 0x40);
        await this._rMasque(0x05, bande === 3 ? 0x00 : 0x20, 0x20);
      }
    } else if (this.tuner === "R828D") {
      const air = freq > 345e6 ? 0x00 : 0x60;
      if (air !== r.entree) { r.entree = air; await this._rMasque(0x05, air, 0x60); }
    }
  }

  async _r82xxGain(dixiemes) {
    if (dixiemes === null) {                       // AGC du tuner
      await this._rMasque(0x05, 0, 0x10);
      await this._rMasque(0x07, 0x10, 0x10);
      await this._rMasque(0x0c, 0x0b, 0x9f);
      return;
    }
    await this._rMasque(0x05, 0x10, 0x10);
    await this._rMasque(0x07, 0, 0x10);
    await this._rLire(4);
    await this._rMasque(0x0c, 0x08, 0x9f);         // VGA fixe 16,3 dB
    let total = 0, lna = 0, mix = 0;
    for (let i = 0; i < 15; i++) {
      if (total >= dixiemes) break;
      total += R82XX_LNA[++lna];
      if (total >= dixiemes) break;
      total += R82XX_MIX[++mix];
    }
    await this._rMasque(0x05, lna, 0x0f);
    await this._rMasque(0x07, mix, 0x0f);
  }

  /** Veille du tuner (r82xx_standby), avant l'échantillonnage direct. */
  async _r82xxVeille() {
    for (const [reg, val] of [[0x06, 0xb1], [0x05, 0xa0], [0x07, 0x3a], [0x08, 0x40], [0x09, 0xc0], [0x0a, 0x36],
      [0x0c, 0x35], [0x0f, 0x68], [0x11, 0x03], [0x17, 0xf4], [0x19, 0x0c]]) await this._rReg(reg, val);
  }

  /** Filtre de FI selon la bande voulue ; renvoie la nouvelle FI. */
  async _r82xxBande(bw) {
    let r0a, r0b, fi;
    if (bw > 7e6) { r0a = 0x10; r0b = 0x0b; fi = 4_570_000; }
    else if (bw > 6e6) { r0a = 0x10; r0b = 0x2a; fi = 4_570_000; }
    else if (bw > R82XX_PASSE_BAS[0] + 350000 + 380000) { r0a = 0x10; r0b = 0x6b; fi = 3_570_000; }
    else {
      r0a = 0x00; r0b = 0x80; fi = 2_300_000; let reel = 0;
      if (bw > R82XX_PASSE_BAS[0] + 350000) { bw -= 380000; fi += 380000; reel += 380000; } else r0b |= 0x20;
      if (bw > R82XX_PASSE_BAS[0]) { bw -= 350000; fi += 350000; reel += 350000; } else r0b |= 0x40;
      let i = 0; while (i < R82XX_PASSE_BAS.length && !(bw > R82XX_PASSE_BAS[i])) i++;
      i--;
      r0b |= 15 - i; reel += R82XX_PASSE_BAS[i];
      fi -= Math.trunc(reel / 2);
    }
    await this._rMasque(0x0a, r0a, 0x10);
    await this._rMasque(0x0b, r0b, 0xef);
    return fi;
  }

  /** Sortie GPIO du RTL2832U (commutation du convertisseur HF de la V4). */
  async _gpio(n, actif) {
    const bit = 1 << n, GPO = 0x3001, GPOE = 0x3003, GPD = 0x3004;
    await this.ecrireReg(BLOC.SYS, GPD, (await this.lireReg(BLOC.SYS, GPD, 1)) & ~bit, 1);    // en sortie
    await this.ecrireReg(BLOC.SYS, GPOE, (await this.lireReg(BLOC.SYS, GPOE, 1)) | bit, 1);
    const r = await this.lireReg(BLOC.SYS, GPO, 1);
    await this.ecrireReg(BLOC.SYS, GPO, actif ? r | bit : r & ~bit, 1);
  }

  // ------------------------------------------------ lecture des échantillons
  /** Lit en continu (plusieurs transferts en vol) et appelle rappel(Uint8Array) pour chaque bloc. */
  async demarrerLecture(octetsParBloc, rappel, enVol = 4) {
    this.lecture = true;
    const boucle = async () => {
      while (this.lecture) {
        let r;
        try { r = await this.usb.transferIn(1, octetsParBloc); }
        catch (e) { if (this.lecture) { this.lecture = false; this.surErreur?.(e); } return; }
        if (r.status === "ok" && r.data && this.lecture) rappel(new Uint8Array(r.data.buffer, r.data.byteOffset, r.data.byteLength));
      }
    };
    this._boucles = Array.from({ length: enVol }, boucle);
  }
  async arreterLecture() {
    this.lecture = false;
    try { await Promise.allSettled(this._boucles || []); } catch { /* rien */ }
  }
  async fermer() {
    await this.arreterLecture();
    try { await this.ecrireReg(BLOC.SYS, REG.DEMOD_CTL, 0x20, 1); } catch { /* clé déjà débranchée */ }
    try { await this.usb.releaseInterface(0); } catch { /* idem */ }
    try { await this.usb.close(); } catch { /* idem */ }
  }
}
