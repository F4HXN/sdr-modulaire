// SPDX-License-Identifier: MIT
// Carte radar autonome pour le module ADS-B : côtes, frontières, cercles de distance,
// avions orientés selon leur cap avec leur trace. Molette : zoom ; glisser : déplacer ;
// clic : choisir un avion ; double-clic : recentrer.

import { distanceKm } from "./adsb.js?v=20261122-niveau";

const KM_DEG = 111.32;
const decoder = l => { const p = []; let x = 0, y = 0; for (let i = 0; i < l.length; i += 2) { x += l[i]; y += l[i + 1]; p.push([x / 100, y / 100]); } return p; };
// Fond de carte mondial (~230 Ko) : chargé seulement à la première ouverture de la carte.
let COTES = [], FRONTIERES = [], chargement = null;
function chargerFond() {
  chargement ??= import("./carte-monde.js").then(({ CARTE }) => {
    COTES = CARTE.cotes.map(decoder); FRONTIERES = CARTE.frontieres.map(decoder);
    for (const l of [...COTES, ...FRONTIERES]) {   // boîte englobante : on ne trace que le visible
      let a = 90, b = -90, c = 180, d = -180;
      for (const [lon, lat] of l) { a = Math.min(a, lat); b = Math.max(b, lat); c = Math.min(c, lon); d = Math.max(d, lon); }
      l.boite = [a, b, c, d];
    }
  });
  return chargement;
}

/** Couleur selon l'altitude : vert au sol, jaune vers 15 000 ft, magenta au-dessus de 35 000 ft. */
export function couleurAltitude(alt) {
  if (alt === undefined) return "#9fb3c8";
  const t = Math.max(0, Math.min(1, alt / 40000));
  const teinte = 120 - t * 220;
  return `hsl(${(teinte + 360) % 360} 95% 62%)`;
}

export class CarteRadar {
  constructor(canvas, { surChoix } = {}) {
    this.c = canvas; this.ctx = canvas.getContext("2d");
    this.lat0 = 46.6; this.lon0 = 2.4; this.kmPx = 2.5;   // km par pixel
    this.selection = null; this.surChoix = surChoix;
    this.avions = []; this.station = null;
    chargerFond().then(() => this.dessiner());
    let glisse = null;
    canvas.addEventListener("wheel", e => {
      e.preventDefault();
      const [lat, lon] = this.versGeo(e.offsetX, e.offsetY), f = e.deltaY < 0 ? 1 / 1.2 : 1.2;
      this.kmPx = Math.max(0.05, Math.min(50, this.kmPx * f));
      const [x, y] = this.versEcran(lat, lon);           // garde le point sous la souris
      this.deplacer(x - e.offsetX, y - e.offsetY); this.dessiner();
    }, { passive: false });
    canvas.addEventListener("pointerdown", e => { glisse = { x: e.offsetX, y: e.offsetY, bouge: false }; canvas.setPointerCapture(e.pointerId); });
    canvas.addEventListener("pointermove", e => {
      if (!glisse) return;
      const dx = e.offsetX - glisse.x, dy = e.offsetY - glisse.y;
      if (Math.abs(dx) + Math.abs(dy) > 2) glisse.bouge = true;
      if (glisse.bouge) { this.deplacer(-dx, -dy); glisse.x = e.offsetX; glisse.y = e.offsetY; this.dessiner(); }
    });
    canvas.addEventListener("pointerup", e => {
      if (glisse && !glisse.bouge) this.choisirPres(e.offsetX, e.offsetY);
      glisse = null;
    });
    canvas.addEventListener("dblclick", e => { [this.lat0, this.lon0] = this.versGeo(e.offsetX, e.offsetY); this.dessiner(); });
  }

  get W() { return this.c.clientWidth; }
  get H() { return this.c.clientHeight; }
  versEcran(lat, lon) {
    return [this.W / 2 + (lon - this.lon0) * Math.cos(this.lat0 * Math.PI / 180) * KM_DEG / this.kmPx,
      this.H / 2 - (lat - this.lat0) * KM_DEG / this.kmPx];
  }
  versGeo(x, y) {
    const lat = this.lat0 - (y - this.H / 2) * this.kmPx / KM_DEG;
    return [lat, this.lon0 + (x - this.W / 2) * this.kmPx / (KM_DEG * Math.cos(this.lat0 * Math.PI / 180))];
  }
  deplacer(dx, dy) { [this.lat0, this.lon0] = this.versGeo(this.W / 2 + dx, this.H / 2 + dy); }

  /** Cadre la vue sur la station, ou à défaut sur les avions connus. */
  centrer() {
    const pos = this.avions.filter(a => a.lat !== undefined);
    if (this.station) { this.lat0 = this.station.lat; this.lon0 = this.station.lon; }
    else if (pos.length) { this.lat0 = pos.reduce((s, a) => s + a.lat, 0) / pos.length; this.lon0 = pos.reduce((s, a) => s + a.lon, 0) / pos.length; }
    let rayon = 250;
    for (const a of pos) rayon = Math.max(rayon, distanceKm(this.lat0, this.lon0, a.lat, a.lon) * 1.15);
    this.kmPx = Math.max(0.05, Math.min(50, 2 * rayon / Math.min(this.W || 600, this.H || 400)));
    this.dessiner();
  }

  choisirPres(x, y) {
    let meilleur = null, d = 18;
    for (const a of this.avions) {
      if (a.lat === undefined) continue;
      const [ax, ay] = this.versEcran(a.lat, a.lon), e = Math.hypot(ax - x, ay - y);
      if (e < d) { d = e; meilleur = a.icao; }
    }
    this.selection = meilleur; this.surChoix?.(meilleur); this.dessiner();
  }

  dessiner() {
    const c = this.c, g = this.ctx, dpr = window.devicePixelRatio || 1, W = this.W, H = this.H;
    if (!W || !H) return;
    if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); }
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.fillStyle = "#040912"; g.fillRect(0, 0, W, H);

    // Cercles de distance autour de la station (ou du centre de la vue).
    const ref = this.station || { lat: this.lat0, lon: this.lon0 }, [cx, cy] = this.versEcran(ref.lat, ref.lon);
    const pas = [5, 10, 25, 50, 100, 200, 500].find(p => p / this.kmPx > 70) || 500;
    g.strokeStyle = "rgba(57,255,159,.16)"; g.fillStyle = "rgba(57,255,159,.55)"; g.lineWidth = 1; g.font = "11px ui-monospace, monospace";
    for (let r = pas, i = 0; i < 12; r += pas, i++) {
      g.beginPath(); g.arc(cx, cy, r / this.kmPx, 0, 2 * Math.PI); g.stroke();
      g.fillText(`${r} km`, cx + 4, cy - r / this.kmPx - 3);
    }
    g.beginPath(); g.moveTo(cx, 0); g.lineTo(cx, H); g.moveTo(0, cy); g.lineTo(W, cy); g.strokeStyle = "rgba(57,255,159,.08)"; g.stroke();

    // Côtes et frontières.
    const trace = (lignes, style, tirets) => {
      g.strokeStyle = style; g.setLineDash(tirets); g.beginPath();
      const [latH] = this.versGeo(0, 0), [latB] = this.versGeo(0, H), [, lonG] = this.versGeo(0, 0), [, lonD] = this.versGeo(W, 0);
      for (const l of lignes) {
        const [a, b, c, d] = l.boite;
        if (b < latB || a > latH || d < lonG || c > lonD) continue;
        l.forEach(([lon, lat], i) => { const [x, y] = this.versEcran(lat, lon); i ? g.lineTo(x, y) : g.moveTo(x, y); });
      }
      g.stroke(); g.setLineDash([]);
    };
    g.lineWidth = 1.2; trace(COTES, "rgba(0,229,255,.55)", []);
    g.lineWidth = 1; trace(FRONTIERES, "rgba(0,229,255,.28)", [4, 4]);

    if (this.station) {
      g.fillStyle = "#ffd166"; g.beginPath(); g.arc(cx, cy, 4, 0, 2 * Math.PI); g.fill();
      g.fillText("Station", cx + 7, cy + 12);
    }

    // Avions : trace, symbole orienté, étiquette.
    const maintenant = Date.now();
    for (const a of this.avions) {
      if (a.lat === undefined) continue;
      const coul = couleurAltitude(a.altitude), choisi = a.icao === this.selection, vieux = maintenant - a.tPos > 20000;
      if (a.trace.length > 1) {
        g.strokeStyle = coul; g.globalAlpha = choisi ? 0.9 : 0.45; g.lineWidth = choisi ? 2 : 1.2; g.beginPath();
        a.trace.forEach(([lat, lon], i) => { const [x, y] = this.versEcran(lat, lon); i ? g.lineTo(x, y) : g.moveTo(x, y); });
        const [x, y] = this.versEcran(a.lat, a.lon); g.lineTo(x, y); g.stroke(); g.globalAlpha = 1;
      }
      const [x, y] = this.versEcran(a.lat, a.lon);
      g.save(); g.translate(x, y); g.rotate((a.cap ?? 0) * Math.PI / 180);
      g.globalAlpha = vieux ? 0.4 : 1; g.fillStyle = coul; g.strokeStyle = "#040912"; g.lineWidth = 1;
      g.beginPath();   // silhouette d'avion vue de dessus, nez vers le haut
      g.moveTo(0, -9); g.lineTo(1.6, -3); g.lineTo(8, 1); g.lineTo(8, 3); g.lineTo(1.6, 1); g.lineTo(1.2, 6); g.lineTo(3.5, 8);
      g.lineTo(3.5, 9.5); g.lineTo(0, 8.5); g.lineTo(-3.5, 9.5); g.lineTo(-3.5, 8); g.lineTo(-1.2, 6); g.lineTo(-1.6, 1);
      g.lineTo(-8, 3); g.lineTo(-8, 1); g.lineTo(-1.6, -3); g.closePath(); g.fill(); g.stroke();
      g.restore();
      if (choisi) { g.strokeStyle = "#fff"; g.lineWidth = 1.5; g.beginPath(); g.arc(x, y, 14, 0, 2 * Math.PI); g.stroke(); }
      g.globalAlpha = vieux ? 0.5 : 1;
      g.fillStyle = "#e6f1ff"; g.font = "600 11px ui-monospace, monospace"; g.fillText(a.indicatif || a.icao, x + 12, y - 2);
      g.fillStyle = coul; g.font = "11px ui-monospace, monospace";
      g.fillText(a.altitude !== undefined ? `FL${String(Math.round(a.altitude / 100)).padStart(3, "0")}` : "—", x + 12, y + 11);
      g.globalAlpha = 1;
    }

    // Échelle et légende.
    g.fillStyle = "rgba(4,9,18,.75)"; g.fillRect(8, H - 26, 230, 18);
    g.fillStyle = "#9fb3c8"; g.font = "11px ui-monospace, monospace";
    g.fillText(`cercles tous les ${pas} km · molette : zoom`, 14, H - 13);
  }
}
