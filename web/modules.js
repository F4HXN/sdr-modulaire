// SPDX-License-Identifier: MIT
// SDR Web : modules ajoutés et retirés à chaud, sur le même principe que SDR Modulaire.
//
// Un module est une classe avec :
//   static nom, static description
//   demarrer()        : état initial et abonnements (this.ctx.abonner(sujet, rappel))
//   creerPanneau()    : facultatif, renvoie un élément HTML affiché dans la colonne des modules
//   arreter()         : minuteries, fichiers… (les abonnements sont retirés automatiquement)
// Sujets : "niveau" (dBFS, puis la mesure de modulation du démodulateur, voir dsp.js), "audio" (Float32Array 48 kHz), "iq" (re, im, centre, taux), "vfo" (fréquence, mode).
// Contexte : frequence(), mode(), reglerFrequence(f), reglerMode(m), taux(), reglerTaux(t) (asynchrone),
//            gains() (dB), gain() (dB, null = automatique), reglerGain(g) (null = automatique),
//            lire(clé, défaut), ecrire(clé, valeur).
// Les rappels du bus doivent rester rapides : on stocke, et une minuterie met l'affichage à jour.

import { FirComplexe, NCO, passeBas, LARGEURS, FFT, MODES } from "./dsp.js?v=20261122-cw";
import { StereoRds } from "./fm-stereo.js?v=20261122-cw";
import { DecodeurOOK433, TAUX_CAPTEURS } from "./capteurs433.js";
import { DemodModeS, Trafic, distanceKm } from "./adsb.js?v=20261122-niveau";
import { CarteRadar, couleurAltitude, couleurSymbole } from "./carte-adsb.js?v=20261123-ais";
import { MoteurAIS, CANAUX as CANAUX_AIS, decoderMessage as decoderAIS, octetsVersBits, texteMessage as texteAIS, categorie as categorieAIS, couleurNavire, nomNavire } from "./ais.js?v=20261123-ais";
import { DecodeurSSTV, MODES_SSTV } from "./sstv.js?v=20261122-sstv";
import { TAUX_CW, DUREE_FENETRE, DUREE_PAS } from "./cw.js?v=20261122-cw";
import { DemodAFSK, trameVersTexte, positionAPRS } from "./aprs.js?v=20261122-carteaprs";
import { enteteWav, iqVersOctets, nomFichier, lireEnteteWav, SourceFichierIQ, LIMITE_WAV } from "./enregistreur-iq.js?v=20261123-iq";
import { DecodeurFax, STATIONS as STATIONS_FAX, LARGEUR as LARGEUR_FAX, TAUX_FAX, DECALAGE_USB } from "./fax-meteo.js?v=20261123-fax";
import { MoteurACARS, LISTES as LISTES_ACARS, texteMessage as texteACARS, mhz as mhzACARS } from "./acars.js?v=20261123-acars";
import { MoteurTonalites, TAUX_TONALITES, texteCtcss, secondes } from "./tonalites.js?v=20261123-tonalites";
import { MoteurVumetre, dessiner as dessinerVumetre, frequenceMax, bords as bordsVumetre, BANDES as BANDES_VUMETRE } from "./vumetre.js?v=20261124-vumetre";
import { DecodeurDMR as MoteurDMR, TAUX_DMR, FID as FID_DMR, SAP as SAP_DMR, nomSlot, texteAppel } from "./dmr.js?v=20261122-dmr";

const el = (balise, props = {}, ...enfants) => { const e = Object.assign(document.createElement(balise), props); e.append(...enfants); return e; };

/**
 * Fenêtre flottante (dialogue non modal) : déplacée en tirant la poignée (barre de titre),
 * redimensionnée par son coin (CSS resize), position et taille retenues avec ctx.ecrire().
 * Échap la ferme. Le récepteur reste utilisable pendant qu'elle est ouverte.
 */
function fenetreFlottante(dialogue, poignee, ctx, cle = "fenetre") {
  const borne = (v, min, max) => Math.max(min, Math.min(max, v));
  const placer = (x, y) => {
    const r = dialogue.getBoundingClientRect();
    dialogue.style.left = `${borne(x, 0, innerWidth - Math.min(r.width, 120))}px`;
    dialogue.style.top = `${borne(y, 0, innerHeight - 48)}px`;
  };
  const retenir = () => {
    const r = dialogue.getBoundingClientRect();
    ctx.ecrire(cle, { x: Math.round(r.left), y: Math.round(r.top), l: Math.round(r.width), h: Math.round(r.height) });
  };
  poignee.classList.add("poignee");
  poignee.addEventListener("pointerdown", e => {
    if (e.button !== 0 || e.target.closest("button, input, select, a")) return;
    const r = dialogue.getBoundingClientRect(), dx = e.clientX - r.left, dy = e.clientY - r.top;
    try { poignee.setPointerCapture(e.pointerId); } catch { /* pointeur déjà relâché */ }
    const bouger = ev => placer(ev.clientX - dx, ev.clientY - dy);
    const lacher = () => { poignee.removeEventListener("pointermove", bouger); poignee.removeEventListener("pointerup", lacher); retenir(); };
    poignee.addEventListener("pointermove", bouger); poignee.addEventListener("pointerup", lacher);
    e.preventDefault();
  });
  let attente;
  new ResizeObserver(() => { if (dialogue.open) { clearTimeout(attente); attente = setTimeout(retenir, 300); } }).observe(dialogue);
  dialogue.addEventListener("keydown", e => { if (e.key === "Escape") dialogue.close(); });
  return () => {                                   // ouvrir : taille et place retenues, sinon centrée
    const g = ctx.lire(cle, null);
    if (g) { dialogue.style.width = `${Math.min(g.l, innerWidth - 16)}px`; dialogue.style.height = `${Math.min(g.h, innerHeight - 16)}px`; }
    dialogue.show();
    const r = dialogue.getBoundingClientRect();
    placer(g ? g.x : (innerWidth - r.width) / 2, g ? g.y : (innerHeight - r.height) / 2);
  };
}

/**
 * Panneau « agrandissable » (décodeurs CW, APRS, FT8 et FT4) : le bouton Agrandir fait passer le contenu dans
 * une fenêtre flottante (déplaçable, redimensionnable, taille et place retenues), avec A−/A+ pour la
 * taille du texte (variable CSS --cw-taille, retenue) et Réduire (ou Échap) pour le remettre en place.
 * actions : la ligne de boutons du panneau, où le bouton Agrandir est ajouté.
 * defiler : élément à faire défiler en bas à l'ouverture (ou null).
 * tailleTexte : false pour les décodeurs d'images (SSTV, fax météo) : pas de boutons A− / A+.
 */
function panneauAgrandissable(ctx, { titre: texteTitre, id, contenu, actions, tailleDefaut, defiler = null, attenteTexte, tailleTexte = true }) {
  const agrandir = el("button", { type: "button", textContent: "Agrandir", className: "cw-agrandir" });
  agrandir.setAttribute("aria-haspopup", "dialog");
  actions.append(agrandir);
  const titre = el("h2", { id, textContent: texteTitre });
  const plusPetit = el("button", { type: "button", textContent: "A−", title: "Texte plus petit" });
  const plusGrand = el("button", { type: "button", textContent: "A+", title: "Texte plus grand" });
  const fermer = el("button", { type: "button", textContent: "Réduire", title: "Remettre dans la colonne des modules (Échap)" });
  const entete = el("header", { title: "Tirer pour déplacer · coin en bas à droite pour agrandir" }, titre,
    el("span", { className: "cw-boutons" }, ...(tailleTexte ? [plusPetit, plusGrand] : []), fermer));
  const dialogue = el("dialog", { className: "capteurs-dialogue cw-dialogue" }, entete);
  dialogue.setAttribute("aria-labelledby", id);
  let taille = ctx.lire("taille", tailleDefaut);
  const police = d => {
    taille = Math.max(0.6, Math.min(4, +(taille + d).toFixed(1)));
    ctx.ecrire("taille", taille); dialogue.style.setProperty("--cw-taille", `${taille}rem`);
  };
  plusPetit.onclick = () => police(-0.2); plusGrand.onclick = () => police(0.2);
  police(0);
  const ouvrir = fenetreFlottante(dialogue, entete, ctx);
  const attente = el("div", { className: "petit", textContent: attenteTexte });
  const panneau = el("div", {}, contenu, dialogue);
  agrandir.onclick = () => {
    if (dialogue.open) { fermer.focus(); return; }
    agrandir.remove(); dialogue.append(contenu); panneau.prepend(attente);
    ouvrir(); if (defiler) defiler.scrollTop = defiler.scrollHeight; fermer.focus();
  };
  fermer.onclick = () => dialogue.close();
  dialogue.addEventListener("close", () => { attente.remove(); actions.append(agrandir); panneau.prepend(contenu); agrandir.focus(); });
  return { panneau, dialogue };
}

const MEMOIRES_DEFAUT = [
  { nom: "APRS", f: 144_800_000, mode: "NFM" }, { nom: "Appel FM 2 m", f: 145_500_000, mode: "NFM" },
  { nom: "Appel FM 70 cm", f: 433_500_000, mode: "NFM" }, { nom: "Radio FM (exemple)", f: 87_800_000, mode: "WFM" },
];

// S-mètre : cadran à aiguille et barre de LED. Échelle relative (la clé n'est pas étalonnée en dBm) :
// 6 dB par point S, S9 à S9_DBFS ; au-delà, +10 à +60 dB.
const S9_DBFS = -55, S_MIN = S9_DBFS - 8 * 6 - 6, S_MAX = S9_DBFS + 60;   // S0 et S9+60
const NB_LED = 30;
/** Position 0..1 sur l'échelle : S0 à S9 sur les 60 % de gauche, +10 à +60 sur le reste. */
function positionS(db) {
  return db <= S9_DBFS ? 0.6 * Math.max(0, (db - S_MIN) / (S9_DBFS - S_MIN))
    : 0.6 + 0.4 * Math.min(1, (db - S9_DBFS) / (S_MAX - S9_DBFS));
}
function texteS(db) {
  if (db <= S9_DBFS) return `S${Math.max(0, Math.min(9, Math.round((db - S_MIN) / 6)))}`;
  return `S9+${Math.min(60, Math.round((db - S9_DBFS) / 10) * 10 || 0)}`;
}

/** Échelle du cadran : position 0..1, graduations, début de la zone rouge, texte. */
function echelleSignal() {
  const grad = [];
  for (let n = 1; n <= 9; n++) grad.push({ p: positionS(S_MIN + 6 * n), longue: n % 2 === 1, texte: n % 2 ? String(n) : "", rouge: false });
  for (let d = 10; d <= 60; d += 10) grad.push({ p: positionS(S9_DBFS + d), longue: d % 20 === 0, texte: d % 20 ? "" : `+${d}`, rouge: true });
  return { cle: "signal", pos: positionS, grad, rouge: 0.6, titre: "S",
    texte: (v, c) => `${texteS(v)} · ${v.toFixed(1)} dBFS · crête ${texteS(c)}` };
}
function echelleModulation(m) {
  const pas = { "%": 25, dB: 10 }[m.unite] || m.max / 4;
  const grad = [];
  for (let v = 0; v <= m.max + 1e-9; v += pas / 2) {
    const longue = Math.abs(v / pas - Math.round(v / pas)) < 1e-6;
    grad.push({ p: v / m.max, longue, texte: longue ? String(+v.toFixed(1)) : "", rouge: m.nominal !== null && v > m.nominal + 1e-9 });
  }
  const nom = { FM: "Excursion", AM: "Modulation", BLU: "Voix", CW: "Signal CW" }[m.type];
  const f = v => m.unite === "%" ? `${Math.round(v)} %` : `+${Math.round(v)} dB`;
  return { cle: `${m.type}/${m.nominal}/${m.max}`, pos: v => Math.max(0, v) / m.max, grad,
    rouge: m.nominal === null ? null : m.nominal / m.max, titre: m.unite === "dB" ? "dB" : m.unite,
    texte: (v, c) => `${nom} ${f(v)} · crête ${f(c)}${m.unite === "dB" ? " au-dessus du bruit" : m.nominal !== null && c > 1.05 * m.nominal ? " · surmodulation" : ""}` };
}

class SMetre {
  static nom = "S-mètre";
  static description = "Cadran à aiguille et barre de LED : modulation (excursion FM, taux AM, niveau BLU) ou force du signal (points S).";
  demarrer() {
    this.vue = this.ctx.lire("vue", "signal");
    this.niveau = -120; this.modulation = null;
    this.derniereMesure = -Infinity;
    this.crete = -Infinity; this.aiguille = 0; this.tCrete = 0;
    this.ctx.abonner("niveau", (db, m) => {
      if (!Number.isFinite(db)) return;
      this.niveau = db;
      this.derniereMesure = performance.now();
      this.modulation = null;
      if (!m || !Number.isFinite(m.valeur)) return;
      const presence = m.presence ?? 1;
      const valeur = m.valeur * presence;
      // En FM, 100 % correspond à l’excursion nominale du mode sélectionné.
      this.modulation = m.type === "FM"
        ? { ...m, valeur: 100 * valeur / m.nominal, nominal: 100, max: Math.ceil(100 * m.max / m.nominal), unite: "%" }
        : { ...m, valeur };
    });
  }
  creerPanneau() {
    const boutons = ["modulation", "signal"].map(v => {
      const b = el("button", { type: "button", textContent: v === "modulation" ? "Modulation" : "Signal" });
      b.onclick = () => { this.vue = v; this.ctx.ecrire("vue", v); maj(); };
      return b;
    });
    const maj = () => boutons.forEach((b, i) => b.setAttribute("aria-pressed", String(["modulation", "signal"][i] === this.vue)));
    maj();
    const choix = el("div", { className: "smetre-choix", role: "group", ariaLabel: "Mesure affichée" }, ...boutons);
    this.toile = el("canvas", { className: "smetre-cadran" });
    this.toile.setAttribute("aria-hidden", "true");
    this.leds = Array.from({ length: NB_LED }, () => el("i"));
    const barre = el("div", { className: "smetre-leds" }, ...this.leds);
    this.txt = el("div", { className: "smetre-txt", role: "status" });
    let avant = performance.now(), dernierTexte = 0, cleEchelle = "";
    const signal = echelleSignal();
    const animer = maintenant => {
      this.anim = requestAnimationFrame(animer);
      const dt = Math.min(0.1, (maintenant - avant) / 1000); avant = maintenant;
      const actif = maintenant - this.derniereMesure < 1000;
      const modulation = this.vue === "modulation" ? this.modulation : null;
      const e = modulation ? echelleModulation(modulation) : signal;
      if (e.cle !== cleEchelle) {              // nouvelle échelle : crête remise à zéro, couleurs des LED
        cleEchelle = e.cle; this.crete = -Infinity;
        const r = e.rouge ?? 2;
        this.leds.forEach((led, i) => { const p = (i + 0.5) / NB_LED; led.className = p < r - 0.12 ? "v" : p < r ? "j" : "r"; });
      }
      const v = modulation ? (actif ? modulation.valeur : 0) : (actif ? this.niveau : -120);
      if (!actif) this.crete = v;
      // Balistique d'un galvanomètre : montée rapide, retombée plus lente.
      const cible = Math.max(0, Math.min(1.02, e.pos(v)));
      this.aiguille += (cible - this.aiguille) * (1 - Math.exp(-dt / (cible > this.aiguille ? 0.05 : 0.35)));
      if (v >= this.crete) { this.crete = v; this.tCrete = maintenant; }
      else if (maintenant - this.tCrete > 1500) this.crete = Math.max(v, this.crete - (e.cle === "signal" ? 20 : 0.4 * modulation.max) * dt);
      this.dessiner(e);
      const allumees = Math.round(Math.min(1, this.aiguille) * NB_LED), iCrete = Math.min(NB_LED - 1, Math.floor(e.pos(this.crete) * NB_LED));
      this.leds.forEach((led, i) => { led.classList.toggle("on", i < allumees); led.classList.toggle("crete", i === iCrete && i >= allumees); });
      if (maintenant - dernierTexte > 200) { dernierTexte = maintenant; this.txt.textContent = e.texte(v, this.crete); }
    };
    this.anim = requestAnimationFrame(animer);
    return el("div", { className: "smetre" }, choix, this.toile, barre, this.txt);
  }
  dessiner(e) {
    const c = this.toile, r = devicePixelRatio || 1, W = c.clientWidth, H = c.clientHeight;
    if (!W || !H) return;
    if (c.width !== Math.round(W * r) || c.height !== Math.round(H * r)) { c.width = Math.round(W * r); c.height = Math.round(H * r); }
    const g = c.getContext("2d");
    g.setTransform(r, 0, 0, r, 0, 0); g.clearRect(0, 0, W, H);
    // Cadran : arc de 100° centré, pivot sous le bas du cadran.
    const ouverture = 100 * Math.PI / 180, cx = W / 2, rayon = Math.min(W * 0.62, H * 1.25), cy = H * 0.12 + rayon;
    const angle = p => -Math.PI / 2 - ouverture / 2 + p * ouverture;
    const point = (p, d) => [cx + d * Math.cos(angle(p)), cy + d * Math.sin(angle(p))];
    g.fillStyle = "#000"; g.beginPath(); g.roundRect(0, 0, W, H, 8); g.fill();
    // Arcs : blanc, puis rouge dans la zone de dépassement.
    const rouge = e.rouge ?? 1;
    g.lineWidth = 2.5; g.strokeStyle = "#fff"; g.beginPath(); g.arc(cx, cy, rayon * 0.86, angle(0), angle(rouge)); g.stroke();
    if (rouge < 1) { g.strokeStyle = "#d22"; g.lineWidth = 5; g.beginPath(); g.arc(cx, cy, rayon * 0.86 + 1, angle(rouge), angle(1)); g.stroke(); }
    g.textAlign = "center"; g.textBaseline = "middle";
    g.font = `600 ${Math.max(9, Math.round(W / 22))}px "Share Tech Mono", monospace`;
    for (const t of e.grad) {
      const [x1, y1] = point(t.p, rayon * 0.86), [x2, y2] = point(t.p, rayon * (t.longue ? 0.79 : 0.82));
      g.strokeStyle = t.rouge ? "#d22" : "#fff"; g.lineWidth = t.longue ? 2 : 1.2;
      g.beginPath(); g.moveTo(x1, y1); g.lineTo(x2, y2); g.stroke();
      if (t.texte) { const [xt, yt] = point(t.p, rayon * 0.95); g.fillStyle = "#fff"; g.fillText(t.texte, xt, yt); }
    }
    g.fillStyle = "#fff"; g.font = `700 ${Math.max(10, Math.round(W / 18))}px "Orbitron", sans-serif`;
    g.fillText(e.titre, cx, cy - rayon * 0.5);
    // Repère de crête (point rouge) et aiguille.
    if (Number.isFinite(this.crete)) {
      const [xc, yc] = point(Math.max(0, Math.min(1, e.pos(this.crete))), rayon * 0.74);
      g.fillStyle = "#d22"; g.beginPath(); g.arc(xc, yc, 2.5, 0, 2 * Math.PI); g.fill();
    }
    const [xa, ya] = point(Math.max(0, Math.min(1.02, this.aiguille)), rayon * 0.9);
    g.save(); g.beginPath(); g.roundRect(0, 0, W, H, 8); g.clip();
    g.strokeStyle = "rgba(0,0,0,.25)"; g.lineWidth = 3; g.beginPath(); g.moveTo(cx + 2, cy + 2); g.lineTo(xa + 2, ya + 2); g.stroke();
    g.strokeStyle = "#fff"; g.lineWidth = 1.8; g.beginPath(); g.moveTo(cx, cy); g.lineTo(xa, ya); g.stroke();
    g.restore();
    g.fillStyle = "#2a2a2a"; g.beginPath(); g.roundRect(0, H - 10, W, 10, [0, 0, 8, 8]); g.fill();
  }
  arreter() { cancelAnimationFrame(this.anim); }
}

// Mémoires : même gestion et mêmes fichiers que l'application (CSV compatible CHIRP, JSON).
const TOUS_GROUPES = "Tous";
const COLONNES_CHIRP = ["Location", "Name", "Frequency", "Duplex", "Offset", "Tone", "rToneFreq", "cToneFreq", "DtcsCode",
  "DtcsPolarity", "RxDtcsCode", "CrossMode", "Mode", "TStep", "Skip", "Power", "Comment", "URCALL", "RPT1CALL", "RPT2CALL", "DVCODE"];
const VERS_CHIRP = { NFM: "FM", WFM: "WFM", AM: "AM", USB: "USB", LSB: "LSB", CW: "CW" };
const DEPUIS_CHIRP = { FM: "NFM", NFM: "NFM", WFM: "WFM", AM: "AM", NAM: "AM", USB: "USB", LSB: "LSB", CW: "CW", CWR: "CW" };

/** « 145.5 », « 145,500 » (MHz) ou « 145500000 » (Hz) → Hz ; null si illisible. */
export function lireFrequence(t) {
  const v = Number(String(t ?? "").trim().replace(",", ".").replace(/\s/g, ""));
  if (!(v > 0) || !String(t ?? "").trim()) return null;
  return Math.round(v < 100_000 ? v * 1e6 : v);
}
/** Mémoire venue d'un fichier (forme de l'application ou de SDR Web) → { nom, f, mode, groupe } ou null. */
export function normaliserMemoire(m) {
  if (!m || typeof m !== "object") return null;
  const brut = m.f ?? m.frequence;
  const f = typeof brut === "number" ? (brut > 0 ? Math.round(brut) : null) : typeof brut === "string" ? lireFrequence(brut) : null;
  if (f === null) return null;
  const mode = String(m.mode ?? "NFM").toUpperCase();
  return { nom: String(m.nom ?? "").trim() || "—", f, mode: MODES.includes(mode) ? mode : "NFM", groupe: String(m.groupe ?? "").trim() };
}
const champCsv = v => /[",\r\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
export function versCsvChirp(liste) {
  const lignes = [COLONNES_CHIRP.join(",")];
  [...liste].sort((a, b) => a.f - b.f).forEach((m, i) => {
    const l = Object.fromEntries(COLONNES_CHIRP.map(c => [c, ""]));
    Object.assign(l, { Location: String(i), Name: m.nom, Frequency: (m.f / 1e6).toFixed(6), Offset: "0.000000", rToneFreq: "88.5",
      cToneFreq: "88.5", DtcsCode: "023", DtcsPolarity: "NN", RxDtcsCode: "023", CrossMode: "Tone->Tone",
      Mode: VERS_CHIRP[m.mode] || "FM", TStep: "5.00", Power: "50W", Comment: m.groupe || "" });   // CHIRP refuse une puissance vide
    lignes.push(COLONNES_CHIRP.map(c => champCsv(l[c])).join(","));
  });
  return lignes.join("\r\n") + "\r\n";
}
function lignesCsv(texte) {
  const premiere = texte.split(/\r?\n/, 1)[0];
  const sep = [",", ";", "\t"].reduce((a, b) => premiere.split(b).length > premiere.split(a).length ? b : a);
  const lignes = []; let ligne = [], champ = "", guillemets = false;
  for (let i = 0; i < texte.length; i++) {
    const c = texte[i];
    if (guillemets) {
      if (c === '"' && texte[i + 1] === '"') { champ += '"'; i++; }
      else if (c === '"') guillemets = false;
      else champ += c;
    } else if (c === '"') guillemets = true;
    else if (c === sep) { ligne.push(champ); champ = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && texte[i + 1] === "\n") i++;
      ligne.push(champ); lignes.push(ligne); ligne = []; champ = "";
    } else champ += c;
  }
  if (champ || ligne.length) { ligne.push(champ); lignes.push(ligne); }
  return lignes.filter(l => l.some(x => x.trim()));
}
/** CSV de CHIRP (Name, Frequency, Mode, Comment) ou en français (Nom, Fréquence, Mode, Groupe). */
export function depuisCsv(texte) {
  const [tete, ...lignes] = lignesCsv(texte.replace(/^﻿/, ""));
  if (!tete) return [];
  const col = noms => tete.findIndex(t => noms.includes(t.trim().toLowerCase()));
  const iF = col(["frequency", "fréquence", "frequence", "fréquence (mhz)", "mhz"]), iN = col(["name", "nom"]);
  const iM = col(["mode"]), iG = col(["comment", "groupe"]);
  if (iF < 0) return [];
  return lignes.map(l => {
    const mode = (l[iM] ?? "").trim().toUpperCase();
    return normaliserMemoire({ nom: l[iN] ?? "", frequence: l[iF] ?? "", mode: DEPUIS_CHIRP[mode] ?? (mode || "NFM"), groupe: l[iG] ?? "" });
  }).filter(Boolean);
}

class Memoires {
  static nom = "Mémoires";
  static description = "Fréquences favorites : ajouter, modifier, ranger par groupe, rappeler, sauvegarder dans un fichier.";
  demarrer() {
    this.liste = this.ctx.lire("liste", MEMOIRES_DEFAUT).map(normaliserMemoire).filter(Boolean).sort((a, b) => a.f - b.f);
    this.groupe = TOUS_GROUPES; this.recherche = "";
  }
  groupes() { return [...new Set(this.liste.map(m => m.groupe).filter(Boolean))].sort((a, b) => a.localeCompare(b, "fr")); }
  creerPanneau() {
    const cherche = el("input", { type: "search", placeholder: "Rechercher…", className: "mem-recherche" });
    cherche.oninput = () => { this.recherche = cherche.value; this.remplir(); };
    this.choixGroupe = el("select", { className: "mem-groupe", title: "N'afficher qu'un groupe" });
    this.choixGroupe.onchange = () => { this.groupe = this.choixGroupe.value; this.remplir(); };
    this.ul = el("ul", { className: "memoires" });
    const bouton = (texte, titre, action) => { const b = el("button", { type: "button", textContent: texte, title: titre }); b.onclick = action; return b; };
    this.fichier = el("input", { type: "file", accept: ".csv,.json,text/csv,application/json", hidden: true });
    this.fichier.onchange = async () => {
      const f = this.fichier.files[0]; this.fichier.value = ""; if (!f) return;
      const r = this.importerTexte(await f.text());
      this.etat.textContent = r ? `${r[0]} mémoire(s) ajoutée(s) depuis ${f.name}${r[1] ? `, ${r[1]} déjà présente(s)` : ""}` : `Aucune mémoire reconnue dans ${f.name}.`;
    };
    this.etat = el("div", { className: "petit" });
    this.remplir();
    return el("div", {},
      el("div", { className: "scanner-choix" }, cherche, this.choixGroupe), this.ul,
      el("div", { className: "scanner-boutons" },
        bouton("Ajouter", "Mémoriser la fréquence et le mode actuels", () => this.editer(null)),
        bouton("Sauvegarder…", "Télécharger les mémoires : CSV compatible CHIRP", () => this.exporter("csv")),
        bouton("JSON", "Télécharger les mémoires en JSON (même format que l'application)", () => this.exporter("json")),
        bouton("Importer…", "Ajouter des mémoires depuis un fichier CSV (CHIRP) ou JSON", () => this.fichier.click())),
      this.fichier, this.etat);
  }
  numero(i) { return `CH ${String(i + 1).padStart(String(this.liste.length).length, "0")}`; }
  remplir() {
    const groupes = [TOUS_GROUPES, ...this.groupes()];
    if (!groupes.includes(this.groupe)) this.groupe = TOUS_GROUPES;
    this.choixGroupe.replaceChildren(...groupes.map(g => new Option(g, g, false, g === this.groupe)));
    const q = this.recherche.trim().toLowerCase().replace(",", ".");
    let n = 0;
    this.ul.replaceChildren(...this.liste.flatMap((m, i) => {
      if (this.groupe !== TOUS_GROUPES && m.groupe !== this.groupe) return [];
      if (q && !`${m.nom} ${(m.f / 1e6).toFixed(6)} ${m.groupe} ${m.mode}`.toLowerCase().includes(q)) return [];
      n++;
      const b = el("button", { type: "button", className: "rappel", title: `Écouter${m.groupe ? ` · groupe ${m.groupe}` : ""}`,
        textContent: `${this.numero(i)} ${(m.f / 1e6).toFixed(4)} ${m.mode} · ${m.nom}` });
      b.onclick = () => { this.ctx.reglerMode(m.mode); this.ctx.reglerFrequence(m.f); };
      const e = el("button", { type: "button", className: "suppr", textContent: "✎", title: "Modifier" }); e.onclick = () => this.editer(m);
      const x = el("button", { type: "button", className: "suppr", textContent: "×", title: "Supprimer" });
      x.onclick = () => { if (confirm(`Supprimer « ${m.nom} » (${(m.f / 1e6).toFixed(4)} MHz) ?`)) { this.liste = this.liste.filter(y => y !== m); this.sauver(); } };
      return [el("li", {}, b, e, x)];
    }));
    this.etat.textContent = `${n} / ${this.liste.length} mémoire(s) · enregistrées automatiquement dans ce navigateur`;
  }
  /** Boîte d'ajout (m = null : fréquence actuelle) ou de modification. */
  editer(m) {
    const ancien = m ?? { nom: "", f: Math.round(this.ctx.frequence()), mode: this.ctx.mode(), groupe: this.groupe === TOUS_GROUPES ? "" : this.groupe };
    const mhz = f => (f / 1e6).toFixed(6).replace(/\.?0+$/, "");
    const nom = el("input", { type: "text", value: ancien.nom, placeholder: "ex. Relais R2" });
    const freq = el("input", { type: "text", value: mhz(ancien.f), inputMode: "decimal", placeholder: "MHz, ex. 145.500" });
    const mode = el("select", {}, ...MODES.map(x => new Option(x, x, false, x === ancien.mode)));
    const liste = el("datalist", { id: "mem-groupes" }, ...this.groupes().map(g => new Option(g)));
    const groupe = el("input", { type: "text", value: ancien.groupe, placeholder: "aucun (ex. Relais, Aviation…)" }); groupe.setAttribute("list", "mem-groupes");
    const actuelle = el("button", { type: "button", textContent: "Fréquence actuelle", title: "Reprendre la fréquence et le mode du récepteur" });
    actuelle.onclick = () => { freq.value = mhz(this.ctx.frequence()); mode.value = this.ctx.mode(); };
    const erreur = el("div", { className: "petit mem-erreur" });
    const ok = el("button", { type: "submit", textContent: "OK" }), annuler = el("button", { type: "button", textContent: "Annuler" });
    const ligne = (titre, ...c) => el("label", {}, el("span", { textContent: titre }), ...c);
    const d = el("dialog", { className: "mem-dialogue" }, el("form", { method: "dialog" },
      el("h2", { textContent: m ? "Modifier la mémoire" : "Nouvelle mémoire" }),
      ligne("Nom", nom), ligne("MHz", freq, actuelle), ligne("Mode", mode), ligne("Groupe", groupe, liste), erreur,
      el("div", { className: "scanner-boutons mem-actions" }, annuler, ok)));
    annuler.onclick = () => d.close();
    d.querySelector("form").onsubmit = e => {
      const f = lireFrequence(freq.value);
      if (f === null) { e.preventDefault(); erreur.textContent = "Fréquence illisible : en MHz, par exemple 145.500"; freq.focus(); return; }
      const nouveau = { nom: nom.value.trim() || "—", f, mode: mode.value, groupe: groupe.value.trim() };
      this.liste = m ? this.liste.map(y => y === m ? nouveau : y) : [...this.liste, nouveau];
      this.sauver();
    };
    d.addEventListener("close", () => d.remove());
    document.body.append(d); d.showModal(); nom.focus(); nom.select();
    return d;
  }
  exporter(format) {
    const contenu = format === "json"
      ? JSON.stringify(this.liste.map(m => ({ nom: m.nom, frequence: m.f, mode: m.mode, groupe: m.groupe })), null, 2)
      : versCsvChirp(this.liste);
    const a = el("a", { href: URL.createObjectURL(new Blob([contenu], { type: format === "json" ? "application/json" : "text/csv" })), download: `memoires.${format}` });
    a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    this.etat.textContent = `${this.liste.length} mémoire(s) sauvegardée(s) dans memoires.${format}`;
  }
  /** Ajoute les mémoires d'un fichier JSON ou CSV, sans doublon (fréquence + mode) : [ajoutées, doublons], ou null. */
  importerTexte(texte) {
    let liste = [];
    if (texte.trimStart().startsWith("[")) { try { liste = JSON.parse(texte).map(normaliserMemoire).filter(Boolean); } catch { liste = []; } }
    else liste = depuisCsv(texte);
    if (!liste.length) return null;
    const connues = new Set(this.liste.map(m => `${m.f}/${m.mode}`)), nouvelles = [];
    for (const m of liste) if (!connues.has(`${m.f}/${m.mode}`)) { connues.add(`${m.f}/${m.mode}`); nouvelles.push(m); }
    this.liste.push(...nouvelles); this.sauver();
    return [nouvelles.length, liste.length - nouvelles.length];
  }
  sauver() { this.liste.sort((a, b) => a.f - b.f); this.ctx.ecrire("liste", this.liste); this.remplir(); }
}

class Enregistreur {
  static nom = "Enregistreur audio";
  static description = "Enregistre le son démodulé en WAV (48 kHz, 16 bits), téléchargé à l'arrêt.";
  demarrer() { this.morceaux = null; this.ctx.abonner("audio", a => { if (this.morceaux) this.morceaux.push(Int16Array.from(a, x => x * 32767)); }); }
  creerPanneau() {
    this.b = el("button", { type: "button", textContent: "● Enregistrer" });
    this.info = el("div", { className: "petit", textContent: "Prêt" });
    this.b.onclick = () => this.morceaux ? this.terminer() : this.commencer();
    this.minuterie = setInterval(() => { if (this.morceaux) this.info.textContent = `Enregistrement : ${((Date.now() - this.debut) / 1000).toFixed(0)} s`; }, 500);
    return el("div", {}, this.b, this.info);
  }
  commencer() { this.morceaux = []; this.debut = Date.now(); this.f = this.ctx.frequence(); this.mode = this.ctx.mode(); this.b.textContent = "■ Arrêter"; }
  terminer() {
    const m = this.morceaux; this.morceaux = null; this.b.textContent = "● Enregistrer";
    const n = m.reduce((s, x) => s + x.length, 0), v = new DataView(new ArrayBuffer(44 + 2 * n));
    const txt = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
    txt(0, "RIFF"); v.setUint32(4, 36 + 2 * n, true); txt(8, "WAVEfmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
    v.setUint16(22, 1, true); v.setUint32(24, 48000, true); v.setUint32(28, 96000, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    txt(36, "data"); v.setUint32(40, 2 * n, true);
    let o = 44; for (const x of m) for (const s of x) { v.setInt16(o, s, true); o += 2; }
    const a = el("a", { href: URL.createObjectURL(new Blob([v], { type: "audio/wav" })),
      download: `${new Date(this.debut).toISOString().slice(0, 19).replace(/[:T]/g, "-")}_${(this.f / 1e6).toFixed(4)}MHz_${this.mode}.wav` });
    a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    this.info.textContent = `Fichier téléchargé (${(n / 48000).toFixed(0)} s)`;
  }
  arreter() { clearInterval(this.minuterie); if (this.morceaux) this.terminer(); }
}

class Journal {
  static nom = "Journal d'activité";
  static description = "Note l'heure, la fréquence et le niveau quand un signal dépasse −60 dBFS.";
  demarrer() {
    this.present = false; this.a_noter = []; this.f = this.ctx.frequence();
    this.ctx.abonner("vfo", f => { this.f = f; });
    this.ctx.abonner("niveau", db => {
      if (!this.present && db > -60) { this.present = true; this.a_noter.push([new Date().toLocaleTimeString("fr-FR"), this.f, db]); }
      else if (this.present && db < -63) this.present = false;
    });
  }
  creerPanneau() {
    this.ul = el("ul", { className: "journal" });
    this.minuterie = setInterval(() => {
      while (this.a_noter.length) { const [h, f, db] = this.a_noter.shift(); this.ul.prepend(el("li", { textContent: `${h}  ${(f / 1e6).toFixed(4)} MHz  ${db.toFixed(1)} dBFS` })); }
    }, 200);
    return this.ul;
  }
  arreter() { clearInterval(this.minuterie); }
}

const FREQUENCE_CAPTEURS = 433_920_000;
const BANDE_CAPTEURS = 45_000;

class Capteurs433 {
  static nom = "Capteurs 433 MHz";
  static description = "Sondes Nexus-T, Nexus-TH et Rubicson sur 433,92 MHz.";

  demarrer() {
    this.derniersIQ = -Infinity;
    this.capteurs = new Map();
    this.ctx.abonner("iq", (re, im, centre, taux) => this.recevoir(re, im, centre, taux));
  }

  recevoir(re, im, centre, taux) {
    const maintenant = performance.now();
    const decalage = FREQUENCE_CAPTEURS - centre;
    this.couvert = Math.abs(decalage) + BANDE_CAPTEURS < 0.45 * taux;
    const interrompu = maintenant - this.derniersIQ > 1000;
    this.derniersIQ = maintenant;
    if (!this.couvert) {
      this.decodeur = null;
      return;
    }
    if (!this.decodeur || interrompu || centre !== this.centre || taux !== this.taux) {
      this.centre = centre;
      this.taux = taux;
      this.nco = new NCO(taux);
      this.nco.freq = decalage;
      const facteur = taux / TAUX_CAPTEURS;
      this.filtre = new FirComplexe(passeBas(taux, BANDE_CAPTEURS, facteur * 12 + 1), facteur);
      this.decodeur = new DecodeurOOK433(mesure => {
        const cle = `${mesure.modele}/${mesure.identifiant}/${mesure.canal}`;
        this.capteurs.delete(cle);
        this.capteurs.set(cle, { ...mesure, heure: new Date().toLocaleTimeString("fr-FR") });
        if (this.capteurs.size > 100) this.capteurs.delete(this.capteurs.keys().next().value);
        this.actualiser = true;
      });
    }
    this.decodeur.traiter(...this.filtre.traiter(...this.nco.melanger(re, im)));
  }

  creerPanneau() {
    const accorder = el("button", { type: "button", textContent: "Accorder sur 433,92 MHz" });
    accorder.onclick = () => {
      this.ctx.reglerMode("AM");
      this.ctx.reglerFrequence(FREQUENCE_CAPTEURS);
    };
    const statut = el("div", { className: "petit", textContent: "En attente de la clé RTL-SDR." });
    const liste = el("ul", { className: "journal" });
    const temperature = new Intl.NumberFormat("fr-FR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
    this.minuterie = setInterval(() => {
      if (performance.now() - this.derniersIQ > 1000) {
        statut.textContent = "En attente des échantillons du récepteur.";
      } else if (!this.couvert) {
        statut.textContent = "433,92 MHz hors de la bande reçue. Cliquez sur Accorder.";
      } else {
        statut.textContent = "Écoute de 433,92 MHz";
      }
      if (!this.actualiser) return;
      this.actualiser = false;
      liste.replaceChildren(...Array.from(this.capteurs.values()).reverse().map(m => {
        const humidite = m.humidite === null ? "" : ` · ${m.humidite} %`;
        const pile = m.pile ? "pile OK" : "pile faible";
        return el("li", { textContent: `${m.heure} · ${m.modele} #${m.identifiant}, canal ${m.canal} · ${temperature.format(m.temperature)} °C${humidite} · ${pile}` });
      }));
    }, 150);
    const contenu = el("div", { className: "capteurs-contenu" }, accorder, statut, liste);
    const agrandir = el("button", { type: "button", textContent: "Agrandir", className: "capteurs-agrandir" });
    agrandir.setAttribute("aria-haspopup", "dialog");
    const titre = el("h2", { id: "capteurs-titre", textContent: "Capteurs 433 MHz" });
    const fermer = el("button", { type: "button", textContent: "Réduire", title: "Remettre dans la colonne des modules (Échap)" });
    const entete = el("header", { title: "Tirer pour déplacer · coin en bas à droite pour agrandir" }, titre, fermer);
    const dialogue = el("dialog", { className: "capteurs-dialogue" }, entete);
    dialogue.setAttribute("aria-labelledby", titre.id);
    const ouvrir = fenetreFlottante(dialogue, entete, this.ctx);
    const panneau = el("div", {}, contenu, agrandir, dialogue);
    agrandir.onclick = () => {
      if (dialogue.open) { fermer.focus(); return; }
      dialogue.append(contenu);
      agrandir.textContent = "Fenêtre ouverte";
      ouvrir();
      fermer.focus();
    };
    fermer.onclick = () => dialogue.close();
    dialogue.addEventListener("close", () => {
      panneau.prepend(contenu);
      agrandir.textContent = "Agrandir";
      agrandir.focus();
    });
    this.dialogue = dialogue;
    return panneau;
  }

  arreter() {
    clearInterval(this.minuterie);
    this.dialogue?.close();
  }
}

class ModuleStereoRds {
  static nom = "Stéréo et RDS en FM";
  static description = "Séparation gauche/droite en WFM, nom de la station et radiotexte.";

  demarrer() {
    this.dernierBloc = -Infinity;
    this.frequence = this.ctx.frequence();
    this.mode = this.ctx.mode();
    this.ctx.abonner("vfo", (frequence, mode) => {
      if (frequence !== this.frequence || mode !== this.mode) this.decodeur = null;
      this.frequence = frequence;
      this.mode = mode;
    });
    this.ctx.abonner("fm", (multiplex, resultat) => {
      const maintenant = performance.now();
      if (!this.decodeur || maintenant - this.dernierBloc > 1000) this.decodeur = new StereoRds();
      this.dernierBloc = maintenant;
      resultat.stereo = this.decodeur.traiter(multiplex);
    });
  }

  creerPanneau() {
    const passerWfm = el("button", { type: "button", textContent: "Passer en WFM" });
    passerWfm.onclick = () => this.ctx.reglerMode("WFM");
    const voyant = el("span", { className: "voyant-stereo" });
    const statutAudio = el("span", { className: "rds-audio" }, voyant);
    const texteAudio = el("span", { textContent: "Mono" });
    statutAudio.append(texteAudio);
    const statutRds = el("span", { className: "petit" });
    const statuts = el("div", { className: "rds-statuts" }, statutAudio, statutRds);
    const station = el("div", { className: "rds-station" });
    const message = el("div", { className: "rds-message" });
    const actualiser = () => {
      const maintenant = performance.now();
      const enWfm = this.mode === "WFM";
      const actif = enWfm && this.decodeur && maintenant - this.dernierBloc < 1000;
      const stereo = Boolean(actif && this.decodeur.melange > 0.15);
      const rds = enWfm ? this.decodeur?.donnees : null;
      const recu = actif && maintenant - rds?.dernierGroupe < 5000;
      passerWfm.hidden = enWfm;
      voyant.classList.toggle("allume", stereo);
      texteAudio.textContent = stereo ? "Stéréo" : "Mono";
      statutRds.textContent = !enWfm ? "Activez le mode WFM" :
        !actif ? "Réception FM arrêtée" :
        recu ? "RDS reçu" : rds?.pi != null ? "RDS intermittent" : "En attente du RDS";
      station.textContent = rds?.nom || (rds?.pi == null ? "" :
        `PI ${rds.pi.toString(16).toUpperCase().padStart(4, "0")}`);
      message.textContent = rds?.message || "";
    };
    this.minuterie = setInterval(actualiser, 200);
    actualiser();
    return el("div", {}, passerWfm, statuts, station, message);
  }

  arreter() {
    clearInterval(this.minuterie);
  }
}

const FREQUENCE_ADSB = 1_090_000_000, TAUX_ADSB = 2_400_000;
// Niveau automatique ADS-B : le gain du tuner est ajusté pour garder le plancher de bruit entre
// BRUIT_MIN et BRUIT_MAX (dBFS) sans faire saturer le convertisseur 8 bits. Le gain automatique
// du tuner, réglé pour la FM, est trop bas pour les impulsions brèves de 1090 MHz.
const BRUIT_MIN = -36, BRUIT_MAX = -18, GAIN_DEPART = 40, STABILISATION_GAIN = 2000;   // dBFS, dB, ms

class ADSB {
  static nom = "ADS-B (avions)";
  static description = "Avions sur 1090 MHz : indicatif, altitude, vitesse, position sur une carte radar.";

  demarrer() {
    this.trafic = new Trafic();
    this.trafic.station = this.ctx.lire("station", null);
    this.derniersIQ = -Infinity; this.etat = "attente"; this.messages = 0; this.debit = 0;
    this.echantillons = 0; this.tauxRecu = 0;
    this.auto = this.ctx.lire("niveauAuto", true); this.prochainReglage = 0; this.etatNiveau = "";
    this.ctx.abonner("iq", (re, im, centre, taux) => {
      const maintenant = performance.now(), interrompu = maintenant - this.derniersIQ > 1000;
      this.derniersIQ = maintenant; this.echantillons += re.length; this.tauxRecu = taux;
      if (taux < TAUX_ADSB) { this.etat = "taux"; this.demod = null; return; }
      if (Math.abs(FREQUENCE_ADSB - centre) > 0.3 * taux) { this.etat = "hors"; this.demod = null; return; }
      this.etat = "ecoute";
      if (!this.demod || interrompu || taux !== this.demod.taux) {
        this.demod = new DemodModeS(taux, o => { this.trafic.recevoir(o); this.messages++; });
      }
      this.demod.traiter(re, im);
    });
  }

  creerPanneau() {
    const accorder = el("button", { type: "button", textContent: "Accorder sur 1090 MHz" });
    accorder.title = "Règle l'échantillonnage sur 2,4 MS/s, le gain et la fréquence sur 1090 MHz";
    accorder.onclick = async () => {
      if (this.ctx.taux() < TAUX_ADSB) await this.ctx.reglerTaux(TAUX_ADSB);
      const gains = this.ctx.gains();
      // Impulsions brèves : gain fort. Avec le niveau automatique, départ vers 40 dB, puis ajustement.
      if (gains.length) this.ctx.reglerGain(this.auto ? gains.reduce((m, x) => Math.abs(x - GAIN_DEPART) < Math.abs(m - GAIN_DEPART) ? x : m) : Math.max(...gains));
      this.prochainReglage = performance.now() + STABILISATION_GAIN;
      this.ctx.reglerMode("AM"); this.ctx.reglerFrequence(FREQUENCE_ADSB);
    };
    const caseAuto = el("input", { type: "checkbox", checked: this.auto });
    caseAuto.onchange = () => { this.auto = caseAuto.checked; this.ctx.ecrire("niveauAuto", this.auto); this.etatNiveau = ""; };
    const niveauAuto = el("label", { className: "petit adsb-auto",
      title: "Ajuste le gain RF pour 1090 MHz : baisse s'il y a saturation ou un bruit trop fort, monte si le signal est trop faible" },
      caseAuto, " Niveau automatique");
    this.texteNiveau = el("span", { className: "petit" });
    const ouvrir = el("button", { type: "button", textContent: "Ouvrir la carte", className: "adsb-ouvrir" });
    ouvrir.setAttribute("aria-haspopup", "dialog");
    this.statut = el("div", { className: "petit" });
    this.diag = el("div", { className: "petit adsb-diag", title: "Diagnostic de réception, par seconde" });
    this.liste = el("ul", { className: "journal adsb-mini" });

    // Fenêtre de la carte.
    const titre = el("h2", { id: "adsb-titre", textContent: "ADS-B · avions sur 1090 MHz" });
    titre.title = "Tirer la barre pour déplacer · coin en bas à droite pour agrandir";
    this.resume = el("span", { className: "adsb-resume" });
    const fermer = el("button", { type: "button", textContent: "Fermer" });
    const recentrer = el("button", { type: "button", textContent: "Recentrer" });
    this.toile = el("canvas", { className: "adsb-carte" });
    this.toile.setAttribute("aria-label", "Carte radar des avions reçus");
    this.tableau = el("tbody");
    const table = el("table", { className: "adsb-table" },
      el("thead", {}, el("tr", {}, ...["Vol", "Alt. (ft)", "Vit. (kt)", "Cap", "V/z", "Dist."].map(t => el("th", { textContent: t })))), this.tableau);
    const st = this.trafic.station;
    const lat = el("input", { type: "number", step: "0.0001", min: -90, max: 90, placeholder: "48.8566", value: st?.lat ?? "" });
    const lon = el("input", { type: "number", step: "0.0001", min: -180, max: 180, placeholder: "2.3522", value: st?.lon ?? "" });
    const placer = el("button", { type: "button", textContent: "Enregistrer la station" });
    placer.onclick = () => {
      const la = parseFloat(lat.value), lo = parseFloat(lon.value);
      this.trafic.station = Number.isFinite(la) && Number.isFinite(lo) ? { lat: la, lon: lo } : null;
      this.ctx.ecrire("station", this.trafic.station); this.carte.station = this.trafic.station; this.carte.centrer();
    };
    const pied = el("div", { className: "adsb-pied" },
      el("label", {}, "Station : lat. ", lat), el("label", {}, " lon. ", lon), placer,
      el("span", { className: "petit", textContent: "Facultatif : sert aux distances et aux cercles." }));
    this.dialogue = el("dialog", { className: "adsb-dialogue" },
      el("header", {}, titre, this.resume, el("span", { className: "adsb-boutons" }, recentrer, fermer)),
      el("div", { className: "adsb-corps" }, this.toile, el("div", { className: "adsb-cote" }, table)), pied);
    this.dialogue.setAttribute("aria-labelledby", titre.id);
    const afficher = fenetreFlottante(this.dialogue, this.dialogue.querySelector("header"), this.ctx, "carte");
    this.carte = new CarteRadar(this.toile, { surChoix: icao => { this.choisi = icao; this.majTableau(); } });
    this.carte.station = this.trafic.station;
    ouvrir.onclick = () => {
      if (this.dialogue.open) { fermer.focus(); return; }
      afficher(); this.carte.avions = [...this.trafic.avions.values()]; this.carte.centrer();
    };
    fermer.onclick = () => this.dialogue.close();
    recentrer.onclick = () => this.carte.centrer();
    this.dialogue.addEventListener("close", () => ouvrir.focus());

    let avant = 0, tAvant = performance.now(), dAvant = { ech: 0, pre: 0, df: 0, crc: 0 };
    this.minuterie = setInterval(() => {
      const maintenant = performance.now();
      if (maintenant - tAvant >= 1000) {
        const dt = (maintenant - tAvant) / 1000, d = this.demod;
        this.debit = (this.messages - avant) / dt; avant = this.messages; tAvant = maintenant; this.trafic.nettoyer();
        // Diagnostic : part des échantillons reçus, niveaux, préambules, Mode S courts, CRC faux (par seconde).
        const ech = this.echantillons, pre = d?.preambules || 0, df = d?.autresDF || 0, crc = d?.crcFaux || 0;
        const sat = d?.satures || 0, rec = d?.recus || 0;
        const partSat = rec > (dAvant.rec || 0) ? Math.max(0, sat - (dAvant.sat || 0)) / (rec - dAvant.rec) : 0;
        if (this.etat === "ecoute" && d) this.reglerNiveau(d, partSat);
        else this.etatNiveau = "";
        if (this.etat === "ecoute" && d) {
          const recus = Math.min(999, Math.round(100 * (ech - dAvant.ech) / dt / this.tauxRecu));
          const db = x => (20 * Math.log10(Math.max(x, 1e-6))).toFixed(0);
          this.diag.textContent = `Diagnostic : échantillons ${recus} % · bruit ${db(d.bruit)} dB · crête ${db(d.crete)} dB` +
            ` · préambules ${Math.max(0, Math.round((pre - dAvant.pre) / dt))}/s · DF11 ${Math.max(0, Math.round((df - dAvant.df) / dt))}/s` +
            ` · CRC faux ${Math.max(0, Math.round((crc - dAvant.crc) / dt))}/s · saturation ${(100 * partSat).toFixed(partSat < 0.01 ? 2 : 1)} %`;
        } else this.diag.textContent = "";
        dAvant = { ech, pre, df, crc, sat, rec };
        this.texteNiveau.textContent = this.etatNiveau;
      }
      if (maintenant - this.derniersIQ > 1000) this.etat = "attente";
      this.statut.textContent = {
        attente: "En attente des échantillons du récepteur.",
        taux: "Il faut 2,4 MS/s : cliquez sur « Accorder sur 1090 MHz ».",
        hors: "1090 MHz hors de la bande reçue : cliquez sur « Accorder ».",
        ecoute: `Écoute de 1090 MHz · ${this.trafic.avions.size} avion(s) · ${this.debit.toFixed(1)} msg/s`,
      }[this.etat];
      const avions = this.trier();
      this.liste.replaceChildren(...avions.slice(0, 6).map(a => el("li", { textContent: this.ligne(a) })));
      if (this.dialogue.open) {
        this.resume.textContent = `${avions.length} avion(s) · ${avions.filter(a => a.lat !== undefined).length} sur la carte · ${this.debit.toFixed(1)} msg/s`;
        this.carte.avions = avions; this.carte.dessiner(); this.majTableau();
      }
    }, 250);
    return el("div", {}, accorder, el("div", { className: "adsb-niveau" }, niveauAuto, this.texteNiveau),
      this.statut, this.diag, this.liste, ouvrir, this.dialogue);
  }

  /** Niveau automatique : un réglage au plus toutes les 2 s, le temps que bruit et crête se stabilisent. */
  reglerNiveau(d, partSat) {
    if (!this.auto) { this.etatNiveau = ""; return; }
    const gains = [...this.ctx.gains()].sort((a, b) => a - b);
    if (!gains.length) { this.etatNiveau = "gain non réglable sur cette source"; return; }
    const actuel = this.ctx.gain();
    let i = actuel === null ? gains.length - 1 : gains.findIndex(x => x >= actuel - 0.05);
    if (i < 0) i = gains.length - 1;
    const db = x => 20 * Math.log10(Math.max(x, 1e-6)), bruit = db(d.bruit), crete = db(d.crete);
    let pas = 0, raison = "réglé";
    if (partSat > 0.01) { pas = -3; raison = "saturation"; }
    else if (partSat > 0.001 || crete > -3) { pas = -1; raison = "saturation"; }
    else if (bruit > BRUIT_MAX + 6) { pas = -2; raison = "bruit trop fort"; }
    else if (bruit > BRUIT_MAX) { pas = -1; raison = "bruit trop fort"; }
    else if (bruit < BRUIT_MIN && crete < -10) { pas = bruit < BRUIT_MIN - 8 ? 2 : 1; raison = "signal faible"; }
    const j = Math.max(0, Math.min(gains.length - 1, i + pas));
    const maintenant = performance.now();
    if (j !== i && actuel !== null && maintenant < this.prochainReglage) raison = "stabilisation";
    else if (j !== i || actuel === null) {
      this.ctx.reglerGain(gains[j]); this.prochainReglage = maintenant + STABILISATION_GAIN;
      i = j;
    } else if (pas) raison += pas < 0 ? " (gain au minimum)" : " (gain au maximum)";
    this.etatNiveau = `gain ${gains[i].toFixed(1)} dB · ${raison}`;
  }

  trier() {   // ordre stable : par distance si la station est connue, sinon par indicatif
    const st = this.trafic.station, cle = a => st && a.lat !== undefined ? distanceKm(st.lat, st.lon, a.lat, a.lon) : Infinity;
    return [...this.trafic.avions.values()].sort((a, b) => cle(a) - cle(b) || (a.indicatif || "~" + a.icao).localeCompare(b.indicatif || "~" + b.icao));
  }
  distance(a) {
    const st = this.trafic.station;
    return st && a.lat !== undefined ? `${Math.round(distanceKm(st.lat, st.lon, a.lat, a.lon))} km` : "";
  }
  ligne(a) {
    return `${(a.indicatif || a.icao).padEnd(8)} ${a.altitude !== undefined ? `${a.altitude} ft` : "—"}${a.vitesse !== undefined ? ` · ${a.vitesse} kt` : ""}`;
  }
  majTableau() {
    this.tableau.replaceChildren(...this.trier().map(a => {
      const tr = el("tr", { className: a.icao === this.choisi ? "choisi" : "" },
        el("td", {}, el("i", { className: "adsb-pastille" }), a.indicatif || "", el("small", { textContent: ` ${a.icao}` })),
        el("td", { textContent: a.altitude ?? "—" }), el("td", { textContent: a.vitesse ?? "—" }),
        el("td", { textContent: a.cap !== undefined ? `${a.cap}°` : "—" }),
        el("td", { textContent: a.vz !== undefined ? (a.vz > 0 ? `+${a.vz}` : `${a.vz}`) : "—" }),
        el("td", { textContent: this.distance(a) || (a.lat === undefined ? "sans pos." : "") }));
      tr.firstChild.firstChild.style.background = couleurAltitude(a.altitude);
      tr.onclick = () => {
        this.choisi = this.carte.selection = a.icao;
        if (a.lat !== undefined) { this.carte.lat0 = a.lat; this.carte.lon0 = a.lon; }
        this.carte.dessiner(); this.majTableau();
      };
      return tr;
    }));
  }

  arreter() { clearInterval(this.minuterie); if (this.dialogue?.open) this.dialogue.close(); }
}

// Scanner : relit la liste du module Mémoires, s'arrête sur un signal, reprend après un silence.
const HYSTERESIS = 3, STABILISATION = 150;   // dB ; ms ignorées après chaque changement de fréquence

class ScannerMemoires {
  static nom = "Scanner de mémoires";
  static description = "Parcourt les mémoires, s'arrête sur un signal et reprend quand il se tait.";

  demarrer() {
    this.seuil = this.ctx.lire("seuil", -60); this.duree = this.ctx.lire("duree", 0.35); this.reprise = this.ctx.lire("reprise", 3);
    this.exclues = new Set(this.ctx.lire("exclues", []));
    this.boucle = this.ctx.lire("boucle", true); this.tours = 0; this.fini = false;
    this.niveaux = []; this.actif = false; this.etat = "arret"; this.indice = -1; this.journal = [];
    this.charger();
    this.ctx.abonner("niveau", db => { this.niveaux.push([performance.now(), db]); if (this.niveaux.length > 200) this.niveaux.splice(0, 100); });
  }
  charger() {
    let liste;
    try { liste = JSON.parse(localStorage.getItem("sdr-web/Mémoires/liste")); } catch { liste = null; }
    this.memoires = [...(Array.isArray(liste) ? liste : MEMOIRES_DEFAUT)].sort((a, b) => a.f - b.f);
  }
  cle(m) { return `${m.f}/${m.mode}`; }
  canaux() { return this.memoires.map((m, i) => i).filter(i => !this.exclues.has(this.cle(this.memoires[i]))); }

  creerPanneau() {
    this.bMarche = el("button", { type: "button" }); this.bMarche.onclick = () => this.basculer();
    const passer = el("button", { type: "button", textContent: "Passer", title: "Quitter le canal actuel et continuer" }); passer.onclick = () => this.actif && this.suivant();
    const relire = el("button", { type: "button", textContent: "Relire", title: "Relire la liste du module Mémoires" });
    relire.onclick = () => { this.charger(); this.indice = -1; this.remplir(); };
    this.statut = el("div", { className: "petit scanner-statut" });
    const curseur = (titre, cle, min, max, pas, texte) => {
      const val = el("span", { className: "petit", textContent: texte(this[cle]) });
      const c = el("input", { type: "range", min, max, step: pas, value: this[cle] });
      c.oninput = () => { this[cle] = Number(c.value); val.textContent = texte(this[cle]); this.ctx.ecrire(cle, this[cle]); };
      return el("label", { className: "scanner-reglage" }, el("span", { textContent: titre }), c, val);
    };
    this.ul = el("ul", { className: "journal scanner-liste" });
    this.ulJournal = el("ul", { className: "journal" });
    const boucle = el("input", { type: "checkbox", checked: this.boucle });
    boucle.onchange = () => { this.boucle = boucle.checked; this.ctx.ecrire("boucle", this.boucle); };
    this.caseBoucle = el("label", { className: "petit scanner-boucle", title: "Reprendre au début après la dernière mémoire ; sinon un seul passage" }, boucle, " En boucle");
    this.remplir();
    this.minuterie = setInterval(() => this.pas(), 50);
    this.afficher();
    return el("div", {},
      el("div", { className: "scanner-boutons" }, this.bMarche, passer, relire), this.caseBoucle, this.statut,
      curseur("Seuil", "seuil", -110, -20, 1, v => `${v} dBFS`),
      curseur("Par canal", "duree", 0.2, 2, 0.05, v => `${v.toFixed(2)} s`),
      curseur("Reprise", "reprise", 1, 15, 1, v => `${v} s`),
      this.ul, el("div", { className: "petit", textContent: "Derniers arrêts :" }), this.ulJournal);
  }
  remplir() {
    this.ul.replaceChildren(...this.memoires.map((m, i) => {
      const c = el("input", { type: "checkbox", checked: !this.exclues.has(this.cle(m)), title: "Décocher pour sauter cette mémoire" });
      c.onchange = () => { c.checked ? this.exclues.delete(this.cle(m)) : this.exclues.add(this.cle(m)); this.ctx.ecrire("exclues", [...this.exclues]); };
      const li = el("li", {}, el("label", {}, c, ` ${this.numero(i)} ${(m.f / 1e6).toFixed(4)} ${m.mode} · ${m.nom}`)); li.dataset.i = i;
      return li;
    }));
  }
  basculer() {
    if (this.actif) { this.actif = false; this.etat = "arret"; }
    else if (!this.canaux().length) { this.statut.textContent = "Aucune mémoire à scanner : cochez-en au moins une."; return; }
    else { this.actif = true; this.tours = 1; this.fini = false; this.indice = -1; this.suivant(); }
    this.afficher();
  }
  suivant() {
    const canaux = this.canaux();
    if (!canaux.length) { this.actif = false; this.etat = "arret"; return; }
    const apres = canaux.find(i => i > this.indice);
    if (apres === undefined && this.indice >= 0) {   // fin de la liste
      if (!this.boucle) { this.actif = false; this.etat = "arret"; this.fini = true; this.afficher(); return; }
      this.tours++;
    }
    this.indice = apres ?? canaux[0];
    const m = this.memoires[this.indice];
    this.ctx.reglerMode(m.mode); this.ctx.reglerFrequence(m.f);
    this.fAttendue = this.ctx.frequence(); this.tAccord = performance.now(); this.tSilence = null; this.etat = "mesure";
    this.afficher();
  }
  niveauDepuis(t) { let m = null; for (const [ti, db] of this.niveaux) if (ti >= t) m = m === null ? db : Math.max(m, db); return m; }
  pas() {
    if (!this.actif) return;
    const maintenant = performance.now();
    if (Math.abs(this.ctx.frequence() - this.fAttendue) > 1) {   // fréquence changée à la main
      this.actif = false; this.etat = "arret"; this.afficher(); this.statut.textContent = "Scanner arrêté : fréquence changée à la main."; return;
    }
    if (this.etat === "mesure") {
      if (maintenant - this.tAccord < STABILISATION + this.duree * 1000) return;
      const db = this.niveauDepuis(this.tAccord + STABILISATION);
      if (db !== null && db > this.seuil) {
        this.etat = "signal";
        const m = this.memoires[this.indice];
        this.journal.unshift(`${new Date().toLocaleTimeString("fr-FR")}  ${(m.f / 1e6).toFixed(4)}  ${m.nom}  ${db.toFixed(0)} dBFS`);
        this.journal.length = Math.min(this.journal.length, 20);
        this.ulJournal.replaceChildren(...this.journal.map(t => el("li", { textContent: t })));
      } else this.suivant();
    } else if (this.etat === "signal") {
      const db = this.niveauDepuis(maintenant - 300);
      if (db === null) { /* pas de mesure récente : on ne conclut rien */ }
      else if (db > this.seuil - HYSTERESIS) this.tSilence = null;
      else if (this.tSilence === null) this.tSilence = maintenant;
      else if (maintenant - this.tSilence >= this.reprise * 1000) this.suivant();
    }
    this.afficher();
  }
  numero(i) { const n = this.memoires.length; return `CH ${String(i + 1).padStart(String(n).length, "0")}`; }
  canal(texte) {   // à côté de la fréquence, dans la barre du haut
    if (texte !== this.canalAffiche) { this.canalAffiche = texte; this.ctx.afficherCanal?.(texte); }
  }
  afficher() {
    const courant = this.memoires[this.indice];
    this.canal(this.actif && courant ? `${this.numero(this.indice)}/${this.memoires.length} · ${courant.nom}` : null);
    this.bMarche.textContent = this.actif ? "■ Arrêter" : "▶ Scanner";
    for (const li of this.ul.children) li.classList.toggle("courant", this.actif && Number(li.dataset.i) === this.indice);
    if (!this.actif) { this.statut.textContent = `${this.fini ? "Passage terminé" : "Arrêté"} · ${this.canaux().length} mémoire(s) à scanner`; return; }
    const m = this.memoires[this.indice], t = `${(m.f / 1e6).toFixed(4)} MHz ${m.mode} · ${m.nom}`;
    this.statut.textContent = this.etat === "signal"
      ? `● Signal sur ${t}${this.tSilence === null ? "" : ` · reprise dans ${Math.max(0, this.reprise - (performance.now() - this.tSilence) / 1000).toFixed(0)} s`}`
      : `Balayage… ${t} · tour ${this.tours}`;
  }
  arreter() { clearInterval(this.minuterie); this.actif = false; this.canal(null); }
}

// Scanner de fréquences : plage début–fin avec un pas, arrêt sur signal, reprise après un silence.
const PAS_SCAN = [5, 6.25, 8.33, 10, 12.5, 20, 25, 50, 100, 200], MODES_SCAN = ["NFM", "AM", "WFM", "USB", "LSB", "CW"];

/**
 * Trouve une station dans le spectre d'un bloc IQ : ses deux bords, autour de f0 (là où le
 * scanner l'a touchée) et dans une fenêtre de quelques largeurs de canal. null si rien de net.
 */
function bordsStation(re, im, centre, taux, f0, bw) {
  const N = 2048, fft = new FFT(N), trames = Math.min(8, Math.floor(re.length / N));
  if (!trames) return null;
  const p = new Float64Array(N);
  for (let t = 0; t < trames; t++) { const db = fft.spectre(re, im, t * N); for (let i = 0; i < N; i++) p[i] += 10 ** (db[i] / 10); }
  const res = taux / N, w = Math.max(1, Math.round(bw / 20 / res)), lisse = new Float64Array(N);
  for (let i = 0; i < N; i++) { let s = 0, c = 0; for (let k = Math.max(0, i - w); k <= Math.min(N - 1, i + w); k++) { s += p[k]; c++; } lisse[i] = 10 * Math.log10(s / c / trames + 1e-20); }
  const bruit = [...lisse].sort((a, b) => a - b)[N >> 1];
  const bin = f => Math.round((f - centre) / res + N / 2), freq = i => centre + (i - N / 2) * res;
  const a = Math.max(2, bin(f0 - bw)), b = Math.min(N - 3, bin(f0 + 2.5 * bw));
  let pic = -1; for (let i = a; i <= b; i++) if (pic < 0 || lisse[i] > lisse[pic]) pic = i;
  if (pic < 0 || lisse[pic] < bruit + 6) return null;
  const seuil = Math.max(bruit + 6, lisse[pic] - 30);
  let g = pic, d = pic;
  while (g > 2 && lisse[g - 1] > seuil) g--;
  while (d < N - 3 && lisse[d + 1] > seuil) d++;
  return { gauche: freq(g), droite: freq(d) };
}

class ScannerFrequences {
  static nom = "Scanner de fréquences";
  static description = "Balaye une plage début–fin avec un pas, s'arrête sur un signal qui dépasse le bruit et reprend après un silence.";

  demarrer() {
    const l = (c, d) => this.ctx.lire(c, d);
    Object.assign(this, { debut: l("debut", 144), fin: l("fin", 146), pas: l("pas", 12.5), modeScan: l("mode", "NFM"),
      marge: l("marge", 10), duree: l("duree", 0.15), reprise: l("reprise", 3) });
    this.mesures = [];   // niveaux des derniers pas : leur médiane estime le bruit
    this.ignorees = new Set(l("ignorees", []));
    this.boucle = l("boucle", true); this.tours = 0; this.fini = false;
    this.niveaux = []; this.actif = false; this.etat = "arret"; this.k = -1; this.f = null; this.journal = [];
    if (MODES_SCAN.includes(this.ctx.mode())) this.modeScan = this.ctx.mode();   // on part du mode du récepteur
    this.ctx.abonner("niveau", db => { this.niveaux.push([performance.now(), db]); if (this.niveaux.length > 200) this.niveaux.splice(0, 100); });
    this.ctx.abonner("iq", (re, im, centre, taux) => {   // un bloc suffit, et seulement pendant le centrage
      if (this.etat === "centrage" && !this.blocIQ && performance.now() > this.tAccord + STABILISATION)
        this.blocIQ = { re: re.slice(), im: im.slice(), centre, taux };
    });
    this.ctx.abonner("vfo", (_f, mode) => {   // mode changé dans la barre du haut : le scanner suit
      if (mode === this.modeScan || !MODES_SCAN.includes(mode)) return;
      this.modeScan = mode; if (this.choixMode) this.choixMode.value = mode;
      this.sauver(); if (this.bMarche) this.afficher();
    });
  }
  sauver() { for (const c of ["debut", "fin", "pas", "marge", "duree", "reprise"]) this.ctx.ecrire(c, this[c]); this.ctx.ecrire("mode", this.modeScan); this.ctx.ecrire("ignorees", [...this.ignorees]); this.ctx.ecrire("boucle", this.boucle); }
  plage() {
    const bas = Math.min(this.debut, this.fin), haut = Math.max(this.debut, this.fin);
    return [Math.floor(((haut - bas) * 1e6) / (this.pas * 1e3) + 1e-9) + 1, k => Math.round(bas * 1e6 + k * this.pas * 1e3)];
  }

  creerPanneau() {
    const nombre = (cle, titre) => {
      const e = el("input", { type: "number", min: 0, max: 6000, step: 0.0125, value: this[cle] });
      e.onchange = () => { this[cle] = Number(e.value) || 0; this.sauver(); this.afficher(); };
      return el("label", { className: "scanner-reglage" }, el("span", { textContent: titre }), e, el("span", { className: "petit", textContent: "MHz" }));
    };
    const pas = el("select", {}, ...PAS_SCAN.map(p => new Option(`${p} kHz`, p, false, p === this.pas)));
    pas.onchange = () => { this.pas = Number(pas.value); this.sauver(); this.afficher(); };
    const mode = this.choixMode = el("select", {}, ...MODES_SCAN.map(m => new Option(m, m, false, m === this.modeScan)));
    mode.onchange = () => {   // mode changé dans le scanner : le récepteur suit
      this.modeScan = mode.value; if (this.ctx.mode() !== this.modeScan) this.ctx.reglerMode(this.modeScan);
      this.sauver(); this.afficher();
    };
    const curseur = (titre, cle, min, max, p, texte) => {
      const val = el("span", { className: "petit", textContent: texte(this[cle]) });
      const c = el("input", { type: "range", min, max, step: p, value: this[cle] });
      c.oninput = () => { this[cle] = Number(c.value); val.textContent = texte(this[cle]); this.sauver(); };
      return el("label", { className: "scanner-reglage" }, el("span", { textContent: titre }), c, val);
    };
    this.bMarche = el("button", { type: "button" }); this.bMarche.onclick = () => this.basculer();
    const passer = el("button", { type: "button", textContent: "Passer" }); passer.onclick = () => this.actif && this.suivant();
    const ignorer = el("button", { type: "button", textContent: "Ignorer", title: "Ne plus s'arrêter sur cette fréquence" });
    ignorer.onclick = () => { if (this.actif && this.f !== null) { this.ignorees.add(this.f); this.sauver(); this.suivant(); } };
    const boucle = el("input", { type: "checkbox", checked: this.boucle });
    boucle.onchange = () => { this.boucle = boucle.checked; this.sauver(); };
    const caseBoucle = el("label", { className: "petit scanner-boucle", title: "Reprendre au début après la fin de la plage ; sinon un seul passage" }, boucle, " En boucle");
    this.barre = el("progress", { className: "scanner-progression", max: 1, value: 0 });
    this.statut = el("div", { className: "petit scanner-statut" });
    this.txtIgnorees = el("div", { className: "petit" });
    const oublier = el("button", { type: "button", textContent: "Oublier les fréquences ignorées" });
    oublier.onclick = () => { this.ignorees.clear(); this.sauver(); this.afficher(); };
    this.ulJournal = el("ul", { className: "journal" });
    this.minuterie = setInterval(() => this.tic(), 30);
    this.afficher();
    return el("div", {}, nombre("debut", "Début"), nombre("fin", "Fin"),
      el("label", { className: "scanner-reglage" }, el("span", { textContent: "Pas, mode" }), el("span", { className: "scanner-choix" }, pas, mode), el("span")),
      curseur("Marge", "marge", 3, 30, 1, v => `+${v} dB`), curseur("Par pas", "duree", 0.05, 1, 0.01, v => `${v.toFixed(2)} s`),
      curseur("Reprise", "reprise", 1, 15, 1, v => `${v} s`),
      el("div", { className: "scanner-boutons" }, this.bMarche, passer, ignorer), caseBoucle, this.barre, this.statut,
      this.txtIgnorees, oublier, el("div", { className: "petit", textContent: "Derniers arrêts :" }), this.ulJournal);
  }
  basculer() {
    if (this.actif) { this.actif = false; this.etat = "arret"; this.afficher(); return; }
    const [n] = this.plage();
    if (this.debut === this.fin || n > 200000) { this.statut.textContent = "Plage invalide : début et fin différents, 200 000 pas au plus."; return; }
    this.actif = true; this.mesures = []; this.tours = 1; this.fini = false; this.kSortie = null; this.ctx.reglerMode(this.modeScan); this.k = -1; this.suivant();
  }
  largeurHz() { return LARGEURS[this.modeScan] || 12500; }
  estIgnoree(f) {   // une fréquence ignorée écarte toute la largeur du canal autour d'elle
    const demi = Math.max(this.largeurHz() / 2, this.pas * 500);
    for (const g of this.ignorees) if (Math.abs(f - g) < demi) return true;
    return false;
  }
  suivant() {
    const [n, fDe] = this.plage();
    if (this.kSortie != null) { this.k = Math.min(this.kSortie, n) - 1; this.kSortie = null; }
    let trouve = false;
    for (let i = 0; i < n; i++) {
      if (this.k + 1 >= n) {   // fin de la plage
        if (!this.boucle) { this.actif = false; this.etat = "arret"; this.fini = true; this.afficher(); return; }
        this.tours++;
      }
      this.k = (this.k + 1) % n; if (!this.estIgnoree(fDe(this.k))) { trouve = true; break; }
    }
    if (!trouve) { this.actif = false; this.etat = "arret"; this.afficher(); return; }
    this.f = fDe(this.k); this.ctx.reglerFrequence(this.f);
    this.fAttendue = this.ctx.frequence(); this.tAccord = performance.now(); this.tSilence = null; this.etat = "mesure";
    this.afficher();
  }
  arreterSur(k, db) {
    const [, fDe] = this.plage();
    if (k !== this.k) { this.k = k; this.f = fDe(k); this.ctx.reglerFrequence(this.f); this.fAttendue = this.ctx.frequence(); }
    this.etat = "signal"; this.tSilence = null; this.tAccord = performance.now();
    this.journal.unshift(`${new Date().toLocaleTimeString("fr-FR")}  ${(this.f / 1e6).toFixed(4)} MHz  ${db.toFixed(0)} dBFS`);
    this.journal.length = Math.min(this.journal.length, 30);
    this.ulJournal.replaceChildren(...this.journal.map(t => el("li", { textContent: t })));
    this.afficher();
  }
  bruit() {   // médiane des derniers pas (presque tous vides) ; rien tant qu'il y en a moins de 5
    if (this.mesures.length < 5) return null;
    const t = [...this.mesures].sort((x, y) => x - y); return t[t.length >> 1];
  }
  niveauDepuis(t) { let m = null; for (const [ti, db] of this.niveaux) if (ti >= t) m = m === null ? db : Math.max(m, db); return m; }
  tic() {
    if (!this.actif) return;
    const maintenant = performance.now();
    if (Math.abs(this.ctx.frequence() - this.fAttendue) > 1) {
      this.actif = false; this.etat = "arret"; this.afficher(); this.statut.textContent = "Scanner arrêté : fréquence changée à la main."; return;
    }
    if (this.etat === "mesure") {
      if (maintenant - this.tAccord < STABILISATION + this.duree * 1000) return;
      const db = this.niveauDepuis(this.tAccord + STABILISATION);
      if (db === null) return;
      const bruit = this.bruit();
      this.mesures.push(db); if (this.mesures.length > 60) this.mesures.shift();
      if (bruit !== null && db > bruit + this.marge) {
        const pasHz = this.pas * 1e3, bw = this.largeurHz();
        if (bw > 2 * pasHz) {
          // Canal plus large que le pas (WFM…) : on vient de toucher le flanc de la station.
          // On lit le spectre pour trouver ses deux bords et se caler au milieu.
          this.etat = "centrage"; this.blocIQ = null; this.dbDetection = db; this.tCentrage = maintenant; this.afficher();
        } else this.arreterSur(this.k, db);
      } else { this.suivant(); return; }
    } else if (this.etat === "centrage") {
      if (!this.blocIQ) { if (maintenant - this.tCentrage > 1000) this.arreterSur(this.k, this.dbDetection); return; }
      const { re, im, centre, taux } = this.blocIQ, bw = this.largeurHz(), pasHz = this.pas * 1e3;
      const bords = bordsStation(re, im, centre, taux, this.f, bw);
      if (!bords) { this.arreterSur(this.k, this.dbDetection); return; }
      const [n] = this.plage(), bas = Math.min(this.debut, this.fin) * 1e6;
      const kc = Math.max(0, Math.min(n - 1, Math.round(((bords.gauche + bords.droite) / 2 - bas) / pasHz)));
      // À la reprise : premier pas dont le canal ne touche plus la station.
      this.kSortie = Math.max(kc + 1, Math.ceil((bords.droite + bw / 2 - bas) / pasHz));
      this.arreterSur(kc, this.dbDetection);
    } else if (this.etat === "signal") {
      const db = this.niveauDepuis(maintenant - 300);
      if (db === null) { /* pas de mesure récente */ }
      else if (db > (this.bruit() ?? -200) + this.marge - HYSTERESIS) this.tSilence = null;
      else if (this.tSilence === null) this.tSilence = maintenant;
      else if (maintenant - this.tSilence >= this.reprise * 1000) { this.suivant(); return; }
    }
    this.afficher();
  }
  afficher() {
    const [n] = this.plage();
    this.bMarche.textContent = this.actif ? "■ Arrêter" : "▶ Scanner";
    this.barre.max = Math.max(1, n - 1); this.barre.value = this.actif ? Math.max(0, this.k) : 0;
    const ig = [...this.ignorees].sort((a, b) => a - b);
    this.txtIgnorees.textContent = ig.length ? `Ignorées : ${ig.slice(0, 12).map(f => (f / 1e6).toFixed(4)).join(", ")}${ig.length > 12 ? " …" : ""}` : "Aucune fréquence ignorée.";
    if (!this.actif) {
      const bas = Math.min(this.debut, this.fin), haut = Math.max(this.debut, this.fin);
      this.statut.textContent = `${this.fini ? "Passage terminé" : "Arrêté"} · ${bas.toFixed(4)} → ${haut.toFixed(4)} MHz, ${n} pas de ${this.pas} kHz en ${this.modeScan}`;
    } else if (this.etat === "signal") {
      this.statut.textContent = `● Signal sur ${(this.f / 1e6).toFixed(4)} MHz ${this.modeScan}${this.tSilence === null ? "" : ` · reprise dans ${Math.max(0, this.reprise - (performance.now() - this.tSilence) / 1000).toFixed(0)} s`}`;
    } else if (this.etat === "centrage") this.statut.textContent = `Centrage sur la station… ${(this.f / 1e6).toFixed(4)} MHz`;
    else this.statut.textContent = `Balayage… ${(this.f / 1e6).toFixed(4)} MHz (${this.k + 1}/${n}, tour ${this.tours})${this.bruit() === null ? " · mesure du bruit" : ` · bruit ${this.bruit().toFixed(0)} dBFS`}`;
  }
  arreter() { clearInterval(this.minuterie); this.actif = false; }
}

// Fréquences SSTV préréglées : [Hz, mode, libellé] (ondes courtes reprises d'IC-7300 Scope).
const FREQUENCES_SSTV = [
  [145_800_000, "NFM", "ISS (ARISS), PD120"], [144_500_000, "NFM", "2 m, appel SSTV (IARU Région 1)"],
  [50_510_000, "USB", "6 m"], [28_680_000, "USB", "10 m"], [21_340_000, "USB", "15 m"],
  [14_230_000, "USB", "20 m, la plus active"], [14_233_000, "USB", "20 m"],
  [7_165_000, "LSB", "40 m"], [7_171_000, "LSB", "40 m"], [3_735_000, "LSB", "80 m"],
];

class ModuleSSTV {
  static nom = "Décodeur SSTV";
  static description = "Images SSTV (ISS en PD120, Martin, Scottie, Robot, PD) décodées depuis l'audio, téléchargeables en PNG.";

  demarrer() {
    this.decodeur = null; this.blocs = []; this.perdus = 0; this.ordres = [];
    // Le rappel du bus ne fait que stocker : le décodage se fait par lots, toutes les 100 ms.
    this.ctx.abonner("audio", (a, taux) => {
      if (!this.decodeur) this.decodeur = new DecodeurSSTV(taux);
      if (this.blocs.length < 200) this.blocs.push(Float32Array.from(a)); else this.perdus++;
    });
    this.travail = setInterval(() => {
      const d = this.decodeur; if (!d) return;
      for (const o of this.ordres.splice(0)) o[0] === "demarrer" ? d.demarrer(MODES_SSTV.find(m => m.nom === o[1])) : d.arreter("Réception arrêtée.");
      for (const b of this.blocs.splice(0)) d.traiter(b);
    }, 100);
  }

  creerPanneau() {
    this.canevas = el("canvas", { width: 320, height: 256, className: "sstv-image", title: "Image SSTV" });
    this.canevas.getContext("2d").fillRect(0, 0, 320, 256);
    this.statut = el("div", { className: "petit sstv-statut" });
    this.tonalite = el("div", { className: "petit" });
    this.choix = el("select", { title: "Mode à forcer pour un démarrage à la main (en-tête VIS manqué)" },
      el("option", { value: "", textContent: "Automatique (VIS)" }),
      ...MODES_SSTV.map(m => el("option", { value: m.nom, textContent: m.nom })));
    this.bDemarrer = el("button", { type: "button", textContent: "Démarrer", disabled: true, title: "Commence l'image maintenant dans le mode choisi" });
    this.bArreter = el("button", { type: "button", textContent: "Arrêter", disabled: true });
    this.choix.onchange = () => { this.bDemarrer.disabled = !this.choix.value; };
    this.bDemarrer.onclick = () => { if (this.choix.value) this.ordres.push(["demarrer", this.choix.value]); };
    this.bArreter.onclick = () => this.ordres.push(["arreter"]);
    this.auto = el("input", { type: "checkbox", checked: this.ctx.lire("auto", false) });
    this.auto.onchange = () => this.ctx.ecrire("auto", this.auto.checked);
    const telecharger = el("button", { type: "button", textContent: "Télécharger l'image" });
    telecharger.onclick = () => { const d = this.decodeur; if (d?.image) this.telecharger(d.image, d.mode.largeur, d.mode.hauteur, d.mode.nom, new Date()); };
    const frequence = el("select", { title: "Accorde le récepteur sur une fréquence SSTV, dans le bon mode (ondes courtes : clé qui reçoit la HF ou convertisseur)" },
      el("option", { value: "", textContent: "Prérégler une fréquence…" }),
      ...FREQUENCES_SSTV.map(([f, mode, nom], i) => el("option", { value: i, textContent: `${(f / 1e6).toFixed(3).replace(".", ",")} MHz ${mode} · ${nom}` })));
    const avertissement = el("div", { className: "petit", hidden: true });
    avertissement.style.color = "var(--orange)";
    frequence.onchange = () => {
      if (frequence.value === "") return;
      const [f, mode] = FREQUENCES_SSTV[Number(frequence.value)];
      const avantF = this.ctx.frequence(), avantMode = this.ctx.mode();
      this.ctx.reglerMode(mode); this.ctx.reglerFrequence(f);
      avertissement.hidden = Math.abs(this.ctx.frequence() - f) <= 1;
      if (!avertissement.hidden) {      // hors de la plage de la clé : retour où l'on était, et pourquoi
        this.ctx.reglerMode(avantMode); this.ctx.reglerFrequence(avantF); frequence.value = "";
        avertissement.textContent = `${(f / 1e6).toFixed(3).replace(".", ",")} MHz est hors de la plage de la clé : réception impossible. Ondes courtes : convertisseur HF (réglage « Convert. »), clé RTL-SDR Blog V4 ou échantillonnage direct (menu Entrée).`;
      }
    };
    const actions = el("div", { className: "scanner-choix sstv-actions" }, telecharger,
      el("label", { className: "petit sstv-auto" }, this.auto, " Télécharger chaque image"));
    const contenu = el("div", { className: "cw-contenu sstv-contenu" }, el("div", { className: "scanner-choix" }, frequence), avertissement, this.canevas,
      this.statut, this.tonalite, el("div", { className: "scanner-choix" }, this.choix, this.bDemarrer, this.bArreter), actions);
    const { panneau, dialogue } = panneauAgrandissable(this.ctx, { titre: ModuleSSTV.nom, id: "sstv-titre", contenu, actions,
      tailleDefaut: 1.0, attenteTexte: "Image affichée dans la fenêtre agrandie.", tailleTexte: false });
    this.dialogue = dialogue;
    this.version = -1;
    this.minuterie = setInterval(() => this.rafraichir(), 250);
    this.rafraichir();
    return panneau;
  }

  rafraichir() {
    const d = this.decodeur;
    if (!d) { this.statut.textContent = "Démarrez la réception : le décodeur écoute l'audio du récepteur."; return; }
    if (d.image && d.version !== this.version) {
      const { largeur, hauteur } = d.mode;
      if (this.canevas.width !== largeur || this.canevas.height !== hauteur) { this.canevas.width = largeur; this.canevas.height = hauteur; }
      this.canevas.getContext("2d").putImageData(new ImageData(d.image, largeur, hauteur), 0, 0);
      this.version = d.version;
    }
    for (const t of d.terminees.splice(0)) if (this.auto.checked) this.telecharger(t.image, t.largeur, t.hauteur, t.mode, t.date, t.partielle);
    this.statut.textContent = d.message + (this.perdus ? ` (${this.perdus} blocs audio perdus)` : "");
    this.tonalite.textContent = d.tonalite ? `Tonalité : ${Math.round(d.tonalite).toLocaleString("fr-FR")} Hz` : "";
    this.bArreter.disabled = d.etat !== "image";
  }

  telecharger(image, largeur, hauteur, mode, date, partielle = false) {
    const c = el("canvas", { width: largeur, height: hauteur });
    c.getContext("2d").putImageData(new ImageData(image.slice(), largeur, hauteur), 0, 0);
    const h = new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 19).replace(/[-:]/g, "").replace("T", "_");
    c.toBlob(blob => {
      const a = el("a", { href: URL.createObjectURL(blob), download: `sstv_${h}_${mode.replace(/ /g, "")}${partielle ? "_partielle" : ""}.png` });
      a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    }, "image/png");
  }

  arreter() { clearInterval(this.minuterie); clearInterval(this.travail); if (this.dialogue?.open) this.dialogue.close(); }
}

// Décodeur CW : même réseau de neurones que le module decodeur_cw.py de SDR Modulaire (cw.js).
// L'audio (48 kHz, après AGC et squelch) est filtré et ramené à 8 kHz ; toutes les 3 s, la fenêtre
// des 6 dernières secondes part au fil de calcul (cw-worker.js), qui renvoie le texte du milieu.
class DecodeurCW {
  static nom = "Décodeur CW";
  static description = "Morse décodé par un réseau de neurones, 10 à 42 WPM, robuste au bruit et au QSB. Mode CW, squelch ouvert.";

  demarrer() {
    this.fenetre = new Float32Array(DUREE_FENETRE * TAUX_CW); this.rempli = 0; this.depuis = 0;
    this.premiere = true; this.occupe = false; this.recus = [];
    this.etat = { pret: false, erreur: "", silence: false, tonalite: null, wpm: null, contraste: null, duree: 0 };
    this.taux = 0;
    this.fil = new Worker(new URL("./cw-worker.js?v=20261122-cw", import.meta.url), { type: "module" });
    this.fil.onmessage = e => {
      const r = e.data;
      if (r.pret) { this.etat.pret = true; return; }
      this.occupe = false;
      if (r.erreur) { this.etat.erreur = r.erreur; return; }
      Object.assign(this.etat, { silence: r.silence, duree: r.duree ?? this.etat.duree });
      if (!r.silence) {
        Object.assign(this.etat, { tonalite: r.tonalite, wpm: r.wpm, contraste: r.contraste });
        if (r.texte) this.recus.push(r.texte);
      }
    };
    this.fil.onerror = e => { this.etat.erreur = e.message || "le fil de calcul n'a pas démarré"; };
    this.ctx.abonner("audio", (a, taux) => this.recevoir(a, taux));
  }

  /** Filtre passe-bas (3,4 kHz) et décimation vers 8 kHz, en ne calculant que les échantillons gardés. */
  recevoir(a, taux) {
    if (taux !== this.taux) {
      this.taux = taux; this.facteur = Math.max(1, Math.round(taux / TAUX_CW));
      this.h = passeBas(taux, 3400, 127); this.histo = new Float32Array(this.h.length - 1); this.phase = 0;
    }
    const h = this.h, L = h.length, f = this.facteur, y = new Float32Array(L - 1 + a.length);
    y.set(this.histo); y.set(a, L - 1);
    const sortie = [];
    let j = L - 1 + this.phase;
    for (; j < y.length; j += f) { let s = 0; for (let q = 0; q < L; q++) s += h[q] * y[j - q]; sortie.push(s); }
    this.phase = j - y.length;
    this.histo = y.slice(y.length - (L - 1));
    // Fenêtre glissante des 6 dernières secondes.
    const n = sortie.length, W = this.fenetre;
    if (n >= W.length) W.set(sortie.slice(n - W.length));
    else { W.copyWithin(0, n); W.set(sortie, W.length - n); }
    this.rempli = Math.min(W.length, this.rempli + n); this.depuis += n;
    if (this.rempli === W.length && this.depuis >= DUREE_PAS * TAUX_CW && !this.occupe && this.etat.pret) {
      this.depuis = 0; this.occupe = true;
      this.fil.postMessage({ fenetre: W.slice(), premiere: this.premiere });
      this.premiere = false;
    }
  }

  creerPanneau() {
    this.zone = el("div", { className: "cw-texte", role: "log", ariaLive: "polite" });
    this.zone.dataset.vide = "Mode CW, squelch ouvert : calez-vous sur une station…";
    this.statut = el("div", { className: "petit" });
    const mode = el("button", { type: "button", textContent: "Mode CW" });
    mode.onclick = () => this.ctx.reglerMode("CW");
    const effacer = el("button", { type: "button", textContent: "Effacer" });
    effacer.onclick = () => { this.zone.textContent = ""; };
    const copier = el("button", { type: "button", textContent: "Copier" });
    copier.onclick = () => navigator.clipboard?.writeText(this.zone.textContent).then(() => {
      copier.textContent = "Copié"; setTimeout(() => { copier.textContent = "Copier"; }, 1200);
    }).catch(() => {});
    const enregistrer = el("button", { type: "button", textContent: "Enregistrer", title: "Télécharge le texte décodé, avec la date et la fréquence" });
    enregistrer.onclick = () => {
      const d = new Date(), local = new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
      const contenu = `[${local.replace("T", " ")} ${(this.ctx.frequence() / 1e6).toFixed(4)} MHz]\n${this.zone.textContent}\n`;
      const a = el("a", { href: URL.createObjectURL(new Blob([contenu], { type: "text/plain" })), download: `cw_${local.replace(/[-:]/g, "").replace("T", "_")}.txt` });
      a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    };
    // Agrandir : le texte passe dans une fenêtre flottante, déplaçable et redimensionnable (taille et place retenues).
    const actions = el("div", { className: "scanner-choix cw-actions" }, mode, effacer, copier, enregistrer);
    const contenu = el("div", { className: "cw-contenu" }, this.zone, this.statut, actions);
    const { panneau, dialogue } = panneauAgrandissable(this.ctx, { titre: "Décodeur CW", id: "cw-titre", contenu, actions,
      tailleDefaut: 1.6, defiler: this.zone, attenteTexte: "Texte affiché dans la fenêtre agrandie." });
    this.dialogue = dialogue;
    this.minuterie = setInterval(() => this.rafraichir(), 250);
    this.rafraichir();
    return panneau;
  }

  rafraichir() {
    for (let morceau of this.recus.splice(0)) {
      const t = this.zone.textContent;
      if (!t || t.endsWith(" ")) morceau = morceau.replace(/^ +/, "");
      this.zone.textContent = (t + morceau.replace(/  +/g, " ")).slice(-4000);
      this.zone.scrollTop = this.zone.scrollHeight;
    }
    const e = this.etat, m = this.ctx.mode();
    this.statut.textContent = e.erreur ? `Erreur : ${e.erreur}`
      : !e.pret ? "Chargement du réseau de neurones…"
      : !["CW", "USB", "LSB"].includes(m) ? `Mode ${m} : passez en CW pour décoder`
      : e.silence ? "Squelch fermé : ouvrez-le pour décoder"
      : e.tonalite === null ? `En attente de ${DUREE_FENETRE} s d'audio…`
      : `Tonalité ${Math.round(e.tonalite)} Hz · ${e.wpm ? `~${Math.round(e.wpm)} WPM` : "vitesse ?"} · contraste ${Math.round(e.contraste)} dB · calcul ${Math.round(e.duree)} ms`;
  }

  arreter() { clearInterval(this.minuterie); this.fil?.terminate(); if (this.dialogue?.open) this.dialogue.close(); }
}

// Décodeur APRS : même démodulateur que le module decodeur_aprs.py de SDR Modulaire (aprs.js).
// Le module extrait son canal des IQ (indépendant du VFO) : NCO → 240 kHz → 48 kHz → FM → 24 kHz.
const FREQUENCES_APRS = [[144_800_000, "144,800 MHz · Europe"], [144_390_000, "144,390 MHz · Amérique du Nord"], [145_825_000, "145,825 MHz · ISS"]];
class DecodeurAPRS {
  static nom = "Décodeur APRS";
  static description = "Trames APRS (AFSK 1200 bauds) décodées dans le navigateur, sur 144,800 MHz ou une autre fréquence APRS de la bande reçue.";

  demarrer() {
    this.frequence = this.ctx.lire("frequence", FREQUENCES_APRS[0][0]);
    this.recues = []; this.nombre = 0; this.cle = ""; this.derniersIQ = -Infinity; this.couvert = false; this.saturation = 0;
    this.stations = new Map();         // indicatif (ou nom d'objet) → { id, lat, lon, symbole, commentaire, vu, nb, trace }
    // Station d'écoute : la sienne, ou à défaut celle déjà enregistrée dans le module ADS-B.
    let adsb = null; try { adsb = JSON.parse(localStorage.getItem("sdr-web/ADS-B (avions)/station")); } catch { /* rien */ }
    this.station = this.ctx.lire("station", adsb);
    this.ctx.abonner("iq", (re, im, centre, taux) => this.recevoir(re, im, centre, taux));
  }

  recevoir(re, im, centre, taux) {
    this.derniersIQ = performance.now();
    let sat = 0;
    for (let k = 0; k < re.length; k += 4) if (re[k] > 0.95 || re[k] < -0.95 || im[k] > 0.95 || im[k] < -0.95) sat++;
    this.saturation = sat / Math.ceil(re.length / 4);
    const f = this.frequence;
    this.couvert = Math.abs(f - centre) + 10_000 < 0.42 * taux;
    if (!this.couvert) { this.cle = ""; return; }
    const cle = `${centre}/${taux}/${f}`;
    if (cle !== this.cle) {                  // nouvelle bande : chaîne de démodulation neuve
      this.cle = cle;
      const d1 = Math.round(taux / 240_000), fs1 = taux / d1, d2 = Math.round(fs1 / 48_000);
      this.nco = new NCO(taux); this.nco.freq = f - centre;
      this.etage1 = new FirComplexe(passeBas(taux, 30_000, d1 * 4 + 1), d1);
      this.etage2 = new FirComplexe(passeBas(fs1, 12_000, d2 * 12 + 1), d2);
      this.fs = fs1 / d2;
      this.versAudio = new FirComplexe(passeBas(this.fs, 6_000, 31), 2);
      this.pr = 1; this.pi = 0;
      this.demod = new DemodAFSK(this.fs / 2);
    }
    const [cr, ci] = this.etage2.traiter(...this.etage1.traiter(...this.nco.melanger(re, im)));
    const fm = new Float32Array(cr.length);
    let pr = this.pr, pi = this.pi;
    for (let k = 0; k < cr.length; k++) { const a = cr[k], b = ci[k]; fm[k] = Math.atan2(b * pr - a * pi, a * pr + b * pi); pr = a; pi = b; }
    this.pr = pr; this.pi = pi;
    // Diagnostic : variance du discriminateur par bloc (≈ 3,3 = bruit seul, nettement moins = porteuse FM présente).
    let v = 0; for (let k = 0; k < fm.length; k++) v += fm[k] * fm[k];
    if (fm.length) { (this.varFM ??= []).push(v / fm.length); if (this.varFM.length > 600) this.varFM.shift(); }
    const [audio] = this.versAudio.traiter(fm, new Float32Array(fm.length));
    for (const o of this.demod.traiter(audio)) { const t = trameVersTexte(o); if (t) { this.recues.push(t); this.placer(t); } }
  }

  /** Mesures pour le rapport de diagnostic (sans le contenu des trames ni la station). */
  diagnostic() {
    const v = [...(this.varFM || [])].sort((a, b) => a - b), q = p => v.length ? +v[Math.floor(p * (v.length - 1))].toFixed(2) : null;
    return { frequence: this.frequence, couverte: this.couvert, saturation: +(100 * this.saturation).toFixed(3),
      trames: this.recues.length, stations: this.stations.size, iqIlYa: Number.isFinite(this.derniersIQ) ? +((performance.now() - this.derniersIQ) / 1000).toFixed(1) : null,
      varianceFM_30s: { min: q(0), mediane: q(0.5), bruitSeul: 3.29 } };
  }

  /** Met à jour la station (ou l'objet) si la trame contient une position. */
  placer(texte) {
    const p = positionAPRS(texte); if (!p) return;
    const maintenant = Date.now();
    let s = this.stations.get(p.id);
    if (!s) { s = { id: p.id, nb: 0, trace: [] }; this.stations.set(p.id, s); }
    Object.assign(s, { lat: p.lat, lon: p.lon, symbole: p.symbole, commentaire: p.commentaire || s.commentaire || "", vu: maintenant });
    s.nb++;
    const der = s.trace[s.trace.length - 1];
    if (!der || der[0] !== p.lat || der[1] !== p.lon) { s.trace.push([p.lat, p.lon, maintenant]); if (s.trace.length > 200) s.trace.shift(); }
    this.carteAJour = true;
  }

  creerPanneau() {
    const choix = el("select", { title: "Fréquence APRS décodée (elle doit être dans la bande reçue)" },
      ...FREQUENCES_APRS.map(([f, t]) => el("option", { value: f, textContent: t, selected: f === this.frequence })));
    choix.onchange = () => { this.frequence = Number(choix.value); this.ctx.ecrire("frequence", this.frequence); this.cle = ""; };
    const accorder = el("button", { type: "button", textContent: "Accorder", title: "Règle le VFO sur la fréquence APRS en NFM (pour l'entendre aussi)" });
    accorder.onclick = () => { this.ctx.reglerMode("NFM"); this.ctx.reglerFrequence(this.frequence); };
    this.statut = el("div", { className: "petit" });
    this.liste = el("ul", { className: "journal aprs-liste" });
    const effacer = el("button", { type: "button", textContent: "Effacer" });
    effacer.onclick = () => this.liste.replaceChildren();
    const texte = () => [...this.liste.children].reverse().map(li => li.textContent).join("\n");
    const copier = el("button", { type: "button", textContent: "Copier" });
    copier.onclick = () => navigator.clipboard?.writeText(texte()).then(() => {
      copier.textContent = "Copié"; setTimeout(() => { copier.textContent = "Copier"; }, 1200);
    }).catch(() => {});
    const enregistrer = el("button", { type: "button", textContent: "Enregistrer", title: "Télécharge les trames reçues dans un fichier texte" });
    enregistrer.onclick = () => {
      const d = new Date(), h = new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19).replace(/[-:]/g, "").replace("T", "_");
      const a = el("a", { href: URL.createObjectURL(new Blob([texte() + "\n"], { type: "text/plain" })), download: `aprs_${h}.txt` });
      a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    };
    const ouvrirCarte = el("button", { type: "button", textContent: "Carte", title: "Stations APRS reçues sur une carte, comme les avions de l'ADS-B" });
    ouvrirCarte.setAttribute("aria-haspopup", "dialog");
    ouvrirCarte.onclick = () => this.ouvrirCarte();
    this.creerCarte();
    const actions = el("div", { className: "scanner-choix cw-actions" }, effacer, copier, enregistrer, ouvrirCarte);
    const contenu = el("div", { className: "cw-contenu aprs-contenu" },
      el("div", { className: "scanner-choix" }, choix, accorder), this.statut, this.liste, actions);
    const { panneau, dialogue } = panneauAgrandissable(this.ctx, { titre: "Décodeur APRS", id: "aprs-titre", contenu, actions,
      tailleDefaut: 1.1, attenteTexte: "Trames affichées dans la fenêtre agrandie." });
    this.dialogue = dialogue;
    panneau.append(this.fenCarte);
    this.minuterie = setInterval(() => this.rafraichir(), 250);
    this.rafraichir();
    return panneau;
  }

  /** Fenêtre de la carte : même présentation que celle de l'ADS-B (carte, tableau, station). */
  creerCarte() {
    const titre = el("h2", { id: "aprs-carte-titre", textContent: "APRS · stations reçues" });
    titre.title = "Tirer la barre pour déplacer · coin en bas à droite pour agrandir";
    this.resume = el("span", { className: "adsb-resume" });
    const fermer = el("button", { type: "button", textContent: "Fermer" });
    const recentrer = el("button", { type: "button", textContent: "Recentrer" });
    const toile = el("canvas", { className: "adsb-carte" });
    toile.setAttribute("aria-label", "Carte des stations APRS reçues");
    this.tableau = el("tbody");
    const table = el("table", { className: "adsb-table" },
      el("thead", {}, el("tr", {}, ...["Station", "Vue", "Dist.", "Trames", "Commentaire"].map(t => el("th", { textContent: t })))), this.tableau);
    const st = this.station;
    const lat = el("input", { type: "number", step: "0.0001", min: -90, max: 90, placeholder: "48.8566", value: st?.lat ?? "" });
    const lon = el("input", { type: "number", step: "0.0001", min: -180, max: 180, placeholder: "2.3522", value: st?.lon ?? "" });
    const placer = el("button", { type: "button", textContent: "Enregistrer la station" });
    placer.onclick = () => {
      const la = parseFloat(lat.value), lo = parseFloat(lon.value);
      this.station = Number.isFinite(la) && Number.isFinite(lo) ? { lat: la, lon: lo } : null;
      this.ctx.ecrire("station", this.station); this.carte.station = this.station; this.carte.centrer(); this.majTableau();
    };
    const pied = el("div", { className: "adsb-pied" },
      el("label", {}, "Station : lat. ", lat), el("label", {}, " lon. ", lon), placer,
      el("span", { className: "petit", textContent: "Facultatif : sert aux distances et aux cercles (reprise du module ADS-B si déjà saisie)." }));
    const entete = el("header", {}, titre, this.resume, el("span", { className: "adsb-boutons" }, recentrer, fermer));
    this.fenCarte = el("dialog", { className: "adsb-dialogue" }, entete,
      el("div", { className: "adsb-corps" }, toile, el("div", { className: "adsb-cote" }, table)), pied);
    this.fenCarte.setAttribute("aria-labelledby", titre.id);
    this.afficherCarte = fenetreFlottante(this.fenCarte, entete, this.ctx, "carte");
    this.carte = new CarteRadar(toile, { surChoix: id => { this.choisi = id; this.majTableau(); } });
    this.carte.station = this.station;
    fermer.onclick = () => this.fenCarte.close();
    recentrer.onclick = () => this.carte.centrer();
  }

  ouvrirCarte() {
    if (this.fenCarte.open) return;
    this.afficherCarte(); this.carte.points = [...this.stations.values()]; this.carte.centrer(); this.majTableau();
  }

  trierStations() {
    const st = this.station, cle = s => st ? distanceKm(st.lat, st.lon, s.lat, s.lon) : -s.vu;
    return [...this.stations.values()].sort((a, b) => cle(a) - cle(b));
  }

  majTableau() {
    const st = this.station;
    this.tableau.replaceChildren(...this.trierStations().map(s => {
      const tr = el("tr", { className: s.id === this.choisi ? "choisi" : "" },
        el("td", {}, el("i", { className: "adsb-pastille" }), s.id),
        el("td", { textContent: new Date(s.vu).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }) }),
        el("td", { textContent: st ? `${Math.round(distanceKm(st.lat, st.lon, s.lat, s.lon))} km` : "" }),
        el("td", { textContent: s.nb }),
        el("td", { textContent: s.commentaire.slice(0, 40), title: s.commentaire }));
      tr.firstChild.firstChild.style.background = couleurSymbole(s.symbole);
      tr.onclick = () => {
        this.choisi = this.carte.selection = s.id;
        this.carte.lat0 = s.lat; this.carte.lon0 = s.lon; this.carte.dessiner(); this.majTableau();
      };
      return tr;
    }));
  }

  rafraichir() {
    if (this.fenCarte?.open) {
      const avecPos = this.stations.size;
      this.resume.textContent = `${avecPos} station(s) placée(s) · ${this.nombre} trame(s)`;
      this.carte.points = [...this.stations.values()]; this.carte.dessiner();
      if (this.carteAJour) { this.carteAJour = false; this.majTableau(); }
    }
    for (const t of this.recues.splice(0)) {
      this.nombre++;
      this.liste.prepend(el("li", { textContent: `${new Date().toLocaleTimeString("fr-FR")}  ${t}` }));
      if (this.liste.children.length > 300) this.liste.lastChild.remove();
    }
    const nom = FREQUENCES_APRS.find(([f]) => f === this.frequence)?.[1].split(" ·")[0] || "";
    const sat = this.saturation > 0.001;
    this.statut.classList.toggle("aprs-alerte", sat);
    this.statut.textContent = performance.now() - this.derniersIQ > 1000 ? "Démarrez la réception : le décodeur lit les IQ de la clé."
      : !this.couvert ? `${nom} hors de la bande reçue : cliquez sur Accorder.`
      : `Écoute de ${nom} · ${this.nombre} trame(s)` + (sat ? ` · SATURATION ${(100 * this.saturation).toFixed(1)} % : baissez le gain RF ou éloignez l'émetteur` : "");
  }

  arreter() { clearInterval(this.minuterie); if (this.dialogue?.open) this.dialogue.close(); if (this.fenCarte?.open) this.fenCarte.close(); }
}

const CRENEAUX_FT = { FT8: 15000, FT4: 7500 };
// Fréquences d'appel (Hz) de la table par défaut de WSJT-X : [bande, FT8, FT4], null si le mode n'y est pas.
const BANDES_FT = [
  ["160 m", 1_840_000, null], ["80 m", 3_573_000, 3_575_000], ["60 m", 5_357_000, 5_357_000],
  ["40 m", 7_074_000, 7_047_500], ["30 m", 10_136_000, 10_140_000], ["20 m", 14_074_000, 14_080_000],
  ["17 m", 18_100_000, 18_104_000], ["15 m", 21_074_000, 21_140_000], ["12 m", 24_915_000, 24_919_000],
  ["10 m", 28_074_000, 28_180_000], ["6 m", 50_313_000, 50_318_000], ["4 m", 70_154_000, null],
  ["2 m", 144_174_000, 144_170_000], ["70 cm", 432_174_000, null],
];
const frequenceBandeFT = (bande, mode) => { const b = BANDES_FT.find(b => b[0] === bande); return b ? b[mode === "FT8" ? 1 : 2] : null; };
const bandeFT = (f, mode) => BANDES_FT.find(b => { const g = b[mode === "FT8" ? 1 : 2]; return g !== null && Math.abs(f - g) < 500; })?.[0] ?? "";

class DecodeurFT {
  static nom = "Décodeur FT8 / FT4";
  static description = "Messages FT8 et FT4 reçus en USB, décodés dans le navigateur sur des créneaux UTC.";

  demarrer() {
    this.mode = this.ctx.lire("mode", "FT8") === "FT4" ? "FT4" : "FT8";
    this.sequence = 0;
    this.pret = false;
    this.occupe = false;
    this.dernierAudio = 0;
    this.fil = new Worker(new URL("./ft8-worker.js?v=1", import.meta.url));
    this.fil.onmessage = ({ data }) => {
      if (data.pret) { this.pret = true; return; }
      this.occupe = false;
      if (data.sequence !== undefined && data.sequence !== this.sequence) return;
      if (data.erreur) { this.erreur = data.erreur; return; }
      const heure = new Date(data.utc).toISOString().slice(11, 19);
      for (const message of data.messages) {
        const ligne = el("div", { textContent: `${heure}  ${Math.round(message.freq)} Hz  ${message.msg}` });
        this.zone.append(ligne);
        if (this.zone.childElementCount > 200) this.zone.firstElementChild.remove();
      }
      if (data.messages.length) this.zone.scrollTop = this.zone.scrollHeight;
      this.bilan = `${heure} UTC · ${data.messages.length} message(s)`;
    };
    this.fil.onerror = e => { this.erreur = e.message || "décodeur indisponible"; };
    this.ctx.abonner("audio", (audio, taux) => this.recevoir(audio, taux, Date.now()));
    this.ctx.abonner("vfo", () => this.reinitialiser());
  }

  reinitialiser() {
    this.sequence++;
    this.finAudio = undefined;
    this.dernierAudio = 0;
    this.audio = null;
    this.bilan = "";
  }

  recevoir(audio, taux, maintenant) {
    if (!this.pret || this.erreur || this.ctx.mode() !== "USB" || !audio.length) return;
    const taille = taux * CRENEAUX_FT[this.mode] / 1000;
    const arrivee = Math.round(maintenant * taux / 1000) - audio.length;
    if (this.taux !== taux || Math.abs(arrivee - this.finAudio) > taux / 2) this.reinitialiser();
    this.taux = taux;
    const debut = this.finAudio ?? arrivee;
    this.finAudio = debut + audio.length;
    this.dernierAudio = maintenant;

    for (let i = 0; i < audio.length;) {
      const instant = debut + i;
      // Un créneau entamé au démarrage est ignoré. Le premier complet commence au prochain top UTC.
      const position = instant % taille;
      const n = Math.min(audio.length - i, taille - position);
      if (position === 0) this.audio = new Float32Array(taille);
      if (this.audio) {
        this.audio.set(audio.subarray(i, i + n), position);
        if (position + n === taille) {
          if (!this.occupe) {
            this.occupe = true;
            const utc = Math.round((instant - position) * 1000 / taux);
            this.fil.postMessage({ audio: this.audio, taux, mode: this.mode, sequence: this.sequence, utc }, [this.audio.buffer]);
          }
          this.audio = null;
        }
      }
      i += n;
    }
  }

  creerPanneau() {
    const mode = el("select", { ariaLabel: "Mode numérique" },
      ...Object.keys(CRENEAUX_FT).map(v => el("option", { value: v, textContent: v })));
    mode.value = this.mode;
    // Liste des bandes : choisir une bande passe en USB sur sa fréquence d'appel ; « Bande… » si le VFO n'est sur aucune.
    const bande = el("select", { ariaLabel: "Bande", title: "Accorde le récepteur en USB sur la fréquence d'appel de la bande" });
    const accorder = f => {
      if (f === null) return;
      this.ctx.reglerMode("USB"); this.ctx.reglerFrequence(f);
      // La clé ne descend pas toujours en HF : l'accord est alors ramené au bord de sa plage.
      const ici = this.ctx.frequence();
      this.horsPortee = Math.abs(ici - f) > 1000 ? { f, ici } : null;
      suivre();
    };
    const suivre = () => { const b = bandeFT(this.ctx.frequence(), this.mode); if (bande.value !== b) bande.value = b; };
    const remplir = () => {
      bande.replaceChildren(el("option", { value: "", textContent: "Bande…" }),
        ...BANDES_FT.filter(b => b[this.mode === "FT8" ? 1 : 2] !== null).map(([nom, ft8, ft4]) => {
          const f = this.mode === "FT8" ? ft8 : ft4;
          return el("option", { value: nom, textContent: `${nom} · ${(f / 1e6).toFixed(f % 1000 ? 4 : 3).replace(".", ",")}` });
        }));
      suivre();
    };
    bande.onchange = () => accorder(frequenceBandeFT(bande.value, this.mode));
    mode.onchange = () => {
      const avant = bandeFT(this.ctx.frequence(), this.mode);
      this.mode = mode.value;
      this.ctx.ecrire("mode", this.mode);
      this.reinitialiser();
      this.zone.replaceChildren();
      remplir();
      if (avant) accorder(frequenceBandeFT(avant, this.mode));   // même bande, fréquence d'appel de l'autre mode
    };
    remplir();
    const effacer = el("button", { type: "button", textContent: "Effacer" });
    effacer.onclick = () => this.zone.replaceChildren();
    this.zone = el("div", { className: "cw-texte", role: "log", ariaLive: "polite" });
    this.zone.dataset.vide = "UTC · fréquence audio · message";
    const statut = el("div", { className: "petit" });
    const actualiser = () => {
      if (this.horsPortee && this.ctx.frequence() !== this.horsPortee.ici) this.horsPortee = null;   // VFO déplacé depuis
      const texte = this.erreur ? `Erreur : ${this.erreur}`
        : this.horsPortee ? `${(this.horsPortee.f / 1e6).toFixed(3).replace(".", ",")} MHz hors de portée de la clé : activez l’entrée HF ou indiquez un convertisseur.`
        : !this.pret ? "Chargement du décodeur…"
        : this.ctx.mode() !== "USB" ? "Choisissez une bande : le récepteur passe en USB sur la fréquence d’appel."
        : Date.now() - this.dernierAudio > 1000 ? "En attente du signal audio."
        : this.bilan || "En attente d’un créneau UTC complet…";
      if (statut.textContent !== texte) statut.textContent = texte;
      suivre();
    };
    actualiser();
    this.minuterie = setInterval(actualiser, 250);
    const actions = el("div", { className: "scanner-choix cw-actions" }, mode, bande, effacer);
    const aide = el("small", { textContent: "Synchronisez l’horloge de l’ordinateur et choisissez une bande (clé qui reçoit la HF ou convertisseur)." });
    const contenu = el("div", { className: "cw-contenu" }, actions, this.zone, statut, aide);
    const { panneau, dialogue } = panneauAgrandissable(this.ctx, {
      titre: DecodeurFT.nom, id: "ft-titre", contenu, actions, tailleDefaut: 1.1, defiler: this.zone,
      attenteTexte: "Messages affichés dans la fenêtre agrandie.",
    });
    this.dialogue = dialogue;
    return panneau;
  }

  arreter() {
    clearInterval(this.minuterie);
    this.fil.terminate();
    if (this.dialogue?.open) this.dialogue.close();
  }
}


class DecodeurDMR {
  static nom = "Décodeur DMR";
  static description = "Appels DMR (relais et direct) : slot, color code, groupe ou appel privé, ID et alias de l'émetteur, SMS. La voix n'est pas décodée.";

  demarrer() {
    this.cle = ""; this.derniersIQ = -Infinity; this.couvert = false;
    this.enCours = new Map();          // slot → { ...appel, heure, li }
    this.moteur = null; this.bruit = null; this.crete = null;
    this.ctx.abonner("iq", (re, im, centre, taux) => this.recevoir(re, im, centre, taux));
  }

  recevoir(re, im, centre, taux) {
    this.derniersIQ = performance.now();
    const f = this.ctx.frequence();
    this.couvert = Math.abs(f - centre) + 8_000 < 0.42 * taux;
    if (!this.couvert) return;
    const cle = `${centre}/${taux}`;
    if (cle !== this.cle) {                  // nouvelle bande : chaîne neuve (changer de fréquence garde le décodeur)
      this.cle = cle;
      const d1 = Math.round(taux / 240_000), fs1 = taux / d1, d2 = Math.round(fs1 / TAUX_DMR);
      this.nco = new NCO(taux);
      this.etage1 = new FirComplexe(passeBas(taux, 30_000, d1 * 4 + 1), d1);
      this.etage2 = new FirComplexe(passeBas(fs1, 7_000, d2 * 12 + 1), d2);
      this.pr = 1; this.pi = 0;
      this.moteur = new MoteurDMR();
    }
    this.nco.freq = f - centre;
    const [cr, ci] = this.etage2.traiter(...this.etage1.traiter(...this.nco.melanger(re, im)));
    if (!cr.length) return;
    let p = 0;
    for (let k = 0; k < cr.length; k++) p += cr[k] * cr[k] + ci[k] * ci[k];
    p = 10 * Math.log10(p / cr.length + 1e-20);
    this.bruit = this.bruit === null ? p : Math.min(p, this.bruit + 0.02);
    this.crete = this.crete === null ? p : Math.max(p, this.crete - 0.5);
    const fm = new Float32Array(cr.length), k2 = TAUX_DMR / (2 * Math.PI);
    let pr = this.pr, pi = this.pi;
    for (let k = 0; k < cr.length; k++) { const a = cr[k], b = ci[k]; fm[k] = Math.atan2(b * pr - a * pi, a * pr + b * pi) * k2; pr = a; pi = b; }
    this.pr = pr; this.pi = pi;
    for (const ev of this.moteur.traiter(fm)) this.evenement(ev);
  }

  ligne(texte) {
    const li = el("li", { textContent: texte });
    this.liste.prepend(li);
    while (this.liste.childElementCount > 300) this.liste.lastElementChild.remove();
    return li;
  }

  texteLigne(a, fin) {
    const cc = a.cc !== null && a.cc !== undefined ? ` · CC ${a.cc}` : "";
    return `${a.heure}  ${nomSlot(a.slot)}${cc}  ${texteAppel(a)}${fin}`;
  }

  evenement(ev) {
    if (!this.liste) return;
    const h = new Date().toTimeString().slice(0, 8), s = ev.slot;
    if (ev.type === "appel") {
      const a = { ...ev, heure: h };
      a.li = this.ligne(this.texteLigne(a, "  …"));
      this.enCours.set(s, a);
    } else if (ev.type === "alias" && this.enCours.has(s)) {
      const a = this.enCours.get(s); a.alias = ev.texte; a.li.textContent = this.texteLigne(a, "  …");
    } else if (ev.type === "fin" && this.enCours.has(s)) {
      const a = this.enCours.get(s); a.alias = ev.alias || a.alias;
      a.li.textContent = this.texteLigne(a, `  ${ev.duree.toFixed(1).replace(".", ",")} s`);
      this.enCours.delete(s);
    } else if (ev.type === "sms") {
      this.ligne(`${h}  ${nomSlot(s)}  ${ev.src} → ${ev.groupe ? "TG" : "privé"} ${ev.dst} · ${ev.texte}${ev.crc ? "" : "  (CRC faux)"}`);
    } else if (ev.type === "donnees" && (ev.blocs === 0 || this.voirCsbk.checked)) {
      this.ligne(`${h}  ${nomSlot(s)}  données ${SAP_DMR[ev.sap] ?? `SAP ${ev.sap}`} ${ev.src} → ${ev.groupe ? "groupe" : "privé"} ${ev.dst}`);
    } else if (ev.type === "csbk" && this.voirCsbk.checked) {
      this.ligne(ev.opcode === 0x3D ? `${h}  ${nomSlot(s)}  préambule ${ev.src} → ${ev.groupe ? "TG" : "privé"} ${ev.dst}`
        : `${h}  ${nomSlot(s)}  CSBK ${ev.opcode.toString(16).toUpperCase().padStart(2, "0")} ${FID_DMR[ev.fid] ?? ""}`.trimEnd());
    }
  }

  creerPanneau() {
    this.statut = el("div", { className: "petit" });
    this.ts = [1, 2].map(() => el("div", { className: "petit dmr-slot" }));
    this.diag = el("div", { className: "petit", title: "Crête du canal au-dessus du bruit, salves DMR reconnues, color code, type d'émetteur (relais, mobile, direct)" });
    this.diag.style.color = "var(--doux)";
    this.liste = el("ul", { className: "journal aprs-liste" });
    const effacer = el("button", { type: "button", textContent: "Effacer" });
    effacer.onclick = () => { this.liste.replaceChildren(); this.enCours.clear(); };
    const texte = () => [...this.liste.children].reverse().map(li => li.textContent).join("\n");
    const copier = el("button", { type: "button", textContent: "Copier" });
    copier.onclick = () => navigator.clipboard?.writeText(texte()).then(() => {
      copier.textContent = "Copié"; setTimeout(() => { copier.textContent = "Copier"; }, 1200);
    }).catch(() => {});
    const enregistrer = el("button", { type: "button", textContent: "Enregistrer", title: "Télécharge les appels et messages dans un fichier texte" });
    enregistrer.onclick = () => {
      const d = new Date(), h = new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19).replace(/[-:]/g, "").replace("T", "_");
      const a = el("a", { href: URL.createObjectURL(new Blob([texte() + "\n"], { type: "text/plain" })), download: `dmr_${h}.txt` });
      a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    };
    this.voirCsbk = el("input", { type: "checkbox", checked: this.ctx.lire("csbk", false) });
    this.voirCsbk.onchange = () => this.ctx.ecrire("csbk", this.voirCsbk.checked);
    const csbk = el("label", { className: "sstv-auto", title: "Affiche aussi les blocs de signalisation (CSBK) : nombreux sur certains relais" }, this.voirCsbk, "CSBK");
    const actions = el("div", { className: "scanner-choix cw-actions" }, effacer, copier, enregistrer, csbk);
    const contenu = el("div", { className: "cw-contenu aprs-contenu" }, this.statut, ...this.ts, this.diag, this.liste, actions);
    const { panneau, dialogue } = panneauAgrandissable(this.ctx, { titre: DecodeurDMR.nom, id: "dmr-titre", contenu, actions,
      tailleDefaut: 1.1, attenteTexte: "Appels affichés dans la fenêtre agrandie." });
    this.dialogue = dialogue;
    this.minuterie = setInterval(() => this.rafraichir(), 250);
    this.rafraichir();
    return panneau;
  }

  rafraichir() {
    const m = this.moteur;
    const texte = performance.now() - this.derniersIQ > 1500 ? "En attente des échantillons de la clé."
      : !this.couvert ? "Le VFO est trop près du bord de la bande reçue."
      : !m?.salves ? "Accordez le VFO sur une fréquence DMR (relais ou direct). Le mode n'a pas d'importance : le module démodule lui-même."
      : this.enCours.size ? "Signal DMR reçu." : "Signal DMR reçu, pas d'appel en cours.";
    if (this.statut.textContent !== texte) this.statut.textContent = texte;
    this.ts.forEach((l, i) => { const a = this.enCours.get(i + 1), t = `${nomSlot(i + 1)} : ${a ? texteAppel(a) : "—"}`; if (l.textContent !== t) l.textContent = t; });
    if (this.crete !== null && m) {
      const d = `crête ${Math.round(this.crete - this.bruit)} dB · ${m.salves} salves${m.cc !== null ? ` · CC ${m.cc}` : ""}${m.source ? ` · ${m.source}` : ""}`;
      if (this.diag.textContent !== d) this.diag.textContent = d;
    }
  }

  arreter() { clearInterval(this.minuterie); if (this.dialogue?.open) this.dialogue.close(); }
}

class Tonalites {
  static nom = "Tonalités CTCSS et DTMF";
  static description = "Sous-porteuse CTCSS d'accès d'un relais (fréquence normalisée, mesure, déviation), codes DTMF et tonalité 1750 Hz entendus sur la fréquence du VFO, quel que soit le mode.";

  demarrer() {
    this.cle = ""; this.f = null; this.derniersIQ = -Infinity; this.couvert = false;
    this.moteur = null; this.bruit = null; this.crete = null;
    this.sequence = ""; this.liSequence = null; this.tTouche = 0; this.derniereTouche = 0;
    this.liCtcss = null; this.li1750 = null;
    this.ctx.abonner("iq", (re, im, centre, taux) => this.recevoir(re, im, centre, taux));
  }

  recevoir(re, im, centre, taux) {
    this.derniersIQ = performance.now();
    const f = this.ctx.frequence();
    this.couvert = Math.abs(f - centre) + 8_000 < 0.42 * taux;
    if (!this.couvert) return;
    const cle = `${centre}/${taux}`;
    if (cle !== this.cle) {                  // nouvelle bande : chaîne neuve
      this.cle = cle;
      const d1 = Math.round(taux / 240_000), fs1 = taux / d1, d2 = Math.round(fs1 / TAUX_TONALITES);
      this.nco = new NCO(taux);
      this.etage1 = new FirComplexe(passeBas(taux, 30_000, d1 * 4 + 1), d1);
      this.etage2 = new FirComplexe(passeBas(fs1, 7_000, d2 * 12 + 1), d2);
      this.pr = 1; this.pi = 0;
      this.moteur = new MoteurTonalites();
    } else if (f !== this.f) {               // autre fréquence : autres tonalités
      this.moteur = new MoteurTonalites();
      this.clore(); this.liCtcss = null; this.li1750 = null;
    }
    this.f = f;
    this.nco.freq = f - centre;
    const [cr, ci] = this.etage2.traiter(...this.etage1.traiter(...this.nco.melanger(re, im)));
    if (!cr.length) return;
    let p = 0;
    for (let k = 0; k < cr.length; k++) p += cr[k] * cr[k] + ci[k] * ci[k];
    p = 10 * Math.log10(p / cr.length + 1e-20);
    this.bruit = this.bruit === null ? p : Math.min(p, this.bruit + 0.02);
    this.crete = this.crete === null ? p : Math.max(p, this.crete - 0.5);
    const fm = new Float32Array(cr.length), k2 = TAUX_TONALITES / (2 * Math.PI);
    let pr = this.pr, pi = this.pi;
    for (let k = 0; k < cr.length; k++) { const a = cr[k], b = ci[k]; fm[k] = Math.atan2(b * pr - a * pi, a * pr + b * pi) * k2; pr = a; pi = b; }
    this.pr = pr; this.pi = pi;
    for (const ev of this.moteur.traiter(fm)) this.evenement(ev);
  }

  ligne(texte) {
    const li = el("li", { textContent: texte });
    this.liste.prepend(li);
    while (this.liste.childElementCount > 300) this.liste.lastElementChild.remove();
    return li;
  }

  clore() { this.sequence = ""; this.liSequence = null; }

  evenement(ev) {
    if (!this.liste) return;
    const h = new Date().toTimeString().slice(0, 8);
    if (ev.type === "ctcss") {
      this.hCtcss = h; this.liCtcss = this.ligne(`${h}  ${texteCtcss(ev)}  …`);
    } else if (ev.type === "ctcss_fin") {
      const t = `${this.hCtcss ?? h}  ${texteCtcss(ev)}  ${secondes(ev.duree)}`;
      if (this.liCtcss) this.liCtcss.textContent = t; else this.ligne(t);
      this.liCtcss = null;
    } else if (ev.type === "dtmf") {
      if (!this.sequence || ev.t - this.tTouche > 2 || performance.now() - this.derniereTouche > 3000) {
        this.clore(); this.hSequence = h; this.liSequence = this.ligne(`${h}  DTMF`);
      }
      this.sequence += ev.touche; this.tTouche = ev.t; this.derniereTouche = performance.now();
      this.liSequence.textContent = `${this.hSequence}  DTMF ${this.sequence}`;
    } else if (ev.type === "1750") {
      this.h1750 = h; this.li1750 = this.ligne(`${h}  tonalité 1750 Hz (ouverture de relais)  …`);
    } else if (ev.type === "1750_fin") {
      const t = `${this.h1750 ?? h}  tonalité 1750 Hz (ouverture de relais)  ${secondes(ev.duree)}`;
      if (this.li1750) this.li1750.textContent = t; else this.ligne(t);
      this.li1750 = null;
    }
  }

  creerPanneau() {
    this.statut = el("div", { className: "petit" });
    const gros = "font: 700 1.05rem 'Orbitron', sans-serif; color: var(--cyan); margin-top: 4px; user-select: text";
    this.lCtcss = el("div", { className: "tonalite-gros" }); this.lCtcss.style.cssText = gros;
    this.lDetail = el("div", { className: "petit" });
    this.lDtmf = el("div", { className: "tonalite-gros" }); this.lDtmf.style.cssText = gros + "; overflow-wrap: anywhere";
    this.diag = el("div", { className: "petit", title: "Crête du canal au-dessus du bruit, dernière analyse CTCSS (raie la plus forte, part de l'énergie 55–270 Hz), touches DTMF reçues" });
    this.diag.style.color = "var(--doux)";
    this.liste = el("ul", { className: "journal aprs-liste" });
    const effacer = el("button", { type: "button", textContent: "Effacer" });
    effacer.onclick = () => { this.liste.replaceChildren(); this.clore(); this.liCtcss = null; this.li1750 = null; };
    const texte = () => [...this.liste.children].reverse().map(li => li.textContent).join("\n");
    const copier = el("button", { type: "button", textContent: "Copier" });
    copier.onclick = () => navigator.clipboard?.writeText(texte()).then(() => {
      copier.textContent = "Copié"; setTimeout(() => { copier.textContent = "Copier"; }, 1200);
    }).catch(() => {});
    const enregistrer = el("button", { type: "button", textContent: "Enregistrer", title: "Télécharge les tonalités et les codes dans un fichier texte" });
    enregistrer.onclick = () => {
      const d = new Date(), h = new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19).replace(/[-:]/g, "").replace("T", "_");
      const a = el("a", { href: URL.createObjectURL(new Blob([texte() + "\n"], { type: "text/plain" })), download: `tonalites_${h}.txt` });
      a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    };
    const actions = el("div", { className: "scanner-choix cw-actions" }, effacer, copier, enregistrer);
    const contenu = el("div", { className: "cw-contenu aprs-contenu" }, this.statut, this.lCtcss, this.lDetail, this.lDtmf, this.diag, this.liste, actions);
    const { panneau, dialogue } = panneauAgrandissable(this.ctx, { titre: Tonalites.nom, id: "tonalites-titre", contenu, actions,
      tailleDefaut: 1.1, attenteTexte: "Tonalités affichées dans la fenêtre agrandie." });
    this.dialogue = dialogue;
    this.minuterie = setInterval(() => this.rafraichir(), 200);
    this.rafraichir();
    return panneau;
  }

  rafraichir() {
    if (this.sequence && performance.now() - this.derniereTouche > 3000) this.clore();
    const m = this.moteur, c = m?.ctcss, en1750 = m?.ton1750 != null;
    const maj = (e, t) => { if (e.textContent !== t) e.textContent = t; };
    maj(this.statut, performance.now() - this.derniersIQ > 1500 ? "En attente des échantillons de la clé."
      : !this.couvert ? "Le VFO est trop près du bord de la bande reçue."
      : c || en1750 ? "Tonalité reçue."
      : "Accordez le VFO sur un relais (sortie ou entrée) ou une fréquence FM. Le mode n'a pas d'importance : le module démodule lui-même.");
    const v = x => x.replace(".", ",");
    maj(this.lCtcss, c ? `CTCSS : ${c.norme !== null ? v(c.norme.toFixed(1)) + " Hz" : v(c.f.toFixed(1)) + " Hz (hors norme)"}` : "CTCSS : —");
    const det = [];
    if (c) det.push(`mesuré ${v(c.f.toFixed(2))} Hz · déviation ≈ ${Math.round(c.deviation)} Hz`);
    if (en1750) det.push("tonalité 1750 Hz en cours");
    maj(this.lDetail, det.join(" · "));
    maj(this.lDtmf, `DTMF : ${this.sequence || "—"}`);
    if (this.crete !== null && m) {
      const a = m.mesure ? ` · raie ${v(m.mesure.f.toFixed(1))} Hz, ${Math.round(100 * m.mesure.part)} %` : "";
      maj(this.diag, `crête ${Math.round(this.crete - this.bruit)} dB${a} · ${m.touches} touches`);
    }
  }

  arreter() { clearInterval(this.minuterie); if (this.dialogue?.open) this.dialogue.close(); }
}


const enMo = o => o >= 1e9 ? `${(o / 1e9).toFixed(2).replace(".", ",")} Go` : `${Math.round(o / 1e6)} Mo`;
const enDuree = s => { s = Math.floor(s); return `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, "0")} s`; };
const enMHz = f => `${(f / 1e6).toFixed(4).replace(".", ",")} MHz`;

class EnregistreurIQ {
  static nom = "Enregistreur IQ";
  static description = "Enregistre toute la bande reçue (IQ, pas seulement le son) dans un WAV que SDR#, HDSDR, SDR++ ou SDR Modulaire savent lire, puis la rejoue : on démodule après coup n'importe quelle station.";

  demarrer() {
    this.actif = false; this.dossier = null; this.lecture = null;
    this.ctx.abonner("iq", (re, im, centre, taux) => this.recevoir(re, im, centre, taux));
  }

  // ------------------------------------------------------------- enregistrement
  async commencer() {
    this.erreur = "";
    if (this.actif) return;
    if (window.showDirectoryPicker && !this.dossier) {
      try { this.dossier = await showDirectoryPicker({ id: "sdr-web-iq", mode: "readwrite", startIn: "downloads" }); }
      catch (e) { if (e.name === "AbortError") return; this.dossier = null; }
    }
    Object.assign(this, { actif: true, octets: 0, perdus: 0, debut: performance.now(), fichier: null, chaine: Promise.resolve(), attente: 0 });
    this.majBouton();
  }

  recevoir(re, im, centre, taux) {
    if (!this.actif) return;
    const o = iqVersOctets(re, im);
    const f = this.fichier;
    const limite = this.dossier ? LIMITE_WAV : this.limiteMemoire();
    if (!f || f.centre !== centre || f.taux !== taux || f.octets + o.length > limite) {
      const plein = f && !this.dossier && f.octets + o.length > limite;
      if (f) { this.fichier = null; this.clore(f); }
      if (plein) { this.arreterEnr(); this.erreur = "Limite de la mémoire atteinte : enregistrement arrêté et téléchargé."; return; }
      this.fichier = this.ouvrir(centre, taux);
    }
    if (this.attente > 64e6) { this.perdus++; return; }      // le disque ne suit pas
    const g = this.fichier;
    g.octets += o.length; this.octets += o.length;
    if (g.morceaux) { g.morceaux.push(o); return; }
    this.attente += o.length;
    this.chaine = this.chaine.then(() => g.flux.then(w => w.write(o))).then(() => { this.attente -= o.length; })
      .catch(e => { this.erreur = `Écriture impossible : ${e.message}`; this.arreterEnr(); });
  }

  limiteMemoire() { return 1e9; }

  ouvrir(centre, taux) {
    const debut = new Date(), nom = nomFichier(debut, centre);
    const f = { nom, centre, taux, debut, octets: 0 };
    if (this.dossier) {
      f.flux = this.dossier.getFileHandle(nom, { create: true }).then(h => h.createWritable())
        .then(async w => { await w.write(enteteWav(taux, centre, debut, debut, 0)); return w; });
    } else f.morceaux = [enteteWav(taux, centre, debut, debut, 0)];
    this.dernier = nom;
    return f;
  }

  clore(f) {
    const entete = enteteWav(f.taux, f.centre, f.debut, new Date(), f.octets);
    if (f.morceaux) {
      f.morceaux[0] = entete;
      const a = el("a", { href: URL.createObjectURL(new Blob(f.morceaux, { type: "audio/wav" })), download: f.nom });
      a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 60000);
      f.morceaux = null;
    } else {
      this.chaine = this.chaine.then(() => f.flux).then(async w => { await w.write({ type: "write", position: 0, data: entete }); await w.close(); })
        .catch(e => { this.erreur = `Fermeture impossible : ${e.message}`; });
    }
  }

  arreterEnr() {
    if (!this.actif) return;
    this.actif = false;
    if (this.fichier) this.clore(this.fichier);
    this.fichier = null;
    this.majBouton();
  }

  majBouton() { if (this.bEnr) this.bEnr.textContent = this.actif ? "■ Arrêter l'enregistrement" : "● Enregistrer"; }

  // ------------------------------------------------------------- relecture
  async rejouer(fichier) {
    try {
      const info = await lireEnteteWav(fichier);
      this.arreterEnr();
      const s = new SourceFichierIQ(fichier, info);
      s.boucle = this.boucle.checked;
      if (await this.ctx.rejouer(s)) this.lecture = s;
    } catch (e) { this.erreur = `Relecture impossible : ${e.message}`; }
  }

  creerPanneau() {
    this.statut = el("div", { className: "petit" });
    this.bEnr = el("button", { type: "button", title: "Enregistre toute la bande reçue (2 octets par échantillon). Chrome demande d'abord un dossier." });
    this.bEnr.onclick = () => this.actif ? this.arreterEnr() : this.commencer();
    this.majBouton();
    const choisir = el("input", { type: "file", accept: ".wav,audio/wav" });
    choisir.style.display = "none";
    choisir.onchange = () => { if (choisir.files[0]) this.rejouer(choisir.files[0]); choisir.value = ""; };
    const bRejouer = el("button", { type: "button", textContent: "Rejouer un fichier…", title: "WAV IQ 8 ou 16 bits : SDR Web, SDR Modulaire, SDR#, HDSDR, SDR++" });
    bRejouer.onclick = () => choisir.click();
    this.bStop = el("button", { type: "button", textContent: "Arrêter la relecture", title: "Reconnectez ensuite la clé avec « Connecter la clé »" });
    this.bStop.onclick = () => { if (this.ctx.source() === this.lecture) this.ctx.arreterSource(); this.lecture = null; };
    this.boucle = el("input", { type: "checkbox", checked: this.ctx.lire("boucle", true) });
    this.boucle.onchange = () => { this.ctx.ecrire("boucle", this.boucle.checked); if (this.lecture) this.lecture.boucle = this.boucle.checked; };
    const boucle = el("label", { className: "sstv-auto" }, this.boucle, "En boucle");
    this.position = el("input", { type: "range", min: 0, max: 1000, value: 0, title: "Position dans l'enregistrement" });
    this.position.style.width = "100%";
    this.position.oninput = () => { this.glisse = true; };
    this.position.onchange = () => { this.lecture?.aller(this.position.value / 1000); this.glisse = false; };
    this.lLecture = el("div", { className: "petit" });
    const actions = el("div", { className: "scanner-choix cw-actions" }, this.bEnr, bRejouer, this.bStop, boucle, choisir);
    this.minuterie = setInterval(() => this.rafraichir(), 300);
    this.rafraichir();
    return el("div", {}, this.statut, actions, this.position, this.lLecture);
  }

  rafraichir() {
    const maj = (e, t) => { if (e.textContent !== t) e.textContent = t; };
    if (this.actif) {
      const ou = this.dossier ? `dossier « ${this.dossier.name} »` : `mémoire (téléchargé à l'arrêt, ${enMo(this.limiteMemoire())} au plus)`;
      maj(this.statut, `● ${enDuree((performance.now() - this.debut) / 1000)} · ${enMo(this.octets)}${this.perdus ? ` · ${this.perdus} blocs perdus` : ""} · ${ou}\n${this.dernier ?? ""}`);
      this.statut.style.color = "#ff5252"; this.statut.style.whiteSpace = "pre-line";
    } else {
      this.statut.style.color = "";
      const t = this.ctx.taux();
      maj(this.statut, this.erreur || `Prêt : ${enMo(2 * t * 60)} par minute à ${t / 1e6} MS/s.${this.dernier ? ` Dernier : ${this.dernier}` : ""}`);
    }
    const s = this.lecture && this.ctx.source() === this.lecture ? this.lecture : null;
    if (!s && this.lecture && !this.ctx.source()) this.lecture = null;
    this.bStop.disabled = !s; this.position.disabled = !s;
    if (s) {
      if (!this.glisse) this.position.value = Math.round(1000 * s.pos / Math.max(1, s.info.n - 1));
      maj(this.lLecture, `▶ ${s.fichier.name} · ${enMHz(s.centre)} · ${enDuree(s.pos / s.taux)} / ${enDuree(s.info.n / s.taux)}${s.info.heure ? ` · enregistré le ${s.info.heure}` : ""}${s.fini ? " · fin de l'enregistrement" : ""}`);
    } else maj(this.lLecture, "");
  }

  arreter() { this.arreterEnr(); clearInterval(this.minuterie); }
}

class DecodeurACARS {
  static nom = "Décodeur ACARS";
  static description = "Messages ACARS des avions et des stations sol (VHF AM, 131 MHz) : immatriculation, vol, label, texte. Fréquences préenregistrées (Europe, Amérique du Nord, Japon…) décodées ensemble.";

  demarrer() {
    this.choix = this.ctx.lire("liste", 0);
    if (!LISTES_ACARS[this.choix]) this.choix = 0;
    this.cle = ""; this.moteur = null; this.derniersIQ = -Infinity; this.avions = new Map(); this.tout = [];
    this.ctx.abonner("iq", (re, im, centre, taux) => this.recevoir(re, im, centre, taux));
  }

  frequences() { const l = LISTES_ACARS[this.choix][1]; return l.length ? l : [this.ctx.frequence()]; }

  recevoir(re, im, centre, taux) {
    this.derniersIQ = performance.now();
    const fs = this.frequences(), cle = `${centre}/${taux}/${fs.join(",")}`;
    if (cle !== this.cle) { this.cle = cle; this.moteur = new MoteurACARS(fs, centre, taux); }
    for (const m of this.moteur.traiter(re, im)) this.message(m);
  }

  message(m) {
    if (m.immat) this.avions.set(m.immat, performance.now());
    const ligne = `${new Date().toTimeString().slice(0, 8)}  ${texteACARS(m)}`;
    this.tout.push(ligne);
    if (this.tout.length > 2000) this.tout.shift();
    if (!this.liste || (!m.texte.trim() && !this.vides.checked)) return;
    this.liste.prepend(el("li", { textContent: ligne }));
    while (this.liste.childElementCount > 500) this.liste.lastElementChild.remove();
  }

  accorder() {
    const fs = LISTES_ACARS[this.choix][1];
    if (!fs.length) return;
    this.ctx.reglerMode("AM");
    this.ctx.reglerFrequence(fs[0] + this.ctx.taux());     // la clé se recentre…
    this.ctx.reglerFrequence(fs[0]);                       // … et couvre la première fréquence et ses voisines
  }

  creerPanneau() {
    const choix = el("select", { title: "Fréquences décodées : toutes celles de la liste qui tiennent dans la bande reçue" });
    LISTES_ACARS.forEach(([nom], i) => choix.add(new Option(nom, i, false, i === this.choix)));
    choix.onchange = () => { this.choix = Number(choix.value); this.ctx.ecrire("liste", this.choix); };
    const accorder = el("button", { type: "button", textContent: "Accorder", title: "Met la clé sur ces fréquences (VFO sur la première, en AM pour écouter)" });
    accorder.onclick = () => this.accorder();
    const haut = el("div", { className: "scanner-choix" }, choix, accorder);
    this.statut = el("div", { className: "petit" });
    this.diag = el("div", { className: "petit", title: "Par fréquence : niveau du canal au-dessus du bruit et messages reçus ; avions entendus dans les 10 dernières minutes" });
    this.diag.style.color = "var(--doux)";
    this.liste = el("ul", { className: "journal aprs-liste" });
    this.liste.style.whiteSpace = "pre-wrap";
    const effacer = el("button", { type: "button", textContent: "Effacer" });
    effacer.onclick = () => { this.liste.replaceChildren(); this.tout = []; };
    const copier = el("button", { type: "button", textContent: "Copier" });
    copier.onclick = () => navigator.clipboard?.writeText(this.tout.join("\n")).then(() => {
      copier.textContent = "Copié"; setTimeout(() => { copier.textContent = "Copier"; }, 1200);
    }).catch(() => {});
    const enregistrer = el("button", { type: "button", textContent: "Enregistrer", title: "Télécharge tous les messages reçus (vides compris) dans un fichier texte" });
    enregistrer.onclick = () => {
      const d = new Date(), h = new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19).replace(/[-:]/g, "").replace("T", "_");
      const a = el("a", { href: URL.createObjectURL(new Blob([this.tout.join("\n") + "\n"], { type: "text/plain" })), download: `acars_${h}.txt` });
      a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    };
    this.vides = el("input", { type: "checkbox", checked: this.ctx.lire("vides", false) });
    this.vides.onchange = () => this.ctx.ecrire("vides", this.vides.checked);
    const vides = el("label", { className: "sstv-auto", title: "Affiche aussi les accusés et messages sans texte (label _d, essais de liaison…)" }, this.vides, "Messages vides");
    const actions = el("div", { className: "scanner-choix cw-actions" }, effacer, copier, enregistrer, vides);
    const contenu = el("div", { className: "cw-contenu aprs-contenu" }, haut, this.statut, this.diag, this.liste, actions);
    const { panneau, dialogue } = panneauAgrandissable(this.ctx, { titre: DecodeurACARS.nom, id: "acars-titre", contenu, actions,
      tailleDefaut: 1.0, attenteTexte: "Messages affichés dans la fenêtre agrandie." });
    this.dialogue = dialogue;
    this.minuterie = setInterval(() => this.rafraichir(), 300);
    this.rafraichir();
    return panneau;
  }

  rafraichir() {
    const m = this.moteur, maj = (e, t) => { if (e.textContent !== t) e.textContent = t; };
    maj(this.statut, performance.now() - this.derniersIQ > 1500 ? "En attente des échantillons de la clé."
      : !m?.canaux.length ? "Aucune de ces fréquences n'est dans la bande reçue : cliquez sur « Accorder »."
      : `Décodage de ${m.canaux.map(c => mhzACARS(c.frequence)).join(", ")} MHz.${m.dehors.length ? ` Hors de la bande : ${m.dehors.map(mhzACARS).join(", ")} MHz.` : ""}`);
    if (m?.canaux.length) {
      const avions = [...this.avions.values()].filter(t => performance.now() - t < 600000).length;
      maj(this.diag, m.canaux.map(c => `${mhzACARS(c.frequence)} : ${Math.round((c.niveau ?? -99) - (c.bruit ?? -99))} dB, ${c.messages} msg`).join(" · ") + ` · ${avions} avion(s)`);
    }
  }

  arreter() { clearInterval(this.minuterie); if (this.dialogue?.open) this.dialogue.close(); }
}

class FaxMeteo {
  static nom = "Fax météo (WEFAX)";
  static description = "Cartes météo en ondes courtes (DWD Pinneberg, Northwood, Athènes, Boston…) : 120 lignes/min, IOC 576, calage automatique sur le phasage, PNG téléchargés. Station choisie dans une liste.";

  demarrer() {
    this.decodeur = new DecodeurFax();
    this.choix = this.ctx.lire("station", 1);
    if (this.choix > STATIONS_FAX.length) this.choix = 1;
    this.cle = ""; this.derniersIQ = -Infinity; this.couvert = false; this.niveau = null; this.bruit = null;
    this.ctx.abonner("iq", (re, im, centre, taux) => this.recevoir(re, im, centre, taux));
  }

  frequence() { return this.choix < STATIONS_FAX.length ? STATIONS_FAX[this.choix][1] : this.ctx.frequence() + DECALAGE_USB; }

  recevoir(re, im, centre, taux) {
    this.derniersIQ = performance.now();
    const f = this.frequence();
    this.couvert = Math.abs(f - centre) + 3000 < 0.42 * taux;
    if (!this.couvert) { this.cle = ""; return; }
    const cle = `${centre}/${taux}`;
    if (cle !== this.cle) {
      this.cle = cle;
      const d1 = Math.round(taux / 240_000), fs1 = taux / d1;
      this.nco = new NCO(taux);
      this.etage1 = new FirComplexe(passeBas(taux, 30_000, d1 * 4 + 1), d1);
      this.etage2 = new FirComplexe(passeBas(fs1, 6_000, 61), Math.round(fs1 / 48_000));
      this.etage3 = new FirComplexe(passeBas(48_000, 2_500, 49), 4);
    }
    this.nco.freq = f - centre;
    const [a, b] = this.etage3.traiter(...this.etage2.traiter(...this.etage1.traiter(...this.nco.melanger(re, im))));
    if (!a.length) return;
    let p = 0;
    for (let k = 0; k < a.length; k++) p += a[k] * a[k] + b[k] * b[k];
    p = 10 * Math.log10(p / a.length + 1e-20);
    this.bruit = this.bruit === null ? p : Math.min(p, this.bruit + 0.02);
    this.niveau = this.niveau === null ? p : 0.9 * this.niveau + 0.1 * p;
    this.decodeur.traiter(a, b);
  }

  accorder() {
    if (this.choix >= STATIONS_FAX.length) return;
    const f = this.frequence() - DECALAGE_USB;
    this.ctx.reglerMode("USB");
    this.ctx.reglerFrequence(f + this.ctx.taux());      // la clé se recentre…
    this.ctx.reglerFrequence(f);                        // … VFO 1,9 kHz sous la fréquence annoncée
  }

  nomFichier(partielle) {
    const d = new Date(), h = new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19).replace(/[-:]/g, "").replace("T", "_");
    return `fax_${h}_${String(this.frequence() / 1000).replace(".", ",")}kHz${partielle ? "_partielle" : ""}.png`;
  }

  telecharger(lignes, partielle) {
    if (!lignes.length) return;
    const c = el("canvas", { width: LARGEUR_FAX, height: lignes.length }), g = c.getContext("2d"), img = g.createImageData(LARGEUR_FAX, lignes.length);
    lignes.forEach((r, k) => { for (let i = 0; i < LARGEUR_FAX; i++) { const o = 4 * (k * LARGEUR_FAX + i); img.data[o] = img.data[o + 1] = img.data[o + 2] = r[i]; img.data[o + 3] = 255; } });
    g.putImageData(img, 0, 0);
    c.toBlob(b => { const a = el("a", { href: URL.createObjectURL(b), download: this.nomFichier(partielle) }); a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 10000); }, "image/png");
  }

  creerPanneau() {
    const choix = el("select", { title: "Station décodée : le module extrait son canal des IQ, quel que soit le mode du VFO" });
    STATIONS_FAX.forEach(([nom, f], i) => choix.add(new Option(`${nom} · ${String(f / 1000).replace(".", ",")} kHz`, i, false, i === this.choix)));
    choix.add(new Option("Fréquence du VFO (USB, +1,9 kHz)", STATIONS_FAX.length, false, this.choix === STATIONS_FAX.length));
    choix.onchange = () => { this.choix = Number(choix.value); this.ctx.ecrire("station", this.choix); this.decodeur.reinitialiser(); };
    const accorder = el("button", { type: "button", textContent: "Accorder", title: "VFO en USB 1,9 kHz sous la fréquence annoncée, pour écouter la station" });
    accorder.onclick = () => this.accorder();
    this.toile = el("canvas", { width: LARGEUR_FAX, height: 1, title: "Cliquez sur le bord gauche réel de la carte pour la recaler" });
    this.toile.style.cssText = "width: 100%; display: block; background: #000; cursor: crosshair";
    this.toile.onclick = e => { const r = this.toile.getBoundingClientRect(); this.decodeur.reglerBordGauche((e.clientX - r.left) / r.width); };
    this.cadre = el("div", {}, this.toile);
    this.cadre.style.cssText = "max-height: 260px; overflow: auto; background: #000";
    this.statut = el("div", { className: "petit" });
    this.diag = el("div", { className: "petit", title: "Niveau du canal au-dessus du bruit, fréquence moyenne mesurée (0 = fréquence annoncée ; blanc +400 Hz, noir −400 Hz)" });
    this.diag.style.color = "var(--doux)";
    const depart = el("button", { type: "button", textContent: "Démarrer", title: "Commence une carte maintenant (signal de départ manqué) ; cliquez ensuite sur son bord gauche" });
    depart.onclick = () => this.decodeur.demarrerMaintenant();
    this.bFin = el("button", { type: "button", textContent: "Terminer", title: "Clôt la carte en cours et la télécharge" });
    this.bFin.onclick = () => this.decodeur.terminerMaintenant();
    this.pente = el("input", { type: "number", step: 10, min: -2000, max: 2000, value: 0, title: "Pente en ppm (écart d'horloge avec l'émetteur), réglée au phasage ; à corriger si la carte penche" });
    this.pente.style.width = "5.5em";
    this.pente.onchange = () => this.decodeur.reglerPente(Number(this.pente.value) || 0);
    const inverse = el("input", { type: "checkbox", title: "Échange noir et blanc (station reçue en LSB)" });
    inverse.onchange = () => { this.decodeur.inverse = inverse.checked; };
    this.auto = el("input", { type: "checkbox", checked: this.ctx.lire("auto", true) });
    this.auto.onchange = () => this.ctx.ecrire("auto", this.auto.checked);
    const enregistrer = el("button", { type: "button", textContent: "Enregistrer", title: "Télécharge la carte en cours en PNG" });
    enregistrer.onclick = () => this.telecharger(this.decodeur.lignes, true);
    const actions = el("div", { className: "scanner-choix cw-actions" }, depart, this.bFin, el("label", {}, "Pente ", this.pente, " ppm"),
      el("label", { className: "sstv-auto" }, inverse, "Inverser"), el("label", { className: "sstv-auto", title: "Télécharge chaque carte terminée en PNG" }, this.auto, "Télécharger chaque carte"), enregistrer);
    const aide = el("div", { className: "petit", textContent: "Ondes courtes : clé qui reçoit la HF (RTL-SDR Blog V4, échantillonnage direct) ou convertisseur. Horaires : programme de chaque station (DWD : dwd.de, « Seefunk »)." });
    aide.style.color = "var(--doux)";
    const contenu = el("div", { className: "cw-contenu" }, el("div", { className: "scanner-choix" }, choix, accorder), this.cadre, this.statut, this.diag, actions, aide);
    const { panneau, dialogue } = panneauAgrandissable(this.ctx, { titre: FaxMeteo.nom, id: "fax-titre", contenu, actions,
      tailleDefaut: 1.0, attenteTexte: "Carte affichée dans la fenêtre agrandie.", tailleTexte: false });
    this.dialogue = dialogue;
    this.vue = { version: -1, lignes: 0 };
    this.minuterie = setInterval(() => this.rafraichir(), 500);
    this.rafraichir();
    return panneau;
  }

  dessiner() {
    const d = this.decodeur, n = d.lignes.length, g = this.toile.getContext("2d");
    let debut = d.tout ? 0 : this.vue.lignes;
    const hauteur = Math.max(1, Math.ceil(n / 50) * 50);
    if (this.toile.height !== hauteur) { this.toile.height = hauteur; debut = 0; }
    if (debut === 0) { g.fillStyle = "#000"; g.fillRect(0, 0, LARGEUR_FAX, hauteur); }
    if (n > debut) {
      const img = g.createImageData(LARGEUR_FAX, n - debut);
      for (let k = debut; k < n; k++) {
        const r = d.lignes[k];
        for (let i = 0; i < LARGEUR_FAX; i++) { const o = 4 * ((k - debut) * LARGEUR_FAX + i); img.data[o] = img.data[o + 1] = img.data[o + 2] = r[i]; img.data[o + 3] = 255; }
      }
      g.putImageData(img, 0, debut);
      const bas = this.cadre.scrollTop + this.cadre.clientHeight >= this.cadre.scrollHeight - 30;
      if (bas) this.cadre.scrollTop = this.cadre.scrollHeight;
    }
    d.tout = false; this.vue.lignes = n;
  }

  rafraichir() {
    const d = this.decodeur, maj = (e, t) => { if (e.textContent !== t) e.textContent = t; };
    for (const t of d.terminees.splice(0)) if (this.auto.checked || t.partielle) this.telecharger(t.lignes, t.partielle);
    if (d.version !== this.vue.version) { this.vue.version = d.version; if (d.lignes.length || d.tout) this.dessiner(); }
    const khz = String(this.frequence() / 1000).replace(".", ",");
    maj(this.statut, performance.now() - this.derniersIQ > 1500 ? "En attente des échantillons de la clé."
      : !this.couvert ? `${khz} kHz n'est pas dans la bande reçue : cliquez sur « Accorder » (clé qui reçoit la HF ou convertisseur).`
      : d.statut());
    if (document.activeElement !== this.pente) this.pente.value = Math.round(d.pentePpm);
    this.bFin.disabled = !d.enCours;
    if (this.couvert && this.niveau !== null)
      maj(this.diag, `niveau ${Math.round(this.niveau - this.bruit)} dB${d.frequence !== null ? ` · fréquence ${d.frequence >= 0 ? "+" : ""}${Math.round(d.frequence)} Hz` : ""}`);
  }

  arreter() { clearInterval(this.minuterie); if (this.dialogue?.open) this.dialogue.close(); }
}

class DecodeurAIS {
  static nom = "Décodeur AIS (navires)";
  static description = "Navires et stations côtières (AIS, 161,975 et 162,025 MHz décodés ensemble) : nom, MMSI, position, vitesse, route, type, destination ; carte et tableau.";

  demarrer() {
    this.recus = []; this.nombre = 0; this.cle = ""; this.moteur = null; this.derniersIQ = -Infinity; this.saturation = 0;
    this.navires = new Map();
    // Station d'écoute : la sienne, ou celle des modules APRS ou ADS-B.
    const autre = nom => { try { return JSON.parse(localStorage.getItem(`sdr-web/${nom}/station`)); } catch { return null; } };
    this.station = this.ctx.lire("station", null) ?? autre("Décodeur APRS") ?? autre("ADS-B (avions)");
    this.ctx.abonner("iq", (re, im, centre, taux) => this.recevoir(re, im, centre, taux));
  }

  recevoir(re, im, centre, taux) {
    this.derniersIQ = performance.now();
    let sat = 0;
    for (let k = 0; k < re.length; k += 4) if (re[k] > 0.95 || re[k] < -0.95 || im[k] > 0.95 || im[k] < -0.95) sat++;
    this.saturation = sat / Math.ceil(re.length / 4);
    const cle = `${centre}/${taux}`;
    if (cle !== this.cle) { this.cle = cle; this.moteur = new MoteurAIS(centre, taux); }
    for (const [canal, o] of this.moteur.traiter(re, im)) {
      const m = decoderAIS(octetsVersBits(o));
      if (m) { this.recus.push([canal, m]); this.mettreAJour(m); }
    }
  }

  mettreAJour(m) {
    const maintenant = Date.now();
    let n = this.navires.get(m.mmsi);
    if (!n) { n = { mmsi: m.mmsi, nb: 0, trace: [] }; this.navires.set(m.mmsi, n); }
    n.vu = maintenant; n.nb++;
    for (const c of ["nom", "indicatif", "type_navire", "destination", "classe", "longueur", "largeur", "imo"])
      if (m[c] !== undefined && m[c] !== "" && m[c] !== 0) n[c] = m[c];
    if (m.vitesse !== undefined) n.vitesse_nd = m.vitesse === 1023 ? null : m.vitesse / 10;
    if (m.route !== undefined || m.cap !== undefined)
      n.route_deg = m.cap !== undefined && m.cap < 360 ? m.cap : m.route !== undefined && m.route < 3600 ? m.route / 10 : null;
    if (m.statut !== undefined) n.statut = m.statut;
    if (m.position) {
      [n.lat, n.lon] = m.position;
      const der = n.trace[n.trace.length - 1];
      if (!der || der[0] !== n.lat || der[1] !== n.lon) { n.trace.push([n.lat, n.lon, maintenant]); if (n.trace.length > 200) n.trace.shift(); }
    }
    n.couleur = couleurNavire(n);
    this.carteAJour = true;
  }

  accorder() {
    const f = CANAUX_AIS[0][0];
    this.ctx.reglerMode("NFM");
    this.ctx.reglerFrequence(f + this.ctx.taux());      // la clé se recentre…
    this.ctx.reglerFrequence(f);                        // … et couvre les deux canaux
  }

  creerPanneau() {
    this.resume = el("div", { className: "petit" });
    const accorder = el("button", { type: "button", textContent: "Accorder", title: "Met la clé sur les deux canaux AIS (VFO sur 161,975 MHz en NFM pour écouter)" });
    accorder.onclick = () => this.accorder();
    this.statut = el("div", { className: "petit" });
    this.diag = el("div", { className: "petit", title: "Par canal : crête au-dessus du bruit, messages reçus, décalage de fréquence mesuré (corrigez le ppm de la clé s'il dépasse 2 kHz)" });
    this.diag.style.color = "var(--doux)";
    this.liste = el("ul", { className: "journal aprs-liste" });
    const effacer = el("button", { type: "button", textContent: "Effacer" });
    effacer.onclick = () => this.liste.replaceChildren();
    const texte = () => [...this.liste.children].reverse().map(li => li.textContent).join("\n");
    const copier = el("button", { type: "button", textContent: "Copier" });
    copier.onclick = () => navigator.clipboard?.writeText(texte()).then(() => {
      copier.textContent = "Copié"; setTimeout(() => { copier.textContent = "Copier"; }, 1200);
    }).catch(() => {});
    const enregistrer = el("button", { type: "button", textContent: "Enregistrer", title: "Télécharge les messages reçus dans un fichier texte" });
    enregistrer.onclick = () => {
      const d = new Date(), h = new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19).replace(/[-:]/g, "").replace("T", "_");
      const a = el("a", { href: URL.createObjectURL(new Blob([texte() + "\n"], { type: "text/plain" })), download: `ais_${h}.txt` });
      a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    };
    const ouvrirCarte = el("button", { type: "button", textContent: "Carte", title: "Navires sur une carte, avec le tableau" });
    ouvrirCarte.setAttribute("aria-haspopup", "dialog");
    ouvrirCarte.onclick = () => this.ouvrirCarte();
    this.creerCarte();
    const actions = el("div", { className: "scanner-choix cw-actions" }, ouvrirCarte, effacer, copier, enregistrer);
    const contenu = el("div", { className: "cw-contenu aprs-contenu" },
      el("div", { className: "scanner-choix" }, this.resume, accorder), this.statut, this.diag, this.liste, actions);
    const { panneau, dialogue } = panneauAgrandissable(this.ctx, { titre: DecodeurAIS.nom, id: "ais-titre", contenu, actions,
      tailleDefaut: 1.1, attenteTexte: "Messages affichés dans la fenêtre agrandie." });
    this.dialogue = dialogue;
    panneau.append(this.fenCarte);
    this.minuterie = setInterval(() => this.rafraichir(), 250);
    this.rafraichir();
    return panneau;
  }

  creerCarte() {
    const titre = el("h2", { id: "ais-carte-titre", textContent: "AIS · navires reçus" });
    titre.title = "Tirer la barre pour déplacer · coin en bas à droite pour agrandir";
    this.resumeCarte = el("span", { className: "adsb-resume" });
    const fermer = el("button", { type: "button", textContent: "Fermer" });
    const recentrer = el("button", { type: "button", textContent: "Recentrer" });
    const toile = el("canvas", { className: "adsb-carte" });
    toile.setAttribute("aria-label", "Carte des navires AIS reçus");
    this.tableau = el("tbody");
    const table = el("table", { className: "adsb-table" },
      el("thead", {}, el("tr", {}, ...["Navire", "MMSI", "Type", "Vitesse", "Route", "Dist.", "Vu", "Destination"].map(t => el("th", { textContent: t })))), this.tableau);
    const st = this.station;
    const lat = el("input", { type: "number", step: "0.0001", min: -90, max: 90, placeholder: "43.6960", value: st?.lat ?? "" });
    const lon = el("input", { type: "number", step: "0.0001", min: -180, max: 180, placeholder: "7.2700", value: st?.lon ?? "" });
    const placer = el("button", { type: "button", textContent: "Enregistrer la station" });
    placer.onclick = () => {
      const la = parseFloat(lat.value), lo = parseFloat(lon.value);
      this.station = Number.isFinite(la) && Number.isFinite(lo) ? { lat: la, lon: lo } : null;
      this.ctx.ecrire("station", this.station); this.carte.station = this.station; this.carte.centrer(); this.majTableau();
    };
    const pied = el("div", { className: "adsb-pied" },
      el("label", {}, "Station : lat. ", lat), el("label", {}, " lon. ", lon), placer,
      el("span", { className: "petit", textContent: "Facultatif : sert aux distances et aux cercles (reprise des modules APRS ou ADS-B si déjà saisie)." }));
    const entete = el("header", {}, titre, this.resumeCarte, el("span", { className: "adsb-boutons" }, recentrer, fermer));
    this.fenCarte = el("dialog", { className: "adsb-dialogue" }, entete,
      el("div", { className: "adsb-corps" }, toile, el("div", { className: "adsb-cote" }, table)), pied);
    this.fenCarte.setAttribute("aria-labelledby", titre.id);
    this.afficherCarte = fenetreFlottante(this.fenCarte, entete, this.ctx, "carte");
    this.carte = new CarteRadar(toile, { surChoix: id => { this.choisi = id; this.majTableau(); } });
    this.carte.station = this.station;
    fermer.onclick = () => this.fenCarte.close();
    recentrer.onclick = () => this.carte.centrer();
  }

  places() { return [...this.navires.values()].filter(n => n.lat !== undefined); }

  ouvrirCarte() {
    if (this.fenCarte.open) return;
    this.afficherCarte(); this.carte.navires = this.places(); this.carte.centrer(); this.majTableau();
  }

  trier() {
    const st = this.station;
    const cle = n => st ? (n.lat !== undefined ? distanceKm(st.lat, st.lon, n.lat, n.lon) : 1e9) : -n.vu;
    return [...this.navires.values()].sort((a, b) => cle(a) - cle(b));
  }

  majTableau() {
    const st = this.station, v = x => String(x).replace(".", ",");
    this.tableau.replaceChildren(...this.trier().map(n => {
      const type = ["station de base", "aide à la navigation"].includes(n.classe) ? n.classe : n.type_navire ? categorieAIS(n.type_navire) : "";
      const tr = el("tr", { className: n.mmsi === this.choisi ? "choisi" : "" },
        el("td", {}, el("i", { className: "adsb-pastille" }), n.nom || (n.classe === "station de base" ? "station côtière" : "—")),
        el("td", { textContent: String(n.mmsi).padStart(9, "0") }),
        el("td", { textContent: type }),
        el("td", { textContent: n.vitesse_nd != null ? `${v(n.vitesse_nd.toFixed(1))} nd` : "" }),
        el("td", { textContent: n.route_deg != null ? `${Math.round(n.route_deg)}°` : "" }),
        el("td", { textContent: st && n.lat !== undefined ? `${v(distanceKm(st.lat, st.lon, n.lat, n.lon).toFixed(1))} km` : "" }),
        el("td", { textContent: new Date(n.vu).toLocaleTimeString("fr-FR") }),
        el("td", { textContent: n.destination || "" }));
      tr.firstChild.firstChild.style.background = n.couleur;
      tr.onclick = () => {
        if (n.lat === undefined) return;
        this.choisi = this.carte.selection = n.mmsi;
        this.carte.lat0 = n.lat; this.carte.lon0 = n.lon; this.carte.dessiner(); this.majTableau();
      };
      return tr;
    }));
  }

  rafraichir() {
    if (this.fenCarte?.open) {
      this.resumeCarte.textContent = `${this.navires.size} navire(s) et station(s) · ${this.places().length} placé(s) · ${this.nombre} message(s)`;
      this.carte.navires = this.places(); this.carte.dessiner();
      if (this.carteAJour) { this.carteAJour = false; this.majTableau(); }
    }
    for (const [canal, m] of this.recus.splice(0)) {
      this.nombre++;
      this.liste.prepend(el("li", { textContent: `${new Date().toLocaleTimeString("fr-FR")}  ${texteAIS(m, canal)}` }));
      if (this.liste.children.length > 500) this.liste.lastChild.remove();
    }
    const maj = (e, t) => { if (e.textContent !== t) e.textContent = t; };
    const actifs = [...this.navires.values()].filter(n => Date.now() - n.vu < 600000).length;
    maj(this.resume, `${actifs} navire(s) ou station(s) en 10 min · ${this.nombre} message(s)`);
    const m = this.moteur, v = x => String(x).replace(".", ",");
    const sat = this.saturation > 0.001;
    maj(this.statut, performance.now() - this.derniersIQ > 1500 ? "Démarrez la réception : le décodeur lit les IQ de la clé."
      : !m?.canaux.length ? "Les canaux AIS (161,975 et 162,025 MHz) ne sont pas dans la bande reçue : cliquez sur « Accorder »."
      : `Écoute de ${m.canaux.map(c => `AIS ${c.nom}`).join(" et ")}${m.dehors.length ? ` · hors de la bande : ${m.dehors.map(([f]) => v((f / 1e6).toFixed(3))).join(", ")} MHz` : ""}`
        + (sat ? ` · SATURATION ${v((100 * this.saturation).toFixed(1))} % : baissez le gain RF` : ""));
    if (m?.canaux.length && performance.now() - this.derniersIQ <= 1500)
      maj(this.diag, m.canaux.map(c => `AIS ${c.nom} : crête +${Math.round(c.crete - c.bruit)} dB, ${c.messages} msg`
        + (c.decalage !== null ? `, décalage ${c.decalage >= 0 ? "+" : ""}${v((c.decalage / 1000).toFixed(1))} kHz` : "")).join(" · "));
  }

  arreter() { clearInterval(this.minuterie); if (this.dialogue?.open) this.dialogue.close(); if (this.fenCarte?.open) this.fenCarte.close(); }
}

// Vu-mètre égaliseur à LED : calcul et dessin dans vumetre.js (même calcul que modules-a-telecharger/vumetre.py).
class Vumetre {
  static nom = "Vu-mètre égaliseur";
  static description = "L'audio découpé en bandes de fréquence, en colonnes de LED vertes, jaunes et rouges avec crête et reflet, comme un égaliseur de chaîne hi-fi ; colonne VU du niveau global.";
  demarrer() {
    this.moteur = new MoteurVumetre(); this.dernier = -Infinity;
    this.bandes = this.ctx.lire("bandes", 16); this.reflet = this.ctx.lire("reflet", true);
    this.ctx.abonner("audio", (a, taux) => { this.moteur.pousser(a, taux); this.dernier = performance.now(); });
  }
  creerPanneau() {
    this.toile = el("canvas", { className: "vumetre-toile" });
    this.toile.setAttribute("aria-hidden", "true");
    const choix = el("select", { title: "Nombre de bandes" }, ...BANDES_VUMETRE.map(n => new Option(`${n} bandes`, n, false, n === this.bandes)));
    choix.onchange = () => { this.bandes = +choix.value; this.ctx.ecrire("bandes", this.bandes); };
    const caseReflet = el("input", { type: "checkbox", checked: this.reflet });
    caseReflet.onchange = () => { this.reflet = caseReflet.checked; this.ctx.ecrire("reflet", this.reflet); };
    const actions = el("div", { className: "scanner-choix cw-actions vumetre-actions" }, choix, el("label", { className: "petit" }, caseReflet, " Reflet"));
    this.txt = el("div", { className: "petit" });
    const contenu = el("div", { className: "cw-contenu vumetre" }, this.toile, actions, this.txt);
    const { panneau } = panneauAgrandissable(this.ctx, { titre: Vumetre.nom, id: "vumetre-titre", contenu, actions,
      tailleDefaut: 1, attenteTexte: "Vu-mètre affiché dans la fenêtre agrandie.", tailleTexte: false });
    let avant = performance.now(), cle = "";
    const animer = maintenant => {
      this.anim = requestAnimationFrame(animer);
      const dt = Math.min(0.1, (maintenant - avant) / 1000); avant = maintenant;
      const fmax = frequenceMax(this.ctx.mode());
      const etat = this.moteur.image(dt, this.bandes, fmax, maintenant - this.dernier < 500);
      dessinerVumetre(this.toile, etat, this.reflet);
      const c = `${this.bandes}/${fmax}/${this.ctx.mode()}`;
      if (c !== cle) {
        cle = c;
        const b = bordsVumetre(this.bandes, fmax), hz = f => f >= 1000 ? `${(f / 1000).toLocaleString("fr-FR", { maximumFractionDigits: 1 })} kHz` : `${Math.round(f)} Hz`;
        this.txt.textContent = `${this.bandes} bandes de ${hz(b[0])} à ${hz(b[b.length - 1])} (${this.ctx.mode()}) · échelle automatique`;
      }
    };
    this.anim = requestAnimationFrame(animer);
    return panneau;
  }
  arreter() { cancelAnimationFrame(this.anim); }
}

export const MODULES = [SMetre, Vumetre, Memoires, Enregistreur, EnregistreurIQ, Journal, Capteurs433, ModuleStereoRds, ADSB, DecodeurAIS, ScannerMemoires, ScannerFrequences, ModuleSSTV, FaxMeteo, DecodeurCW, DecodeurAPRS, DecodeurFT, DecodeurDMR, DecodeurACARS, Tonalites];
