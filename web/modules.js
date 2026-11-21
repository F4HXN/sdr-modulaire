// SPDX-License-Identifier: MIT
// SDR Web : modules ajoutés et retirés à chaud, sur le même principe que SDR Modulaire.
//
// Un module est une classe avec :
//   static nom, static description
//   demarrer()        : état initial et abonnements (this.ctx.abonner(sujet, rappel))
//   creerPanneau()    : facultatif, renvoie un élément HTML affiché dans la colonne des modules
//   arreter()         : minuteries, fichiers… (les abonnements sont retirés automatiquement)
// Sujets : "niveau" (dBFS), "audio" (Float32Array 48 kHz), "iq" (re, im, centre, taux), "vfo" (fréquence, mode).
// Contexte : frequence(), mode(), reglerFrequence(f), reglerMode(m), lire(clé, défaut), ecrire(clé, valeur).
// Les rappels du bus doivent rester rapides : on stocke, et une minuterie met l'affichage à jour.

const el = (balise, props = {}, ...enfants) => { const e = Object.assign(document.createElement(balise), props); e.append(...enfants); return e; };

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
    this.liste = this.ctx.lire("liste", [
      { nom: "APRS", f: 144_800_000, mode: "NFM" }, { nom: "Appel FM 2 m", f: 145_500_000, mode: "NFM" },
      { nom: "Appel FM 70 cm", f: 433_500_000, mode: "NFM" }, { nom: "Radio FM (exemple)", f: 87_800_000, mode: "WFM" },
    ]);
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

export const MODULES = [SMetre, Memoires, Enregistreur, Journal];
