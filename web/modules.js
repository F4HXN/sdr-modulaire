// SPDX-License-Identifier: MIT
// SDR Web : modules ajoutés et retirés à chaud, sur le même principe que SDR Modulaire.
//
// Un module est une classe avec :
//   static nom, static description
//   demarrer()        : état initial et abonnements (this.ctx.abonner(sujet, rappel))
//   creerPanneau()    : facultatif, renvoie un élément HTML affiché dans la colonne des modules
//   arreter()         : minuteries, fichiers… (les abonnements sont retirés automatiquement)
// Sujets : "niveau" (dBFS), "audio" (Float32Array 48 kHz), "iq" (re, im, centre, taux), "vfo" (fréquence, mode).
// Contexte : frequence(), mode(), reglerFrequence(f), reglerMode(m), taux(), reglerTaux(t) (asynchrone),
//            gains() (dB), reglerGain(g) (null = automatique),
//            lire(clé, défaut), ecrire(clé, valeur).
// Les rappels du bus doivent rester rapides : on stocke, et une minuterie met l'affichage à jour.

import { FirComplexe, NCO, passeBas, LARGEURS, FFT } from "./dsp.js?v=20261121-fm-stereo";
import { StereoRds } from "./fm-stereo.js?v=20261121-fm-stereo";
import { DecodeurOOK433, TAUX_CAPTEURS } from "./capteurs433.js";
import { DemodModeS, Trafic, distanceKm } from "./adsb.js?v=20261122-diag";
import { CarteRadar, couleurAltitude } from "./carte-adsb.js?v=20261122-diag";
import { DecodeurSSTV, MODES_SSTV } from "./sstv.js?v=20261122-sstv";

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

const MEMOIRES_DEFAUT = [
  { nom: "APRS", f: 144_800_000, mode: "NFM" }, { nom: "Appel FM 2 m", f: 145_500_000, mode: "NFM" },
  { nom: "Appel FM 70 cm", f: 433_500_000, mode: "NFM" }, { nom: "Radio FM (exemple)", f: 87_800_000, mode: "WFM" },
];

class SMetre {
  static nom = "S-mètre";
  static description = "Niveau du canal en dBFS, avec maintien de crête.";
  demarrer() {
    this.niveau = -120; this.crete = -120;
    this.ctx.abonner("niveau", db => { this.niveau = db; this.crete = Math.max(db, this.crete - 0.3); });
  }
  creerPanneau() {
    this.txt = el("div", { className: "smetre-txt" });
    this.barre = el("div", { className: "smetre-barre" }, el("i"));
    this.minuterie = setInterval(() => {
      this.txt.textContent = `${this.niveau.toFixed(1)} dBFS · crête ${this.crete.toFixed(1)}`;
      this.barre.firstChild.style.width = `${Math.max(0, Math.min(100, (this.niveau + 120) / 120 * 100))}%`;
    }, 100);
    return el("div", {}, this.txt, this.barre);
  }
  arreter() { clearInterval(this.minuterie); }
}

class Memoires {
  static nom = "Mémoires";
  static description = "Fréquences favorites : ajouter la fréquence courante, rappeler d'un clic.";
  demarrer() {
    this.liste = this.ctx.lire("liste", MEMOIRES_DEFAUT);
  }
  creerPanneau() {
    this.ul = el("ul", { className: "memoires" });
    const ajout = el("button", { type: "button", textContent: "Ajouter la fréquence actuelle" });
    ajout.onclick = () => {
      const nom = prompt("Nom de la mémoire :"); if (nom === null) return;
      this.liste.push({ nom: nom || "—", f: Math.round(this.ctx.frequence()), mode: this.ctx.mode() }); this.sauver();
    };
    this.remplir();
    return el("div", {}, this.ul, ajout);
  }
  remplir() {
    this.ul.replaceChildren(...[...this.liste].sort((a, b) => a.f - b.f).map(m => {
      const b = el("button", { type: "button", className: "rappel", textContent: `${(m.f / 1e6).toFixed(4)} ${m.mode} · ${m.nom}` });
      b.onclick = () => { this.ctx.reglerMode(m.mode); this.ctx.reglerFrequence(m.f); };
      const x = el("button", { type: "button", className: "suppr", textContent: "×", title: "Supprimer" });
      x.onclick = () => { this.liste = this.liste.filter(y => y !== m); this.sauver(); };
      return el("li", {}, b, x);
    }));
  }
  sauver() { this.ctx.ecrire("liste", this.liste); this.remplir(); }
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

class ADSB {
  static nom = "ADS-B (avions)";
  static description = "Avions sur 1090 MHz : indicatif, altitude, vitesse, position sur une carte radar.";

  demarrer() {
    this.trafic = new Trafic();
    this.trafic.station = this.ctx.lire("station", null);
    this.derniersIQ = -Infinity; this.etat = "attente"; this.messages = 0; this.debit = 0;
    this.echantillons = 0; this.tauxRecu = 0;
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
    accorder.title = "Règle l'échantillonnage sur 2,4 MS/s, le gain au maximum et la fréquence sur 1090 MHz";
    accorder.onclick = async () => {
      if (this.ctx.taux() < TAUX_ADSB) await this.ctx.reglerTaux(TAUX_ADSB);
      const gains = this.ctx.gains();
      if (gains.length) this.ctx.reglerGain(Math.max(...gains));   // impulsions brèves : gain maximal
      this.ctx.reglerMode("AM"); this.ctx.reglerFrequence(FREQUENCE_ADSB);
    };
    const ouvrir = el("button", { type: "button", textContent: "Ouvrir la carte", className: "adsb-ouvrir" });
    ouvrir.setAttribute("aria-haspopup", "dialog");
    this.statut = el("div", { className: "petit" });
    this.diag = el("div", { className: "petit adsb-diag", title: "Diagnostic de réception, par seconde" });
    this.liste = el("ul", { className: "journal adsb-mini" });

    // Fenêtre de la carte.
    const titre = el("h2", { id: "adsb-titre", textContent: "ADS-B · avions sur 1090 MHz" });
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
    this.carte = new CarteRadar(this.toile, { surChoix: icao => { this.choisi = icao; this.majTableau(); } });
    this.carte.station = this.trafic.station;
    ouvrir.onclick = () => { this.dialogue.showModal(); this.carte.avions = [...this.trafic.avions.values()]; this.carte.centrer(); };
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
        if (this.etat === "ecoute" && d) {
          const recus = Math.min(999, Math.round(100 * (ech - dAvant.ech) / dt / this.tauxRecu));
          const db = x => (20 * Math.log10(Math.max(x, 1e-6))).toFixed(0);
          this.diag.textContent = `Diagnostic : échantillons ${recus} % · bruit ${db(d.bruit)} dB · crête ${db(d.crete)} dB` +
            ` · préambules ${Math.max(0, Math.round((pre - dAvant.pre) / dt))}/s · DF11 ${Math.max(0, Math.round((df - dAvant.df) / dt))}/s` +
            ` · CRC faux ${Math.max(0, Math.round((crc - dAvant.crc) / dt))}/s`;
        } else this.diag.textContent = "";
        dAvant = { ech, pre, df, crc };
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
    return el("div", {}, accorder, this.statut, this.diag, this.liste, ouvrir, this.dialogue);
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
      const li = el("li", {}, el("label", {}, c, ` ${(m.f / 1e6).toFixed(4)} ${m.mode} · ${m.nom}`)); li.dataset.i = i;
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
  afficher() {
    this.bMarche.textContent = this.actif ? "■ Arrêter" : "▶ Scanner";
    for (const li of this.ul.children) li.classList.toggle("courant", this.actif && Number(li.dataset.i) === this.indice);
    if (!this.actif) { this.statut.textContent = `${this.fini ? "Passage terminé" : "Arrêté"} · ${this.canaux().length} mémoire(s) à scanner`; return; }
    const m = this.memoires[this.indice], t = `${(m.f / 1e6).toFixed(4)} MHz ${m.mode} · ${m.nom}`;
    this.statut.textContent = this.etat === "signal"
      ? `● Signal sur ${t}${this.tSilence === null ? "" : ` · reprise dans ${Math.max(0, this.reprise - (performance.now() - this.tSilence) / 1000).toFixed(0)} s`}`
      : `Balayage… ${t} · tour ${this.tours}`;
  }
  arreter() { clearInterval(this.minuterie); this.actif = false; }
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
    this.version = -1;
    this.minuterie = setInterval(() => this.rafraichir(), 250);
    this.rafraichir();
    return el("div", {}, this.canevas, this.statut, this.tonalite,
      el("div", { className: "scanner-choix" }, this.choix, this.bDemarrer, this.bArreter),
      el("div", { className: "scanner-choix sstv-actions" }, telecharger,
        el("label", { className: "petit sstv-auto" }, this.auto, " Télécharger chaque image")),
      el("small", { textContent: "ISS : 145,800 MHz en NFM. Ondes courtes : 14,230 · 21,340 · 28,680 MHz en USB." }));
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

  arreter() { clearInterval(this.minuterie); clearInterval(this.travail); }
}

export const MODULES = [SMetre, Memoires, Enregistreur, Journal, Capteurs433, ModuleStereoRds, ADSB, ScannerMemoires, ScannerFrequences, ModuleSSTV];
