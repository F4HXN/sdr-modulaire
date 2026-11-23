// SPDX-License-Identifier: MIT
// Carte radar autonome pour le module ADS-B : côtes, frontières, cercles de distance,
// avions orientés selon leur cap avec leur trace, points (stations APRS : rond coloré selon
// le symbole, indicatif, ancienneté, trace) et navires AIS (orientés selon leur route, vecteur vitesse
// sur 6 minutes, carré pour une station côtière, losange pour une aide à la navigation). Molette : zoom ; glisser : déplacer ;
// clic : choisir un avion ou un point ; double-clic : recentrer.

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

/** Couleur d'un symbole APRS (table + code) : véhicules, météo, relais, domicile, autres. */
export function couleurSymbole(s = "") {
  const c = s[1] || "";
  if ("_W".includes(c) && s[0] === "/") return "#4fc3f7";                       // météo
  if (c === "#" || c === "&") return "#d98cff";                                 // relais numérique, passerelle
  if ("-yI".includes(c)) return "#ffd166";                                      // domicile, station fixe
  if ("<>OPRUXabfjkuvs'=".includes(c)) return "#39ff9f";                        // véhicules, piétons, bateaux, avions
  return "#7fd4ff";
}
const anciennete = t => { const s = (Date.now() - t) / 1000; return s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${Math.round(s / 3600)} h`; };

export class CarteRadar {
  constructor(canvas, { surChoix } = {}) {
    this.c = canvas; this.ctx = canvas.getContext("2d");
    this.lat0 = 46.6; this.lon0 = 2.4; this.kmPx = 2.5;   // km par pixel
    this.selection = null; this.surChoix = surChoix;
    this.avions = []; this.points = []; this.navires = []; this.station = null;
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
    const pos = [...this.avions, ...this.points, ...this.navires].filter(a => a.lat !== undefined);
    if (this.station) { this.lat0 = this.station.lat; this.lon0 = this.station.lon; }
    else if (pos.length) { this.lat0 = pos.reduce((s, a) => s + a.lat, 0) / pos.length; this.lon0 = pos.reduce((s, a) => s + a.lon, 0) / pos.length; }
    let rayon = this.navires.length && !this.avions.length ? 10 : 250;
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
    for (const p of this.points) {
      const [px, py] = this.versEcran(p.lat, p.lon), e = Math.hypot(px - x, py - y);
      if (e < d) { d = e; meilleur = p.id; }
    }
    for (const n of this.navires) {
      const [px, py] = this.versEcran(n.lat, n.lon), e = Math.hypot(px - x, py - y);
      if (e < d) { d = e; meilleur = n.mmsi; }
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
    const pas = [1, 2, 5, 10, 25, 50, 100, 200, 500].find(p => p / this.kmPx > 70) || 500;
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

    // Points (stations APRS) : trace, rond coloré, indicatif et ancienneté ; pâlis après 30 min.
    for (const p of this.points) {
      const [x, y] = this.versEcran(p.lat, p.lon), coul = couleurSymbole(p.symbole), choisi = p.id === this.selection;
      const vieux = Date.now() - p.vu > 30 * 60000;
      if (p.trace?.length > 1) {
        g.strokeStyle = coul; g.globalAlpha = choisi ? 0.9 : 0.45; g.lineWidth = choisi ? 2 : 1.2; g.beginPath();
        p.trace.forEach(([lat, lon], i) => { const [tx, ty] = this.versEcran(lat, lon); i ? g.lineTo(tx, ty) : g.moveTo(tx, ty); });
        g.stroke(); g.globalAlpha = 1;
      }
      g.globalAlpha = vieux ? 0.45 : 1;
      g.fillStyle = coul; g.strokeStyle = "#040912"; g.lineWidth = 1.5;
      g.beginPath(); g.arc(x, y, 5.5, 0, 2 * Math.PI); g.fill(); g.stroke();
      if (choisi) { g.strokeStyle = "#fff"; g.lineWidth = 1.5; g.beginPath(); g.arc(x, y, 12, 0, 2 * Math.PI); g.stroke(); }
      g.fillStyle = "#e6f1ff"; g.font = "600 11px ui-monospace, monospace"; g.fillText(p.id, x + 9, y - 2);
      g.fillStyle = coul; g.font = "11px ui-monospace, monospace"; g.fillText(anciennete(p.vu), x + 9, y + 11);
      g.globalAlpha = 1;
    }

    // Navires AIS : trace, symbole orienté selon la route (vecteur vitesse sur 6 min), étiquette ; pâlis après 10 min.
    for (const n of this.navires) {
      const [x, y] = this.versEcran(n.lat, n.lon), coul = n.couleur, choisi = n.mmsi === this.selection;
      const vieux = Date.now() - n.vu > 10 * 60000;
      if (n.trace?.length > 1) {
        g.strokeStyle = coul; g.globalAlpha = choisi ? 0.9 : 0.45; g.lineWidth = choisi ? 2 : 1.2; g.beginPath();
        n.trace.forEach(([lat, lon], i) => { const [tx, ty] = this.versEcran(lat, lon); i ? g.lineTo(tx, ty) : g.moveTo(tx, ty); });
        g.stroke(); g.globalAlpha = 1;
      }
      g.globalAlpha = vieux ? 0.45 : 1;
      g.fillStyle = coul; g.strokeStyle = "#040912"; g.lineWidth = 1.2;
      g.beginPath();
      if (n.classe === "station de base") g.rect(x - 4.5, y - 4.5, 9, 9);
      else if (n.classe === "aide à la navigation") { g.moveTo(x, y - 6); g.lineTo(x + 6, y); g.lineTo(x, y + 6); g.lineTo(x - 6, y); g.closePath(); }
      else if (n.route_deg != null && (n.vitesse_nd ?? 0) >= 0.5) {
        g.save(); g.translate(x, y); g.rotate(n.route_deg * Math.PI / 180);
        g.moveTo(0, -10); g.lineTo(4, -3); g.lineTo(4, 7); g.lineTo(-4, 7); g.lineTo(-4, -3); g.closePath(); g.fill(); g.stroke();   // coque, étrave vers la route
        g.beginPath(); g.strokeStyle = coul; g.lineWidth = 1;
        g.moveTo(0, -10); g.lineTo(0, -10 - n.vitesse_nd * 1.852 * 0.1 / this.kmPx); g.stroke();
        g.restore(); g.beginPath();
      } else g.arc(x, y, 4.5, 0, 2 * Math.PI);
      g.fill(); g.stroke();
      if (choisi) { g.strokeStyle = "#fff"; g.lineWidth = 1.5; g.beginPath(); g.arc(x, y, 13, 0, 2 * Math.PI); g.stroke(); }
      g.fillStyle = "#e6f1ff"; g.font = "600 11px ui-monospace, monospace"; g.fillText(n.nom || String(n.mmsi).padStart(9, "0"), x + 10, y - 2);
      g.fillStyle = coul; g.font = "11px ui-monospace, monospace";
      g.fillText(`${n.vitesse_nd != null ? `${n.vitesse_nd.toFixed(1).replace(".", ",")} nd · ` : ""}${anciennete(n.vu)}`, x + 10, y + 11);
      g.globalAlpha = 1;
    }

    // Échelle et légende.
    g.fillStyle = "rgba(4,9,18,.75)"; g.fillRect(8, H - 26, 230, 18);
    g.fillStyle = "#9fb3c8"; g.font = "11px ui-monospace, monospace";
    g.fillText(`cercles tous les ${pas} km · molette : zoom`, 14, H - 13);
  }
}
