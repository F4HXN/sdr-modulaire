// SPDX-License-Identifier: GPL-2.0-or-later
// SDR Modulaire (version web) : pilote WebUSB pour clé RTL2832U + tuner Fitipower FC0013.
//
// Portage en JavaScript de librtlsdr (projet osmocom rtl-sdr) :
//   librtlsdr.c   © 2012-2014 Steve Markgraf, Dimitri Stolnikov, Hoernchen, Kyle Keen
//   tuner_fc0013.c © 2012 Hans-Frieder Vogt, © 2010 Fitipower Integrated Technology Inc,
//                  © 2012 Steve Markgraf
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

export class CleRtlSdr {
  constructor(usb) { this.usb = usb; this.nom = "RTL-SDR"; this.ppm = 0; this.freq = 0; this.taux = 0; this.lecture = false; }

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
    if (this.tuner !== "FC0013") {
      await this.repeteurI2C(false);
      const nom = this.tuner ? NOMS_TUNERS[this.tuner] : "inconnu";
      await this.fermer();
      throw new Error(`Tuner ${nom} : la version web ne gère pour l'instant que le Fitipower FC0013. `
        + "Les autres tuners viendront ensuite ; en attendant, la version Linux les gère tous.");
    }
    await this._fc0013Init();
    await this.repeteurI2C(false);
    this.modele = u.productName || "RTL2832U";
    this.fabricant = u.manufacturerName || "";
    this.serie = u.serialNumber || "";
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
    if (this.freq) await this.reglerFrequence(this.freq);
  }
  async reglerFrequence(f) {
    f = Math.round(f);
    await this.repeteurI2C(true);
    try { await this._fc0013Frequence(f); } finally { await this.repeteurI2C(false); }
    this.freq = f;
  }
  gains() { return [...new Set(GAINS_FC0013.map(g => g[0] / 10))]; }
  /** gain en dB, ou null pour l'AGC du tuner. */
  async reglerGain(gain) {
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
             couverture: "22–1100 MHz", gains: this.gains() };
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
