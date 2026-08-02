// Ghost Track — Live Map Frontend (v3)

var API = '', POLL_MS = 5000, MAX_RETRIES = 3, RETRY_DELAY = 2000;
var COLOR_AMBER = '#c4a860', COLOR_RED = '#e05545', COLOR_SELECTED = '#f5d780';

// ── Map ───────────────────────────────────────────────────────────

var map = L.map('map', {
  attributionControl: true,
  zoomControl: true,
  preferCanvas: true
}).setView([55, 20], 5);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png', {
  attribution: '&copy; OSM · &copy; CARTO · ADS-B: adsb.lol / OpenSky',
  maxZoom: 18,
  subdomains: 'abcd'
}).addTo(map);
// Subtle label layer for city/airport context without washing the map
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_only_labels/{z}/{x}/{y}{r}.png', {
  maxZoom: 18, opacity: 0.55, pane: 'overlayPane'
}).addTo(map);

var markers = new Map();
var trailLayer = L.layerGroup().addTo(map);
var varianceLayer = L.layerGroup().addTo(map);
var heatmapLayer = L.layerGroup().addTo(map);
var bordersLayer = L.layerGroup().addTo(map);
var showHeatmap = false, showBorders = false;

var currentRegion = '', currentReports = [], retryCount = 0, minGhostScore = 0;
var selectedAircraft = null, timelineData = [];

// ── Icons — crisp canvas top-down jet (ATC style) ─────────────────
// Heading: 0° = north, clockwise (aviation true track). Leaflet CSS rotate.
// Aircraft drawn pointing UP (north); we rotate by heading.

var _iconCache = {};

function airplaneIcon(heading, flagged, selected) {
  var rot = (heading != null && !isNaN(heading)) ? Number(heading) : 0;
  // Quantize heading for cache (~2°)
  var qh = Math.round(rot / 2) * 2;
  var key = qh + (flagged ? 'F' : 'C') + (selected ? 'S' : '');
  if (_iconCache[key]) return _iconCache[key];

  var size = selected ? 36 : (flagged ? 32 : 28);
  var dpr = Math.min(window.devicePixelRatio || 1, 2);
  var canvas = document.createElement('canvas');
  canvas.width = size * dpr;
  canvas.height = size * dpr;
  var ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, size, size);

  var cx = size / 2, cy = size / 2;
  ctx.save();
  ctx.translate(cx, cy);
  ctx.rotate((qh * Math.PI) / 180);

  // Soft selection halo
  if (selected) {
    ctx.beginPath();
    ctx.arc(0, 0, size * 0.42, 0, Math.PI * 2);
    ctx.strokeStyle = 'rgba(245, 215, 128, 0.55)';
    ctx.lineWidth = 1.5;
    ctx.stroke();
  } else if (flagged) {
    ctx.beginPath();
    ctx.arc(0, 0, size * 0.40, 0, Math.PI * 2);
    ctx.strokeStyle = 'rgba(224, 85, 69, 0.65)';
    ctx.lineWidth = 1.25;
    ctx.stroke();
  }

  // Jet silhouette pointing north (nose at -Y)
  var s = size * 0.034; // scale unit
  ctx.beginPath();
  // fuselage + nose
  ctx.moveTo(0, -11 * s);
  ctx.lineTo(1.1 * s, -7 * s);
  ctx.lineTo(1.0 * s, 6 * s);
  // right wing
  ctx.lineTo(10 * s, 2.5 * s);
  ctx.lineTo(10.5 * s, 4 * s);
  ctx.lineTo(1.0 * s, 7.5 * s);
  // right stab
  ctx.lineTo(3.2 * s, 10 * s);
  ctx.lineTo(3.2 * s, 11.2 * s);
  ctx.lineTo(0.3 * s, 10 * s);
  // tail tip
  ctx.lineTo(0, 11.5 * s);
  ctx.lineTo(-0.3 * s, 10 * s);
  // left stab
  ctx.lineTo(-3.2 * s, 11.2 * s);
  ctx.lineTo(-3.2 * s, 10 * s);
  ctx.lineTo(-1.0 * s, 7.5 * s);
  // left wing
  ctx.lineTo(-10.5 * s, 4 * s);
  ctx.lineTo(-10 * s, 2.5 * s);
  ctx.lineTo(-1.0 * s, 6 * s);
  ctx.lineTo(-1.1 * s, -7 * s);
  ctx.closePath();

  var fill = flagged ? '#e05545' : '#c9b06a';
  if (selected) fill = '#f5d780';
  ctx.fillStyle = fill;
  ctx.strokeStyle = 'rgba(8, 10, 14, 0.95)';
  ctx.lineWidth = 1.1;
  ctx.lineJoin = 'round';
  ctx.fill();
  ctx.stroke();

  // leading-edge highlight for depth
  ctx.beginPath();
  ctx.moveTo(0, -10.2 * s);
  ctx.lineTo(0.6 * s, -6.5 * s);
  ctx.lineTo(-0.6 * s, -6.5 * s);
  ctx.closePath();
  ctx.fillStyle = 'rgba(255,255,255,0.22)';
  ctx.fill();

  ctx.restore();

  var url = canvas.toDataURL('image/png');
  var icon = L.icon({
    iconUrl: url,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
    className: 'ac-marker' + (flagged ? ' flagged' : '') + (selected ? ' selected' : '')
  });
  _iconCache[key] = icon;
  return icon;
}

// ── API ───────────────────────────────────────────────────────────

function fetchJSON(path, retries) {
  retries = retries || 0;
  return fetch(API + path).then(function(r) {
    if (!r.ok) throw new Error(path + ': ' + r.status);
    return r.json();
  }).then(function(d) { retryCount = 0; return d; }).catch(function(e) {
    if (retries < MAX_RETRIES) return sleep(RETRY_DELAY * (retries + 1)).then(function() { return fetchJSON(path, retries + 1); });
    throw e;
  });
}
function sleep(ms) { return new Promise(function(r) { setTimeout(r, ms); }); }

// ── Status ───────────────────────────────────────────────────────

function setStatus(cls, label) {
  var d = document.getElementById('status-dot'), l = document.getElementById('status-label');
  if (d) d.className = 'status-' + cls;
  if (l) l.textContent = label;
}

function updateStat(id, value) {
  var el = document.getElementById(id), next = String(value);
  if (el.textContent !== next) { el.textContent = next; el.classList.add('changed'); setTimeout(function() { el.classList.remove('changed'); }, 600); }
}

// ── Trail rendering helper ───────────────────────────────────────

function _renderTrail(entry, ac) {
  // Remove old trail layers
  if (entry.trail) trailLayer.removeLayer(entry.trail);
  if (entry.variance) varianceLayer.removeLayer(entry.variance);
  if (entry.anomalyDots) { entry.anomalyDots.forEach(function(d) { trailLayer.removeLayer(d); }); }

  if (!ac.trail || ac.trail.length < 2) return;

  var trailCoords = ac.trail.map(function(p) { return [p.lat, p.lon]; });
  var isSelected = selectedAircraft && ac.icao24 === selectedAircraft.icao24;

  var trailColor, trailWeight, trailOpacity, projColor, projOpacity;
  if (isSelected) {
    trailColor = COLOR_SELECTED; trailWeight = 4; trailOpacity = 0.92;
    projColor = COLOR_SELECTED; projOpacity = 0.30;
  } else {
    trailColor = ac.flagged ? COLOR_RED : COLOR_AMBER;
    trailWeight = 2.5; trailOpacity = 0.55;
    projColor = trailColor; projOpacity = 0.18;
  }

  // Past trail
  entry.trail = L.polyline(trailCoords, {
    color: trailColor, weight: trailWeight, opacity: trailOpacity, interactive: false
  }).addTo(trailLayer);

  // Forward projection
  var latlng = [ac.latitude, ac.longitude];
  if (ac.heading != null && !isNaN(ac.heading) && ac.velocity != null && !isNaN(ac.velocity) && ac.velocity > 10) {
    var hr = ac.heading * Math.PI / 180, dt = 180;
    var dist = (ac.velocity * dt) / 111320;
    var dlat = dist * Math.cos(hr);
    var dlon = dist * Math.sin(hr) / Math.cos(ac.latitude * Math.PI / 180);
    var endLL = [ac.latitude + dlat, ac.longitude + dlon];
    L.polyline([latlng, endLL], {
      color: projColor, weight: 1, opacity: projOpacity, dashArray: '3 10', interactive: false
    }).addTo(trailLayer);
  }

  // Variance band
  var hasVariance = ac.trail.some(function(p) { return p.lat_std_m != null; });
  if (hasVariance) {
    var bandCoords = [];
    ac.trail.forEach(function(p) {
      if (p.lat_std_m != null) bandCoords.push([p.lat + p.lat_std_m / 111320 * 1.5, p.lon]);
    });
    for (var i = ac.trail.length - 1; i >= 0; i--) {
      var pt = ac.trail[i];
      if (pt.lat_std_m != null) bandCoords.push([pt.lat - pt.lat_std_m / 111320 * 1.5, pt.lon]);
    }
    if (bandCoords.length >= 4) {
      entry.variance = L.polygon(bandCoords, {
        color: 'transparent', fillColor: trailColor,
        fillOpacity: isSelected ? 0.12 : 0.05, interactive: false
      }).addTo(varianceLayer);
    }
  }

  // Anomaly dots on trail
  if (ac.anomaly_count > 0) {
    entry.anomalyDots = [];
    ac.trail.forEach(function(p) {
      if (p.innovation != null && p.innovation > 3.0) {
        var dot = L.circleMarker([p.lat, p.lon], {
          radius: isSelected ? 4 : 3, color: COLOR_RED, fillColor: COLOR_RED,
          fillOpacity: 0.8, weight: 1, interactive: false
        }).addTo(trailLayer);
        entry.anomalyDots.push(dot);
      }
    });
  }
}

// ── Map update ───────────────────────────────────────────────────

function updateMap(aircraft) {
  var seen = new Set();

  aircraft.forEach(function(ac) {
    if (Math.abs(ac.latitude) > 90 || Math.abs(ac.longitude) > 180) return;
    seen.add(ac.icao24);
    var latlng = [ac.latitude, ac.longitude];

    var isSel = selectedAircraft && ac.icao24 === selectedAircraft.icao24;
    var entry = markers.get(ac.icao24);
    if (entry) {
      entry.marker.setLatLng(latlng);
      entry.marker.setIcon(airplaneIcon(ac.heading, ac.flagged, isSel));
      // Fade stale tracks
      if (entry.marker._icon) {
        entry.marker._icon.style.opacity = (ac.age_s != null && ac.age_s > 45) ? '0.45' : '1';
      }
    } else {
      entry = {};
      entry.marker = L.marker(latlng, {
        icon: airplaneIcon(ac.heading, ac.flagged, isSel),
        zIndexOffset: ac.flagged ? 800 : 0
      }).addTo(map);
      entry.marker.on('click', function() { openDetail(ac); });
      var tip = (ac.callsign || ac.icao24).trim();
      if (ac.registration) tip += ' · ' + ac.registration;
      if (ac.typecode) tip += ' · ' + ac.typecode;
      tip += ' · ' + formatAlt(ac.altitude) + ' · ' + formatSpd(ac.velocity);
      if (ac.flagged) tip += ' · FLAGGED';
      entry.marker.bindTooltip(tip, { direction: 'top', opacity: 0.95, className: 'ac-tooltip', offset: [0, -8] });
      markers.set(ac.icao24, entry);
    }

    // Render trail (delegated to helper for selected-state awareness)
    _renderTrail(entry, ac);

    entry.ac = ac;
  });

  // Remove stale markers and layers
  markers.forEach(function(entry, id) {
    if (!seen.has(id)) {
      map.removeLayer(entry.marker);
      if (entry.trail) trailLayer.removeLayer(entry.trail);
      if (entry.vector) trailLayer.removeLayer(entry.vector);
      if (entry.variance) varianceLayer.removeLayer(entry.variance);
      markers.delete(id);
    }
  });

  // Heatmap
  if (showHeatmap) drawHeatmap(aircraft);
}

// ── Heatmap ──────────────────────────────────────────────────────

function drawHeatmap(aircraft) {
  heatmapLayer.clearLayers();
  var flagged = aircraft.filter(function(a) { return a.flagged; });
  if (!flagged.length) return;

  // Simple grid-based heatmap
  var grid = {}, cellSize = 0.5;
  flagged.forEach(function(ac) {
    var cx = Math.round(ac.latitude / cellSize) * cellSize;
    var cy = Math.round(ac.longitude / cellSize) * cellSize;
    var key = cx + ',' + cy;
    grid[key] = (grid[key] || 0) + 1;
  });

  var maxCount = 0;
  Object.keys(grid).forEach(function(k) { if (grid[k] > maxCount) maxCount = grid[k]; });
  if (!maxCount) return;

  Object.keys(grid).forEach(function(k) {
    var parts = k.split(','), lat = parseFloat(parts[0]), lon = parseFloat(parts[1]);
    var intensity = grid[k] / maxCount;
    var alpha = 0.1 + intensity * 0.5;
    L.rectangle([[lat, lon], [lat + cellSize, lon + cellSize]], {
      color: 'transparent', fillColor: COLOR_RED, fillOpacity: alpha,
      weight: 0, interactive: false
    }).addTo(heatmapLayer);
  });
}

// ── Borders ──────────────────────────────────────────────────────

var borderData = {
  baltic: [
    [53.5, 9.5], [53.5, 30.5], [62.5, 30.5], [62.5, 9.5], [53.5, 9.5]  // Approximate Baltic bbox
  ],
  kaliningrad: [
    [54.3, 19.8], [54.3, 22.8], [55.0, 22.8], [55.0, 19.8], [54.3, 19.8]
  ]
};

function toggleBorders() {
  showBorders = !showBorders;
  document.getElementById('btn-borders').classList.toggle('active', showBorders);
  if (showBorders) {
    L.polyline(borderData.baltic, { color: '#4a5568', weight: 1, dashArray: '8 4', interactive: false }).addTo(bordersLayer);
    L.circleMarker([54.7, 20.5], { radius: 6, color: COLOR_RED, fillColor: COLOR_RED, fillOpacity: 0.3, weight: 1 })
      .bindTooltip('Known jammer: Kaliningrad').addTo(bordersLayer);
    L.circleMarker([35.0, 33.0], { radius: 6, color: COLOR_AMBER, fillColor: COLOR_AMBER, fillOpacity: 0.3, weight: 1 })
      .bindTooltip('Known jammer: E. Mediterranean').addTo(bordersLayer);
  } else {
    bordersLayer.clearLayers();
  }
}

// ── Reports ──────────────────────────────────────────────────────

function updateReports(reports) {
  var filtered = reports;
  if (minGhostScore > 0) {
    filtered = reports.filter(function(r) {
      var gs = (r.ghost_score != null) ? r.ghost_score : 0;
      return gs >= minGhostScore;
    });
  }
  currentReports = reports;

  var el = document.getElementById('reports-list');
  if (!el) return;
  if (!filtered.length) {
    el.innerHTML = '<div class="empty-state">' +
      (minGhostScore > 0 ? 'No alerts above GS ' + minGhostScore + '.' : 'Detectors idle · 0 flags in window · next poll ~5s') +
      '</div>';
    return;
  }

  var existing = {};
  el.querySelectorAll('.report-card').forEach(function(c) { existing[c.dataset.id] = c; });
  var newIds = {}, html = '';

  filtered.forEach(function(r) {
    newIds[r.incident_id] = true;
    if (!existing[r.incident_id]) {
      var gs = (r.ghost_score != null) ? r.ghost_score : 0;
      var gsLabel = gs >= 70 ? 'high' : gs >= 40 ? 'med' : 'low';
      var region = (r.region || '').replace(/_/g, ' ');
      var chips = evidenceChipsHtml(r.evidence);
      // article — not nested buttons
      html += '<article class="report-card new" data-id="' + r.incident_id + '" tabindex="0" role="button" aria-expanded="false">' +
        '<div class="report-card-inner">' +
          '<div class="report-card-header">' +
            '<span class="sev sev-' + r.severity_score + '" title="Severity">' + r.severity_score + '</span>' +
            '<span class="gs gs-' + gsLabel + '">GS ' + gs + '</span>' +
            '<span class="region-chip">' + esc(region) + '</span>' +
            '<span class="time">' + formatTime(r.time_start) + '</span>' +
          '</div>' +
          '<div class="action-line">' + esc(r.recommended_action || 'Manual review') + '</div>' +
          '<div class="summary">' + esc(r.summary).substring(0, 160) + '</div>' +
          '<div class="report-expanded">' +
            '<div class="summary-full">' + esc(r.summary) + '</div>' +
            '<div class="chip-row">' + chips + '</div>' +
            '<div class="aircraft">' + (r.aircraft_ids || []).join(', ') + ' · ' + (r.anomaly_count || 0) + ' flags</div>' +
            '<div class="report-actions">' +
              '<button type="button" class="btn-fly" data-id="' + r.incident_id + '">Zoom to region</button>' +
              '<button type="button" class="btn-export" data-id="' + r.incident_id + '">Export</button>' +
            '</div>' +
          '</div>' +
        '</div></article>';
    }
  });

  if (html) {
    el.insertAdjacentHTML('afterbegin', html);
    setTimeout(function() { el.querySelectorAll('.report-card.new').forEach(function(c) { c.classList.remove('new'); }); }, 400);
  }

  Object.keys(existing).forEach(function(id) { if (!newIds[id] && existing[id].parentNode) existing[id].remove(); });

  var cards = el.querySelectorAll('.report-card');
  for (var i = 50; i < cards.length; i++) cards[i].remove();

  el.querySelectorAll('.report-card').forEach(function(card) {
    if (card._bound) return; card._bound = true;
    function toggle() {
      var was = card.classList.contains('expanded');
      el.querySelectorAll('.report-card.expanded').forEach(function(c) {
        c.classList.remove('expanded');
        c.setAttribute('aria-expanded', 'false');
      });
      if (!was) {
        card.classList.add('expanded');
        card.setAttribute('aria-expanded', 'true');
      }
    }
    card.addEventListener('click', function(e) {
      if (e.target.closest('.btn-fly, .btn-export')) return;
      toggle();
    });
    card.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    });
  });
  el.querySelectorAll('.btn-fly').forEach(function(btn) {
    if (btn._bound) return; btn._bound = true;
    btn.addEventListener('click', function(e) { e.stopPropagation(); flyToReport(btn.dataset.id); });
  });
  el.querySelectorAll('.btn-export').forEach(function(btn) {
    if (btn._bound) return; btn._bound = true;
    btn.addEventListener('click', function(e) { e.stopPropagation(); exportReport(btn.dataset.id); });
  });
}

function evidenceChipsHtml(ev) {
  ev = ev || {};
  function chip(label, status, title) {
    var st = status || 'unavailable';
    if (st === 'ok' || (st && st !== 'unavailable' && st !== 'pending' && st !== 'cross_check_unavailable' && st !== 'aircraft_not_seen')) {
      st = (st === 'probable_spoofing' || st === 'positions_corroborated') ? 'ok' : (st === 'ok' ? 'ok' : 'warn');
    } else if (st === 'pending') st = 'warn';
    else st = 'unavailable';
    return '<span class="chip chip-' + st + '" title="' + esc(title || label) + '">' + label + '</span>';
  }
  var wx = ev.weather === 'ok' ? 'ok' : 'unavailable';
  var xc = ev.xcheck || 'unavailable';
  var jam = ev.jam_zone ? 'ok' : 'unavailable';
  return chip('WEATHER', wx, ev.weather_note || 'Weather') +
    chip('XCHECK', xc, ev.xcheck_note || xc) +
    chip('JAM', jam, ev.jam_zone ? (ev.jam_zone + ' · ' + (ev.jam_level || '')) : 'No jam zone') +
    chip('ID', ev.identity || 'pending', 'Aircraft identity');
}

function flyToReport(id) {
  var r = currentReports.find(function(rr) { return rr.incident_id === id; });
  if (!r) return;
  fetchJSON('/api/regions').then(function(d) {
    var bbox = d.regions[r.region];
    if (bbox) map.fitBounds([[bbox.min_lat, bbox.min_lon], [bbox.max_lat, bbox.max_lon]], { padding: [50, 50], maxZoom: 8 });
  }).catch(function() {});
}

function exportReport(id) {
  fetchJSON('/api/export/' + id).then(function(d) {
    var text = 'GHOST TRACK — INCIDENT REPORT\n' +
      '================================\n' +
      'ID: ' + d.incident.incident_id + '\n' +
      'Severity: ' + d.incident.severity_score + '/5\n' +
      'Region: ' + d.incident.region + '\n' +
      'Aircraft: ' + d.incident.aircraft_ids.join(', ') + '\n' +
      'Time: ' + d.incident.time_start + ' to ' + d.incident.time_end + '\n\n' +
      'SUMMARY\n' + d.incident.summary + '\n\n' +
      'RECOMMENDED ACTION\n' + d.incident.recommended_action + '\n\n' +
      'RELATED ANOMALIES: ' + d.related_anomalies.length + '\n' +
      'EXPORTED: ' + d.exported_at + '\n' +
      d.disclaimer;
    var blob = new Blob([text], { type: 'text/plain' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a'); a.href = url; a.download = 'ghost-track-' + id + '.txt'; a.click();
    showToast('Report exported');
  }).catch(function() { showToast('Export failed'); });
}

// ── Detail panel ─────────────────────────────────────────────────

function formatAlt(m) {
  if (m == null || isNaN(m)) return '—';
  if (m >= 1000) return 'FL' + Math.round(m / 30.48);
  return Math.round(m * 3.28084) + ' ft';
}
function formatSpd(ms) {
  if (ms == null || isNaN(ms)) return '—';
  return Math.round(ms * 1.94384) + ' kt';
}
function formatVr(ms) {
  if (ms == null || isNaN(ms)) return '—';
  var fpm = Math.round(ms * 196.85);
  return (fpm >= 0 ? '+' : '') + fpm + ' fpm';
}

function openDetail(ac) {
  selectedAircraft = ac;
  var panel = document.getElementById('detail-panel');
  document.getElementById('detail-title').textContent = (ac.callsign || ac.icao24);
  panel.classList.remove('detail-hidden');

  var alt = formatAlt(ac.altitude);
  var spd = formatSpd(ac.velocity);
  var hdg = ac.heading != null ? Math.round(ac.heading) + '°' : '—';
  var vr = formatVr(ac.vertical_rate);
  var flagged = ac.flagged ? 'FLAGGED (' + ac.anomaly_count + ')' : 'CLEAN';
  var age = ac.age_s != null ? Math.round(ac.age_s) + 's' : '—';

  var chipsEl = document.getElementById('detail-chips');
  if (chipsEl) {
    chipsEl.innerHTML = evidenceChipsHtml({
      weather: 'unavailable',
      xcheck: 'unavailable',
      jam_zone: null,
      identity: ac.identity_status || 'pending'
    });
  }

  document.getElementById('detail-stats').innerHTML =
    '<div class="detail-stat"><div class="detail-stat-label">ICAO24</div><div class="detail-stat-value">' + esc(ac.icao24) + '</div></div>' +
    '<div class="detail-stat"><div class="detail-stat-label">Reg / Type</div><div class="detail-stat-value">' + esc(ac.registration || '—') + ' / ' + esc(ac.typecode || '—') + '</div></div>' +
    '<div class="detail-stat"><div class="detail-stat-label">Operator</div><div class="detail-stat-value">' + esc(ac.operator || '—') + '</div></div>' +
    '<div class="detail-stat"><div class="detail-stat-label">Status</div><div class="detail-stat-value" style="color:' + (ac.flagged ? COLOR_RED : 'inherit') + '">' + flagged + '</div></div>' +
    '<div class="detail-stat"><div class="detail-stat-label">Altitude</div><div class="detail-stat-value">' + alt + '</div></div>' +
    '<div class="detail-stat"><div class="detail-stat-label">Speed</div><div class="detail-stat-value">' + spd + '</div></div>' +
    '<div class="detail-stat"><div class="detail-stat-label">VS</div><div class="detail-stat-value">' + vr + '</div></div>' +
    '<div class="detail-stat"><div class="detail-stat-label">Heading</div><div class="detail-stat-value">' + hdg + '</div></div>' +
    '<div class="detail-stat"><div class="detail-stat-label">Squawk</div><div class="detail-stat-value">' + esc(ac.squawk || '—') + '</div></div>' +
    '<div class="detail-stat"><div class="detail-stat-label">Age</div><div class="detail-stat-value">' + age + '</div></div>' +
    '<div class="detail-stat"><div class="detail-stat-label">Lat / Lon</div><div class="detail-stat-value">' + ac.latitude.toFixed(4) + ', ' + ac.longitude.toFixed(4) + '</div></div>' +
    '<div class="detail-stat"><div class="detail-stat-label">Region</div><div class="detail-stat-value">' + esc(ac.region) + '</div></div>';

  drawAltChart(ac);

  fetchJSON('/api/anomalies?limit=200').then(function(d) {
    var related = d.anomalies.filter(function(a) { return a.icao24 === ac.icao24; }).slice(-6);
    var html = '<h3>Recent Anomalies</h3>';
    if (!related.length) html += '<div class="detail-anomaly">None detected</div>';
    related.forEach(function(a) {
      var typeLabel = (a.flag_type || '').replace(/_/g, ' ');
      if (typeLabel === 'position jump') typeLabel = 'Position jump';
      if (typeLabel === 'cusum drift') typeLabel = 'CUSUM drift';
      html += '<div class="detail-anomaly"><span class="da-type">' + esc(typeLabel) + '</span> ' +
        '<span class="da-value">' + (a.mahalanobis_distance != null ? a.mahalanobis_distance + 'σ' : '') +
        ' · GS ' + (a.ghost_score || 0) + '</span></div>';
      if (a.evidence && chipsEl) {
        chipsEl.innerHTML = evidenceChipsHtml(Object.assign({ identity: ac.identity_status }, a.evidence));
      }
    });
    document.getElementById('detail-anomalies').innerHTML = html;
  }).catch(function() {});

  document.getElementById('detail-export').onclick = function() {
    var text = 'AIRCRAFT REPORT\n===============\n' +
      'ICAO24: ' + ac.icao24 + '\nCallsign: ' + (ac.callsign || 'N/A') + '\n' +
      'Reg: ' + (ac.registration || 'N/A') + ' Type: ' + (ac.typecode || 'N/A') + '\n' +
      'Position: ' + ac.latitude.toFixed(4) + ', ' + ac.longitude.toFixed(4) + '\n' +
      'Altitude: ' + alt + '\nSpeed: ' + spd + '\nHeading: ' + hdg + '\n' +
      'Region: ' + ac.region + '\nStatus: ' + flagged + '\n';
    var blob = new Blob([text], { type: 'text/plain' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a'); a.href = url; a.download = 'aircraft-' + ac.icao24 + '.txt'; a.click();
  };

  var entry = markers.get(ac.icao24);
  if (entry) {
    entry.marker.setIcon(airplaneIcon(ac.heading, ac.flagged, true));
    if (entry.ac) _renderTrail(entry, entry.ac);
  }

  if (ac.trail && ac.trail.length >= 2) {
    try {
      var bounds = L.latLngBounds(ac.trail.map(function(p) { return [p.lat, p.lon]; }));
      if (bounds.isValid()) map.fitBounds(bounds, { padding: [50, 50], maxZoom: 12 });
    } catch (e) { /* ignore */ }
  }
}

function drawAltChart(ac) {
  var canvas = document.getElementById('detail-alt-chart');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');
  var w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  if (!ac.trail || ac.trail.length < 2) {
    ctx.fillStyle = '#484f58'; ctx.font = '11px sans-serif';
    ctx.fillText('Insufficient data for altitude profile', 10, h / 2);
    return;
  }

  var alts = ac.trail.map(function(p) { return p.alt; }).filter(function(a) { return a != null; });
  if (alts.length < 2) return;

  var minAlt = Math.min.apply(null, alts), maxAlt = Math.max.apply(null, alts);
  var range = maxAlt - minAlt || 1000;
  var pad = range * 0.1;
  minAlt -= pad; maxAlt += pad; range = maxAlt - minAlt;

  // Grid lines
  ctx.strokeStyle = '#2d3139'; ctx.lineWidth = 0.5;
  for (var i = 0; i < 3; i++) {
    var y = 10 + (h - 20) * i / 2;
    ctx.beginPath(); ctx.moveTo(30, y); ctx.lineTo(w - 10, y); ctx.stroke();
    var altLabel = Math.round(maxAlt - range * i / 2) + 'm';
    ctx.fillStyle = '#6b7280'; ctx.font = '9px monospace';
    ctx.fillText(altLabel, 2, y + 3);
  }

  // Altitude line
  ctx.strokeStyle = COLOR_AMBER; ctx.lineWidth = 1.5; ctx.beginPath();
  alts.forEach(function(a, i) {
    var x = 30 + (w - 40) * i / (alts.length - 1);
    var y = 10 + (h - 20) * (1 - (a - minAlt) / range);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Fill
  ctx.lineTo(w - 10, h - 10); ctx.lineTo(30, h - 10); ctx.closePath();
  ctx.fillStyle = COLOR_AMBER + '15'; ctx.fill();
}

function closeDetail() {
  var prevSelected = selectedAircraft;
  document.getElementById('detail-panel').classList.add('detail-hidden');
  selectedAircraft = null;
  if (prevSelected) {
    var entry = markers.get(prevSelected.icao24);
    if (entry && entry.ac) {
      entry.marker.setIcon(airplaneIcon(entry.ac.heading, entry.ac.flagged, false));
      _renderTrail(entry, entry.ac);
    }
  }
}
document.getElementById('detail-close').addEventListener('click', closeDetail);
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeDetail();
});

// ── Threat bar ───────────────────────────────────────────────────

function updateThreats() {
  fetchJSON('/api/threats').then(function(d) {
    var bar = document.getElementById('threat-bar');
    var html = '';
    Object.keys(d.threats).forEach(function(name) {
      var t = d.threats[name];
      var label = name.replace(/_/g, ' ').toUpperCase();
      var short = label.length > 14 ? label.slice(0, 12) + '…' : label;
      html += '<button type="button" class="threat-indicator threat-' + t.level + '" data-region="' + name + '" title="' +
        esc(label) + ': ' + esc(t.label) + ' (' + t.anomaly_count + ' anomalies / ' + t.aircraft_count + ' ac)">' +
        '<span class="threat-dot" aria-hidden="true"></span>' +
        '<span>' + esc(short) + ' · ' + esc(t.level) + '</span></button>';
    });
    bar.innerHTML = html;
    bar.querySelectorAll('.threat-indicator').forEach(function(btn) {
      btn.addEventListener('click', function() {
        var sel = document.getElementById('region-select');
        if (sel) {
          sel.value = btn.dataset.region;
          sel.dispatchEvent(new Event('change'));
        }
      });
    });
  }).catch(function() {});
}

function updateModeBanner(mode, detail, lastPollIso, source) {
  var el = document.getElementById('mode-banner');
  if (!el) return;
  mode = (mode || 'connecting').toLowerCase();
  el.className = 'mode-' + mode;
  var label = document.getElementById('mode-label');
  var det = document.getElementById('mode-detail');
  var age = document.getElementById('last-poll-age');
  if (label) label.textContent = (mode || 'CONNECTING').toUpperCase();
  if (det) {
    if (mode === 'live') det.textContent = detail || ('live ADS-B' + (source ? ' · ' + source : ''));
    else if (mode === 'synthetic') det.textContent = detail || 'demo fleet — not live airspace';
    else if (mode === 'degraded') det.textContent = detail || 'feed errors — data may be stale';
    else det.textContent = detail || 'establishing feed';
  }
  if (age && lastPollIso) {
    var secs = Math.max(0, Math.round((Date.now() - new Date(lastPollIso).getTime()) / 1000));
    age.textContent = 'poll ' + secs + 's ago';
  }
}

// ── Timeline ─────────────────────────────────────────────────────

function updateTimeline() {
  fetchJSON('/api/timeline').then(function(d) {
    timelineData = d.timeline;
    drawTimeline();
  }).catch(function() {});
}

function drawTimeline() {
  var canvas = document.getElementById('timeline-canvas');
  if (!canvas || !timelineData.length) return;
  canvas.width = canvas.offsetWidth * 2;
  canvas.height = 72;
  var ctx = canvas.getContext('2d'), w = canvas.width, h = canvas.height;

  ctx.clearRect(0, 0, w, h);

  var values = timelineData.map(function(d) { return d.count; });
  var maxVal = Math.max.apply(null, values) || 1;
  var barW = Math.max(2, (w - 20) / values.length - 1);

  values.forEach(function(v, i) {
    var barH = Math.max(1, (v / maxVal) * (h - 20));
    var x = 10 + i * (barW + 1);
    var alpha = 0.3 + (v / maxVal) * 0.7;
    ctx.fillStyle = v > 0 ? COLOR_RED + Math.round(alpha * 255).toString(16).padStart(2, '0') : '#2d3139';
    ctx.fillRect(x, h - 10 - barH, barW, barH);
  });

  // Labels
  ctx.fillStyle = '#6b7280'; ctx.font = '9px monospace';
  ctx.fillText('60m ago', 10, h - 2);
  ctx.fillText('now', w - 30, h - 2);
  ctx.fillText(maxVal + '', w - 30, 12);
}

// ── Heatmap toggle ───────────────────────────────────────────────

document.getElementById('btn-heatmap').addEventListener('click', function() {
  showHeatmap = !showHeatmap;
  this.classList.toggle('active', showHeatmap);
  if (!showHeatmap) heatmapLayer.clearLayers();
});

document.getElementById('btn-borders').addEventListener('click', toggleBorders);

// ── Toast ────────────────────────────────────────────────────────

function showToast(msg) {
  var t = document.getElementById('toast');
  t.textContent = msg; t.classList.remove('toast-hidden'); t.classList.add('toast-visible');
  clearTimeout(t._timeout);
  t._timeout = setTimeout(function() { t.classList.remove('toast-visible'); t.classList.add('toast-hidden'); }, 3000);
}

// ── Utils ────────────────────────────────────────────────────────

function formatTime(iso) {
  if (!iso) return '';
  var d = new Date(iso);
  return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
}
function esc(s) {
  if (!s) return '';
  var d = document.createElement('div'); d.textContent = s; return d.innerHTML;
}

// ── Polling ──────────────────────────────────────────────────────

function loadAircraft() {
  return fetchJSON('/api/aircraft' + (currentRegion ? '?region=' + currentRegion : '')).then(function(d) {
    updateMap(d.aircraft);
    updateStat('stat-aircraft', d.aircraft.length);
    var flagged = (d.aircraft || []).filter(function(a) { return a.flagged; }).length;
    updateStat('stat-flagged', flagged);
    if (d.data_mode) updateModeBanner(d.data_mode, null, d.time);
    setStatus('live', (d.data_mode || 'live').toLowerCase());
  });
}
function loadReports() {
  return fetchJSON('/api/reports?limit=50').then(function(d) {
    updateReports(d.reports);
    updateStat('stat-reports', d.reports.length);
  });
}
function loadStats() {
  return fetchJSON('/api/stats').then(function(d) {
    updateStat('stat-anomalies', d.anomalies_total);
    if (d.flagged_aircraft != null) updateStat('stat-flagged', d.flagged_aircraft);
    var lastPoll = d.last_poll;
    var source = '';
    if (d.regions) {
      Object.keys(d.regions).forEach(function(k) {
        if (d.regions[k].last_poll) lastPoll = d.regions[k].last_poll;
        if (d.regions[k].source && d.regions[k].source !== 'none') source = d.regions[k].source;
      });
    }
    updateModeBanner(d.data_mode || 'connecting', null, lastPoll, source);
    if (d.errors > 0 && d.data_mode === 'DEGRADED') setStatus('error', d.errors + ' errors');
  }).catch(function() {});
}
function loadRegions() {
  return fetchJSON('/api/regions').then(function(d) {
    var sel = document.getElementById('region-select');
    Object.keys(d.regions).forEach(function(n) {
      var o = document.createElement('option'); o.value = n;
      o.textContent = n.replace(/_/g, ' ').replace(/\b\w/g, function(c) { return c.toUpperCase(); });
      sel.appendChild(o);
    });
  });
}

var pollTimer;
function poll() {
  Promise.all([loadAircraft(), loadReports(), loadStats()]).then(function() {
    document.getElementById('status-label').textContent = 'live';
  }).catch(function(e) {
    console.error('Poll error:', e);
    retryCount++; setStatus('error', 'retrying'); showToast('Connection lost. Retrying...');
    if (retryCount >= MAX_RETRIES) { setStatus('error', 'offline'); clearInterval(pollTimer); }
  });
  // Slower polls for threat/timeline
  updateThreats();
}

// ── Region switch ────────────────────────────────────────────────

document.getElementById('region-select').addEventListener('change', function(e) {
  currentRegion = e.target.value;
  markers.forEach(function(entry) {
    map.removeLayer(entry.marker);
    if (entry.trail) trailLayer.removeLayer(entry.trail);
    if (entry.vector) trailLayer.removeLayer(entry.vector);
  });
  markers.clear();
  trailLayer.clearLayers();
  varianceLayer.clearLayers();
  heatmapLayer.clearLayers();
  loadAircraft();
});

// ── Ghost Score filter ───────────────────────────────────────────

document.getElementById('ghost-score-filter').addEventListener('input', function() {
  minGhostScore = parseInt(this.value);
  document.getElementById('ghost-score-value').textContent = minGhostScore;
  if (currentReports.length) updateReports(currentReports);
});

// ── Init ─────────────────────────────────────────────────────────

loadRegions();
poll();
pollTimer = setInterval(poll, POLL_MS);
setInterval(updateTimeline, 30000); // Timeline every 30s
updateTimeline();

document.getElementById('replay-tutorial').addEventListener('click', function() {
  if (window.ghostTrackTour) window.ghostTrackTour.start();
});
