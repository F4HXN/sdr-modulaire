// SPDX-License-Identifier: MIT
// SDR Web : récepteur SDR dans le navigateur (WebUSB), version web de SDR Modulaire.

import { Chaine, FFT, octetsVersIQ, TAUX, MODES, LARGEURS, TAUX_AUDIO } from "./dsp.js?v=20261122-cw";
import { CleRtlSdr } from "./rtlsdr.js?v=20261122-pll";
import { SourceSimulation } from "./simulation.js?v=20261122-niveau";
import { MODULES } from "./modules.js?v=20261123-sstv2";

const $ = s => document.querySelector(s);
const PAS = [1, 10, 100, 1000, 2500, 5000, 6250, 8330, 10000, 12500, 25000, 100000, 1000000];
const TAILLE_FFT = 2048;

// ---------------------------------------------------------------- réglages persistants
const DEFAUT = { centre: 145_300_000, decalage: 200_000, taux: 960000, mode: "NFM", largeurs: {}, gain: null, ppm: 0,
  agc: true, direct: 0, convertisseur: 0, squelch: -120, volume: 0.5, pas: 12500, moyennage: 0.6, min: -100, max: -20, zoom: 0, modules: ["S-mètre"] };
const R = Object.assign({}, DEFAUT, JSON.parse(localStorage.getItem("sdr-web") || "{}"));
const sauver = () => localStorage.setItem("sdr-web", JSON.stringify(R));
addEventListener("beforeunload", sauver);
const frequence = () => R.centre + R.decalage;
const largeur = () => R.largeurs[R.mode] || LARGEURS[R.mode];

// ---------------------------------------------------------------- bus des modules
const abonnes = { niveau: new Set(), audio: new Set(), iq: new Set(), vfo: new Set(), fm: new Set() };
function publier(sujet, ...args) {
  for (const f of [...abonnes[sujet]]) {
    try { f(...args); } catch (e) { console.error(e); const m = f.module; if (m) { retirerModule(m.nom); statut(`Module « ${m.nom} » retiré après une erreur : ${e.message}`); } }
  }
}

// ---------------------------------------------------------------- moteur
let source = null, chaine = null, fft = null, audio = null, sortie = null, minuterie = null;
const etatDC = { dr: 0, di: 0 };
let moyenne = null, dernierSpectre = null;

// Zoom en fréquence : R.zoom = largeur affichée en Hz (0 = toute la bande). La FFT grandit avec le zoom
// (jusqu'à 65 536 points) pour garder le détail ; on n'affiche que la fenêtre [vue.a, vue.b] (Hz, par rapport
// au centre de la clé). La vue reste fixe pendant l'accord et se recentre si le VFO approche de son bord.
const ZOOMS = [0, 1_000_000, 500_000, 200_000, 100_000, 50_000, 20_000];
const vue = { a: 0, b: 0, centre: 0 };
function tailleFFT(bloc) {
  if (!R.zoom || R.zoom >= R.taux) return TAILLE_FFT;
  let n = TAILLE_FFT; const voulu = TAILLE_FFT * R.taux / R.zoom;
  while (n < voulu && n * 2 <= Math.min(65536, bloc)) n *= 2;
  return n;
}
function majVue(recentrer = false) {
  const large = !R.zoom || R.zoom >= R.taux, l = large ? R.taux : R.zoom, demi = R.taux / 2 - l / 2;
  const avant = vue.centre;
  if (large) vue.centre = 0;
  else if (recentrer || R.decalage < vue.a + 0.1 * l || R.decalage > vue.b - 0.1 * l) vue.centre = Math.max(-demi, Math.min(demi, R.decalage));
  vue.a = vue.centre - l / 2; vue.b = vue.centre + l / 2;
  return vue.centre !== avant;
}

// Confirmation d'ouverture de la clé : au premier bloc d'échantillons reçu, ou erreur au bout de 3 s.
let attenteEchantillons = null;
const nomCle = () => { const i = source?.infos?.() || {}; return [i.modele || "clé RTL-SDR", i.tuner && `tuner ${i.tuner}`].filter(Boolean).join(", "); };
function traiterBloc(octets) {
  if (attenteEchantillons) { clearTimeout(attenteEchantillons); attenteEchantillons = null; statut(`✓ Clé ouverte, réception en cours : ${nomCle()}.`); $("#statut").className = "ok"; }
  const [ir, ii] = octetsVersIQ(octets, etatDC);
  const nFFT = tailleFFT(ir.length);
  if (!fft || fft.n !== nFFT) { fft = new FFT(nFFT); moyenne = null; }
  if (ir.length >= fft.n) {
    const db = fft.spectre(ir, ii, 0);
    if (fft.n !== TAILLE_FFT) {          // même plancher de bruit à l'écran quelle que soit la taille de la FFT
      const k = 10 * Math.log10(fft.n / TAILLE_FFT); for (let i = 0; i < db.length; i++) db[i] += k;
    }
    if (!moyenne || moyenne.length !== db.length) moyenne = db;
    else { const a = R.moyennage; for (let k = 0; k < db.length; k++) moyenne[k] = a * moyenne[k] + (1 - a) * db[k]; }
    dernierSpectre = moyenne;
  }
  if (abonnes.iq.size) publier("iq", ir, ii, R.centre, R.taux);
  chaine.agcActif = R.agc; chaine.squelch = R.squelch;
  const surMultiplex = abonnes.fm.size ? multiplex => {
    const resultat = {};
    publier("fm", multiplex, resultat);
    return resultat.stereo;
  } : null;
  const a = chaine.traiter(ir, ii, R.decalage, surMultiplex);
  publier("niveau", chaine.niveau, chaine.modulation);
  publier("audio", a, TAUX_AUDIO);
  if (sortie) {
    const canaux = (chaine.stereo || [a]).map(canal => Float32Array.from(canal, x => x * R.volume));
    sortie.port.postMessage(canaux, canaux.map(canal => canal.buffer));
  }
}

async function demarrerAudio() {
  if (!audio) {
    audio = new AudioContext({ sampleRate: TAUX_AUDIO });
    await audio.audioWorklet.addModule("sdr-web/audio.js?v=20261121-fm-stereo");
    sortie = new AudioWorkletNode(audio, "sortie-sdr", { outputChannelCount: [2] });
    sortie.connect(audio.destination);
  }
  if (audio.state === "suspended") await audio.resume();
}

// usb : clé déjà autorisée (relance sans redemander) ; objet : source fournie par un module (fichier IQ)
async function demarrer(type, usb = null, objet = null) {
  await arreter();
  try {
    await demarrerAudio();
    source = type === "fichier" ? objet : type === "cle" ? (usb ? new CleRtlSdr(usb) : await CleRtlSdr.choisir()) : new SourceSimulation();
    statut(type === "cle" ? "Ouverture de la clé…" : type === "fichier" ? `Relecture : ${objet.fichier?.name ?? objet.nom}` : "Source simulée");
    await source.ouvrir(R.taux, R.centre + R.convertisseur);
    if (source.reglerDirect && R.direct) await source.reglerDirect(R.direct);
    if (source.reglerPpm) await source.reglerPpm(R.ppm);
    if (source.reglerGain && R.gain !== null) await source.reglerGain(R.gain);
  } catch (e) {
    const cle = type === "cle" && source;
    source = null; majEtat();
    if (e.name === "NotFoundError") statut("Aucune clé choisie.");
    else erreur(cle ? `✗ La clé n'a pas pu être ouverte : ${e.message}` : e.message);
    return;
  }
  chaine = new Chaine(R.taux); chaine.regler(R.mode, largeur());
  fft = new FFT(TAILLE_FFT); moyenne = null;
  const bloc = R.taux / 20;   // 50 ms
  if (source instanceof SourceSimulation) minuterie = setInterval(() => traiterBloc(source.lire(bloc)), 50);
  else if (type === "fichier") {
    source.surErreur = e => { erreur("Relecture interrompue : " + e.message); arreter(); };
    source.demarrerLecture(Math.ceil((bloc * 2) / 512) * 512, traiterBloc);
  } else {
    source.surErreur = e => { erreur("Lecture interrompue : " + e.message); arreter(); };
    statut(`Clé ouverte (${nomCle()}), en attente des échantillons…`);
    clearTimeout(attenteEchantillons);
    attenteEchantillons = setTimeout(() => {
      attenteEchantillons = null;
      erreur("✗ La clé est ouverte mais n'envoie aucun échantillon : débranchez-la, rebranchez-la puis cliquez à nouveau sur « Connecter la clé ».");
    }, 3000);
    source.demarrerLecture(Math.ceil((bloc * 2) / 512) * 512, traiterBloc);
  }
  afficherMateriel(); majEtat(); majVfo();
}

async function arreter() {
  clearInterval(minuterie); minuterie = null;
  clearTimeout(attenteEchantillons); attenteEchantillons = null;
  const s = source; source = null;
  effacerSpectre();
  if (s) await s.fermer();
  majEtat();
}

let enAccord = Promise.resolve();
function accorderCentre(c) {      // les réglages de la clé passent l'un après l'autre
  R.centre = c;   // fréquence affichée ; la clé, elle, reçoit c + décalage du convertisseur
  if (source?.reglerFrequence) enAccord = enAccord.then(() => source?.reglerFrequence(c + R.convertisseur)).catch(e => erreur(e.message));
  else source?.regler?.(c);
  effacerSpectre();
}

// ---------------------------------------------------------------- accord
function reglerFrequence(f, recentrer = true) {
  const [a, b] = source?.centreFixe   // relecture d'un fichier : centre imposé, le VFO reste dans la bande enregistrée
    ? [R.centre + R.convertisseur - R.taux * 0.46, R.centre + R.convertisseur + R.taux * 0.46]
    : source?.plage || [22e6, 1100e6];   // plage de la clé, ramenée à l'affichage
  if (source?.centreFixe) recentrer = false;
  const mini = Math.max(0, a - R.convertisseur), maxi = b - R.convertisseur;
  f = Math.round(Math.max(mini, Math.min(maxi, f)));
  const marge = R.taux * 0.4;
  if (recentrer && Math.abs(f - R.centre) > marge) accorderCentre(f - R.taux * 0.1);
  R.decalage = f - R.centre;
  majVfo();
}
function reglerMode(m) {
  if (!MODES.includes(m)) return;
  R.mode = m; $("#mode").value = m; $("#largeur").value = largeur();
  chaine?.regler(R.mode, largeur()); majVfo();
}
const pas = n => reglerFrequence(Math.round(frequence() / R.pas) * R.pas + n * R.pas);

// ---------------------------------------------------------------- interface : afficheur
function majVfo() {
  const f = frequence(), txt = String(Math.round(f)).padStart(10, "0");
  const chiffres = $("#chiffres"); chiffres.replaceChildren();
  let debut = true;
  txt.split("").forEach((c, i) => {
    const poids = 10 ** (9 - i);
    if (c !== "0") debut = false;
    const s = document.createElement("span"); s.textContent = c; s.dataset.poids = poids;
    if (debut && poids > 1e6) s.className = "eteint";
    chiffres.append(s);
    if (i === 0 || i === 3 || i === 6) { const p = document.createElement("i"); p.textContent = "."; chiffres.append(p); }
  });
  document.title = `${(f / 1e6).toFixed(4)} MHz ${R.mode} · SDR Web`;
  publier("vfo", f, R.mode);
}
$("#chiffres").addEventListener("wheel", e => {
  const p = Number(e.target.dataset?.poids); if (!p) return;
  e.preventDefault(); reglerFrequence(frequence() + (e.deltaY < 0 ? p : -p));
}, { passive: false });

// Boutons ▲ ▼ : un pas par clic ; maintenus, ils répètent.
for (const [id, n] of [["#f-haut", 1], ["#f-bas", -1]]) {
  const b = $(id); let attente, repetition;
  const stop = () => { clearTimeout(attente); clearInterval(repetition); };
  b.addEventListener("pointerdown", e => {
    if (e.button !== 0) return;
    e.preventDefault(); stop(); pas(n);
    attente = setTimeout(() => { repetition = setInterval(() => pas(n), 80); }, 400);
  });
  for (const ev of ["pointerup", "pointerleave", "pointercancel"]) b.addEventListener(ev, stop);
  b.addEventListener("click", e => { if (e.detail === 0) pas(n); });   // clavier (Entrée, Espace)
}

// Saisie au clavier : clic sur l'afficheur, ou un chiffre tapé n'importe où sur la page.
// En MHz par défaut (145.5 ou 145,5) ; suffixes k, M, G acceptés ; un grand nombre sans suffixe est en Hz.
const saisie = $("#saisie-freq");
function lireFrequence(texte) {
  const m = texte.trim().replace(",", ".").replace(/\s+/g, "").match(/^(\d+(?:\.\d*)?|\.\d+)([kmg])?(?:hz)?$/i);
  if (!m) return null;
  const v = parseFloat(m[1]), u = m[2]?.toLowerCase();
  const f = u === "k" ? v * 1e3 : u === "m" ? v * 1e6 : u === "g" ? v * 1e9 : v >= 1e5 ? v : v * 1e6;
  return f > 0 && f < 10e9 ? Math.round(f) : null;
}
function ouvrirSaisie(debut) {
  if (!saisie.hidden) return;
  saisie.value = debut ?? (frequence() / 1e6).toFixed(4).replace(/\.?0+$/, "");
  $("#chiffres").hidden = true; saisie.hidden = false; $("#unite").textContent = "MHz";
  saisie.focus();
  if (debut === undefined) saisie.select(); else saisie.setSelectionRange(saisie.value.length, saisie.value.length);
}
function fermerSaisie(valider) {
  if (saisie.hidden) return;
  const f = valider ? lireFrequence(saisie.value) : null;
  if (valider && f === null && saisie.value.trim()) { saisie.style.borderBottomColor = "#ff7a7a"; return; }
  saisie.hidden = true; saisie.style.borderBottomColor = ""; $("#chiffres").hidden = false; $("#unite").textContent = "Hz";
  if (f !== null) reglerFrequence(f);
}
saisie.addEventListener("input", () => { saisie.style.borderBottomColor = ""; });
saisie.addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); fermerSaisie(true); }
  else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); fermerSaisie(false); }
});
saisie.addEventListener("blur", () => { if (!saisie.hidden) { saisie.hidden = true; saisie.style.borderBottomColor = ""; $("#chiffres").hidden = false; $("#unite").textContent = "Hz"; } });
$("#chiffres").addEventListener("click", () => ouvrirSaisie());

// ---------------------------------------------------------------- interface : réglages
function remplir(sel, valeurs, texte, actuelle) {
  sel.replaceChildren(...valeurs.map(v => { const o = new Option(texte(v), v); o.selected = v === actuelle; return o; }));
}
remplir($("#mode"), MODES, m => m, R.mode);
remplir($("#pas"), PAS, p => p >= 1e6 ? `${p / 1e6} MHz` : p >= 1000 ? `${p / 1000} kHz` : `${p} Hz`, R.pas);
remplir($("#taux"), TAUX, t => `${(t / 1e6).toFixed(3)} MS/s`, R.taux);
$("#mode").onchange = e => reglerMode(e.target.value);
$("#pas").onchange = e => { R.pas = Number(e.target.value); };
$("#taux").onchange = async e => {
  if (source?.centreFixe) { e.target.value = R.taux; statut("Le taux est celui de l'enregistrement pendant la relecture."); return; }
  R.taux = Number(e.target.value); remplirZoom(); if (source) await demarrer(source instanceof SourceSimulation ? "sim" : "cle", source.usb); };
$("#largeur").value = largeur();
$("#largeur").onchange = e => { R.largeurs[R.mode] = Number(e.target.value); chaine?.regler(R.mode, largeur()); };
// Entrée HF : échantillonnage direct (branche Q de la Blog V3) ; décalage d'un convertisseur HF
$("#hf").value = String(R.direct);
$("#hf").onchange = e => {
  R.direct = Number(e.target.value); sauver();
  if (!source?.reglerDirect) return;
  enAccord = enAccord.then(async () => {
    await source.reglerDirect(R.direct);
    afficherMateriel(); majGain();
    const [a, b] = source.plage, f = frequence() + R.convertisseur;
    // hors de la nouvelle plage : 7,1 MHz en HF, 145,5 MHz en retour sur le tuner
    if (f < a || f > b) { reglerMode(R.direct ? "LSB" : "NFM"); reglerFrequence((R.direct ? 7_100_000 : 145_500_000) - R.convertisseur); }
    else accorderCentre(R.centre);
  }).catch(err => erreur(err.message));
};
$("#conv").value = R.convertisseur / 1e6;
$("#conv").onchange = e => { R.convertisseur = Math.round((Number(e.target.value) || 0) * 1e6); sauver(); accorderCentre(R.centre); };
$("#ppm").value = R.ppm;
$("#ppm").onchange = e => { R.ppm = Number(e.target.value) || 0; if (source?.reglerPpm) enAccord = enAccord.then(() => source.reglerPpm(R.ppm)); };
$("#agc").checked = R.agc; $("#agc").onchange = e => { R.agc = e.target.checked; };
$("#squelch").value = R.squelch; $("#squelch").oninput = e => { R.squelch = Number(e.target.value); $("#squelch-val").textContent = R.squelch <= -120 ? "ouvert" : `${R.squelch} dBFS`; };
$("#squelch").oninput({ target: $("#squelch") });
$("#volume").value = R.volume; $("#volume").oninput = e => { R.volume = Number(e.target.value); };
$("#moyennage").value = R.moyennage; $("#moyennage").oninput = e => { R.moyennage = Number(e.target.value); };
function remplirZoom() {     // largeurs plus petites que la bande reçue seulement
  const z = ZOOMS.filter(v => v === 0 || v < R.taux);
  if (!z.includes(R.zoom)) R.zoom = 0;
  remplir($("#zoom"), z, v => v === 0 ? "Toute la bande" : v >= 1e6 ? `${v / 1e6} MHz` : `${v / 1e3} kHz`, R.zoom);
}
remplirZoom();
$("#zoom").onchange = e => { R.zoom = Number(e.target.value); majVue(true); effacerChute(); moyenne = null; };
$("#gain-auto").checked = R.gain === null;
function majGain() {
  const auto = $("#gain-auto").checked, g = source?.gains?.() || [];
  $("#gain").disabled = auto || !g.length; $("#gain").max = Math.max(0, g.length - 1);
  const i = R.gain === null ? g.length - 1 : Math.max(0, g.findIndex(x => x >= R.gain));
  $("#gain").value = i; $("#gain-val").textContent = auto ? "auto" : g.length ? `${g[i]} dB` : "—";
}
const appliquerGain = () => {
  const g = source?.gains?.() || [];
  R.gain = $("#gain-auto").checked || !g.length ? null : g[Number($("#gain").value)];
  majGain();
  if (source?.reglerGain) enAccord = enAccord.then(() => source.reglerGain(R.gain)).catch(e => erreur(e.message));
};
$("#gain-auto").onchange = appliquerGain; $("#gain").oninput = appliquerGain;
$("#echelle").onclick = () => {
  if (!dernierSpectre) return;
  const t = [...dernierSpectre].sort((a, b) => a - b);
  R.min = Math.round(t[Math.floor(t.length * 0.2)] - 8); R.max = Math.round(Math.max(t[Math.floor(t.length * 0.999)] + 5, R.min + 30));
};
$("#b-cle").onclick = () => demarrer("cle");
$("#b-arret").onclick = () => arreter();
addEventListener("keydown", e => {
  if (e.defaultPrevented || e.target.closest("input, select, textarea, dialog")) return;
  if (/^[0-9.,]$/.test(e.key) && !e.ctrlKey && !e.altKey && !e.metaKey) { e.preventDefault(); ouvrirSaisie(e.key === "," ? "." : e.key); return; }
  const n = { ArrowUp: 1, ArrowDown: -1, ArrowRight: 1, ArrowLeft: -1, PageUp: 10, PageDown: -10 }[e.key];
  if (n) { e.preventDefault(); pas(n); }
});

function majEtat() {
  const actif = !!source;
  $("#b-arret").disabled = !actif;
  $("#etat").textContent = actif ? `${source.nom} en fonction` : "arrêté";
  $("#etat").className = actif ? "marche" : "";
  const cle = actif && source instanceof CleRtlSdr;   // une vraie clé est ouverte
  $("#b-cle").classList.toggle("connecte", cle);
  $("#b-cle").textContent = cle ? "✓ Clé connectée" : "🔌 Connecter la clé";
  $("#b-cle").title = cle ? "Clé RTL-SDR ouverte et en réception (cliquer pour en choisir une autre)" : "";
  majGain();
}
function afficherMateriel() {
  const i = source?.infos?.() || {};
  $("#materiel").innerHTML = source ? `<b>${i.modele || "—"}</b><br>${i.tuner ? "Tuner " + i.tuner : ""}${i.serie ? " · n° " + i.serie : ""}<br>${i.couverture ? "Couverture " + i.couverture : ""}` : "—";
}
function statut(t) { $("#statut").textContent = t; $("#statut").className = ""; }
function erreur(t) { $("#statut").textContent = t; $("#statut").className = "erreur"; }

// ---------------------------------------------------------------- spectre et chute d'eau
const cs = $("#spectre"), cw = $("#chute"), xs = cs.getContext("2d"), xw = cw.getContext("2d");
function effacerSpectre() {
  moyenne = dernierSpectre = null;
  effacerChute();
}
function effacerChute() { xw.clearRect(0, 0, cw.width, cw.height); }

const separateur = document.createElement("div");
separateur.tabIndex = 0;
separateur.setAttribute("role", "separator");
separateur.setAttribute("aria-orientation", "horizontal");
separateur.setAttribute("aria-label", "Hauteur du spectre et de la chute d’eau");
separateur.setAttribute("aria-valuemin", "10");
separateur.setAttribute("aria-valuemax", "90");
Object.assign(separateur.style, {
  cursor: "ns-resize", touchAction: "none",
  background: "linear-gradient(to bottom, #101a2b 3px, #48a6bb 3px 5px, #101a2b 5px)",
});

const ecran = cs.parentElement;
// La taille des bitmaps ne doit pas déterminer la hauteur de la grille.
ecran.style.contain = "size";
const hauteurSeparateur = 8;
cs.after(separateur);
for (const canvas of [cs, cw]) {
  Object.assign(canvas.style, { minWidth: "0", minHeight: "0", contain: "size" });
}
function repartirGraphiques(pourcentage) {
  const partSpectre = Math.max(10, Math.min(90, pourcentage));
  ecran.style.gridTemplateRows = `calc((100% - ${hauteurSeparateur}px) * ${partSpectre / 100}) ${hauteurSeparateur}px minmax(0, 1fr)`;
  separateur.setAttribute("aria-valuenow", String(Math.round(partSpectre)));
}
repartirGraphiques(50);

let glissement;
separateur.addEventListener("pointerdown", e => {
  if (e.button !== 0 || glissement) return;
  e.preventDefault();
  separateur.focus({ preventScroll: true });
  separateur.setPointerCapture(e.pointerId);
  glissement = {
    id: e.pointerId,
    decalage: e.clientY - separateur.getBoundingClientRect().top,
  };
});
addEventListener("pointermove", e => {
  if (glissement?.id !== e.pointerId) return;
  const hauteurGraphiques = Math.max(1, ecran.clientHeight - hauteurSeparateur);
  const y = e.clientY - ecran.getBoundingClientRect().top - glissement.decalage;
  repartirGraphiques(100 * y / hauteurGraphiques);
});
for (const evenement of ["pointerup", "pointercancel", "lostpointercapture"]) {
  addEventListener(evenement, e => {
    if (glissement?.id === e.pointerId) glissement = undefined;
  });
}
separateur.addEventListener("keydown", e => {
  if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
  e.preventDefault();
  const part = Number(separateur.getAttribute("aria-valuenow"));
  repartirGraphiques(part + (e.key === "ArrowUp" ? -5 : 5));
});
const LUT = (() => {   // palette froide → chaude
  const pts = [[0, [8, 10, 30]], [0.3, [20, 60, 160]], [0.55, [0, 200, 230]], [0.75, [250, 220, 60]], [1, [255, 60, 40]]];
  const t = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const x = i / 255; let j = 0; while (j < pts.length - 2 && x > pts[j + 1][0]) j++;
    const [x0, c0] = pts[j], [x1, c1] = pts[j + 1], u = (x - x0) / (x1 - x0);
    for (let c = 0; c < 3; c++) t[i * 3 + c] = c0[c] + (c1[c] - c0[c]) * u;
  }
  return t;
})();
function dimensionner() {
  const r = devicePixelRatio || 1;
  for (const c of [cs, cw]) {
    const w = Math.round(c.clientWidth * r), h = Math.round(c.clientHeight * r);
    if (c.width === w && c.height === h) continue;
    // Conserve les lignes récentes quand seule la hauteur du waterfall change.
    const lignes = Math.min(h, c.height);
    const historique = c === cw && c.width === w && w > 0 && lignes > 0
      ? xw.getImageData(0, 0, w, lignes) : null;
    c.width = w;
    c.height = h;
    if (historique) xw.putImageData(historique, 0, 0);
  }
}
function dessiner() {
  requestAnimationFrame(dessiner);
  dimensionner();
  const W = cs.width, H = cs.height, r = devicePixelRatio || 1;
  xs.fillStyle = "#070d18"; xs.fillRect(0, 0, W, H);
  // grille et graduations
  xs.font = `${10 * r}px 'Share Tech Mono', monospace`; xs.fillStyle = "#6f8aa0"; xs.strokeStyle = "rgba(0,229,255,.08)"; xs.lineWidth = 1;
  for (let db = Math.ceil(R.min / 10) * 10; db <= R.max; db += 10) {
    const y = H - (db - R.min) / (R.max - R.min) * H; xs.beginPath(); xs.moveTo(0, y); xs.lineTo(W, y); xs.stroke(); xs.fillText(`${db}`, 4 * r, y - 2 * r);
  }
  if (majVue()) effacerChute();
  const l = vue.b - vue.a, xDe = f => (f - vue.a) / l * W;        // f : Hz par rapport au centre de la clé
  const pasGrad = [1e3, 2e3, 5e3, 10e3, 20e3, 25e3, 50e3, 100e3, 250e3, 500e3].find(p => l / p <= 10) || 1e6;
  for (let f = Math.ceil((R.centre + vue.a) / pasGrad) * pasGrad; f < R.centre + vue.b; f += pasGrad) {
    const x = xDe(f - R.centre); xs.beginPath(); xs.moveTo(x, 0); xs.lineTo(x, H); xs.stroke();
    const dec = f % 1e6 === 0 ? 0 : f % 1e5 === 0 ? 1 : f % 1e4 === 0 ? 2 : f % 1e3 === 0 ? 3 : 4;
    xs.fillText((f / 1e6).toFixed(dec), x + 3 * r, H - 4 * r);
  }
  // bande du canal et VFO
  const xv = xDe(R.decalage), lb = largeur() / l * W;
  const [b0, b1] = R.mode === "USB" ? [0, lb] : R.mode === "LSB" ? [-lb, 0] : [-lb / 2, lb / 2];
  xs.fillStyle = "rgba(255,255,255,.08)"; xs.fillRect(xv + b0, 0, Math.max(1, b1 - b0), H);
  xs.strokeStyle = "#ff5252"; xs.beginPath(); xs.moveTo(xv, 0); xs.lineTo(xv, H); xs.stroke();
  if (!dernierSpectre || !source) return;
  const s = dernierSpectre, n = s.length;
  // courbe (max par colonne de pixels)
  xs.strokeStyle = "#4fc3f7"; xs.lineWidth = 1.2 * r; xs.beginPath();
  const ligne = new Float32Array(W), caseDe = f => Math.floor((f / R.taux + 0.5) * n);
  for (let x = 0; x < W; x++) {
    const a = Math.max(0, caseDe(vue.a + x / W * l)), b = Math.min(n, Math.max(a + 1, caseDe(vue.a + (x + 1) / W * l))); let m = -200;
    for (let k = a; k < b; k++) if (s[k] > m) m = s[k];
    ligne[x] = m;
    const y = H - (m - R.min) / (R.max - R.min) * H;
    x ? xs.lineTo(x, y) : xs.moveTo(x, y);
  }
  xs.stroke();
  // chute d'eau : décale d'une ligne vers le bas et dessine la nouvelle en haut
  const Wc = cw.width;
  xw.drawImage(cw, 0, 0, Wc, cw.height - 1, 0, 1, Wc, cw.height - 1);
  const img = xw.createImageData(Wc, 1);
  for (let x = 0; x < Wc; x++) {
    const v = ligne[Math.min(W - 1, Math.floor(x * W / Wc))];
    const i = Math.max(0, Math.min(255, Math.round((v - R.min) / (R.max - R.min) * 255))) * 3;
    img.data[x * 4] = LUT[i]; img.data[x * 4 + 1] = LUT[i + 1]; img.data[x * 4 + 2] = LUT[i + 2]; img.data[x * 4 + 3] = 255;
  }
  xw.putImageData(img, 0, 0);
}
for (const c of [cs, cw]) {
  c.addEventListener("click", e => {
    const rect = c.getBoundingClientRect();
    const position = (e.clientX - rect.left) / rect.width;
    reglerFrequence(R.centre + vue.a + position * (vue.b - vue.a), false);
  });
  c.addEventListener("wheel", e => { e.preventDefault(); pas(e.deltaY < 0 ? 1 : -1); }, { passive: false });
}

// ---------------------------------------------------------------- modules
const actifs = new Map();
const contexte = module => ({
  abonner(sujet, f) { f.module = module; abonnes[sujet].add(f); module._abos.push([sujet, f]); },
  frequence, mode: () => R.mode, reglerFrequence, reglerMode,
  taux: () => R.taux,
  gains: () => source?.gains?.() || [],
  gain: () => R.gain,         // dB, ou null en gain automatique du tuner
  reglerGain(g) {             // g en dB, ou null pour le gain automatique
    const liste = source?.gains?.() || [];
    if (g !== null && !liste.length) return;
    $("#gain-auto").checked = g === null;
    if (g !== null) $("#gain").value = liste.reduce((m, x, i) => Math.abs(x - g) < Math.abs(liste[m] - g) ? i : m, 0);
    appliquerGain();
  },
  async reglerTaux(t) {
    if (!TAUX.includes(t) || t === R.taux || source?.centreFixe) return;
    R.taux = t; $("#taux").value = t; remplirZoom();
    if (source) await demarrer(source instanceof SourceSimulation ? "sim" : "cle", source.usb);
  },
  /** Remplace la source par un objet du module (relecture d'un fichier IQ : taux et centre imposés). */
  async rejouer(objet) {
    if (!TAUX.includes(objet.taux)) throw new Error(`taux de ${objet.taux / 1e6} MS/s non pris en charge (${TAUX.map(t => t / 1e6).join(", ")} MS/s)`);
    const avant = frequence();
    R.taux = objet.taux; $("#taux").value = objet.taux; remplirZoom();
    R.centre = objet.centre;
    R.decalage = Math.abs(avant - R.centre) < 0.4 * R.taux ? avant - R.centre : 0.1 * R.taux;
    await demarrer("fichier", null, objet);
    reglerFrequence(frequence(), false);
    return source === objet;
  },
  source: () => source,
  arreterSource: () => arreter(),
  lire(cle, defaut) { try { return JSON.parse(localStorage.getItem(`sdr-web/${module.nom}/${cle}`)) ?? defaut; } catch { return defaut; } },
  ecrire(cle, v) { localStorage.setItem(`sdr-web/${module.nom}/${cle}`, JSON.stringify(v)); },
});
function activerModule(nom) {
  const Classe = MODULES.find(m => m.nom === nom); if (!Classe || actifs.has(nom)) return;
  const m = new Classe(); m.nom = nom; m._abos = []; m.ctx = contexte(m);
  try {
    m.demarrer?.();
    const panneau = m.creerPanneau?.();
    if (panneau) {
      const boite = document.createElement("section"); boite.className = "panneau-module";
      const h = document.createElement("h3"); h.textContent = nom;
      const x = document.createElement("button"); x.type = "button"; x.textContent = "×"; x.title = "Retirer le module"; x.onclick = () => retirerModule(nom);
      h.append(x); boite.append(h, panneau); $("#panneaux").append(boite); m._boite = boite;
    }
    actifs.set(nom, m);
  } catch (e) { console.error(e); erreur(`Le module « ${nom} » n'a pas pu démarrer : ${e.message}`); for (const [s, f] of m._abos) abonnes[s].delete(f); }
  R.modules = [...actifs.keys()]; listeModules();
}
function retirerModule(nom) {
  const m = actifs.get(nom); if (!m) return;
  for (const [s, f] of m._abos) abonnes[s].delete(f);
  try { m.arreter?.(); } catch (e) { console.error(e); }
  m._boite?.remove(); actifs.delete(nom);
  R.modules = [...actifs.keys()]; listeModules();
}
function listeModules() {
  const choix = document.createElement("select"); choix.id = "choix-module"; choix.setAttribute("aria-label", "Ajouter ou retirer un module");
  const titre = new Option(actifs.size ? `${actifs.size} module${actifs.size > 1 ? "s" : ""} actif${actifs.size > 1 ? "s" : ""} — ajouter…` : "Ajouter un module…", "");
  titre.disabled = true; titre.selected = true; choix.add(titre);
  for (const M of MODULES) {
    const o = new Option(`${actifs.has(M.nom) ? "✓ " : "\u2003"}${M.nom}`, M.nom);
    o.title = M.description + (actifs.has(M.nom) ? " (choisir pour le retirer)" : ""); choix.add(o);
  }
  const aide = document.createElement("small"); aide.textContent = "Un module coché ✓ est actif ; le choisir à nouveau le retire.";
  choix.onchange = () => { const nom = choix.value; actifs.has(nom) ? retirerModule(nom) : activerModule(nom); };
  $("#modules").replaceChildren(choix, aide);
}

// ---------------------------------------------------------------- démarrage de la page
const compatible = "usb" in navigator;
$("#b-cle").disabled = !compatible;
if (!compatible) erreur("Ce navigateur ne gère pas WebUSB : la clé ne peut pas être utilisée. Essayez Chrome, Edge ou Opera sur ordinateur ou Android.");
else statut("Branchez la clé puis cliquez sur « Connecter la clé ».");
reglerMode(R.mode);
majVfo(); majEtat(); listeModules();
[...R.modules].forEach(activerModule);
requestAnimationFrame(dessiner);
// Présence : toutes les 30 s, un numéro tiré au hasard à l'ouverture de la page (gardé en mémoire,
// rien n'est stocké dans le navigateur) et si une clé est en réception. Sert seulement à compter
// les utilisateurs de SDR Web en ce moment (stats/presence.py du serveur, page /visites/).
const PRESENCE = crypto.getRandomValues(new Uint32Array(2)).reduce((s, x) => s + x.toString(36).padStart(7, "0"), "");
const presence = (fin = false) => {
  const cle = source instanceof CleRtlSdr ? 1 : 0;
  const url = `/sdr-web/presence?s=${PRESENCE}&cle=${cle}${fin ? "&fin=1" : ""}`;
  if (fin) navigator.sendBeacon?.(url); else fetch(url, { cache: "no-store", keepalive: true }).catch(() => {});
};
presence(); setInterval(presence, 30000);
addEventListener("pagehide", () => presence(true));
navigator.usb?.addEventListener("disconnect", e => { if (source?.usb === e.device) { erreur("La clé a été débranchée."); source = null; majEtat(); } });
