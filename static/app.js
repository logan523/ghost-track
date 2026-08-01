// Ghost Track — Live Map Frontend (v3)

var API = '', POLL_MS = 5000, MAX_RETRIES = 3, RETRY_DELAY = 2000;
var COLOR_AMBER = '#c4a860', COLOR_RED = '#e05545', COLOR_SELECTED = '#f5d780';

// ── Map ───────────────────────────────────────────────────────────

var map = L.map('map', { attributionControl: false, zoomControl: true }).setView([55, 20], 6);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution: '&copy; OSM', maxZoom: 18
}).addTo(map);

var markers = new Map();
var trailLayer = L.layerGroup().addTo(map);
var varianceLayer = L.layerGroup().addTo(map);
var heatmapLayer = L.layerGroup().addTo(map);
var bordersLayer = L.layerGroup().addTo(map);
var showHeatmap = false, showBorders = false;

var currentRegion = '', currentReports = [], retryCount = 0, minGhostScore = 0;
var selectedAircraft = null, timelineData = [];

// ── Icons ─────────────────────────────────────────────────────────

function airplaneIcon(heading, flagged) {
  var rot = (heading != null && !isNaN(heading)) ? heading : 0;
  var body    = flagged ? COLOR_RED   : COLOR_AMBER;
  var wing    = flagged ? '#b33a2e'   : '#9a8560';
  var engine  = flagged ? '#7a2520'   : '#5a5340';
  var glass   = flagged ? '#5a1010'   : '#1a1f2b';
  var size    = flagged ? 36 : 28;

  // viewBox 0 0 48 48, plane centered (24,24), nose points right (east / 0°)
  // Render order: shadow → stab → wings → engines → fuselage → details
  return L.divIcon({
    className: flagged ? 'aircraft-icon flagged' : 'aircraft-icon',
    html: '<svg width="' + size + '" height="' + size + '" viewBox="0 0 48 48">' +
      '<g transform="rotate(' + rot + ' 24 24)">' +
        // Ground shadow
        '<ellipse cx="24" cy="27" rx="18" ry="4.5" fill="#000" opacity="0.22"/>' +
        // Horizontal stabilizer (small, at tail)
        '<path d="M12,23.5 L4,16 L1,17.5 L9,23.5 Z" fill="' + wing + '" opacity="0.55"/>' +
        '<path d="M12,24.5 L4,32 L1,30.5 L9,24.5 Z" fill="' + wing + '" opacity="0.55"/>' +
        // Main wings — swept back ~35°, wide span
        '<path d="M22,21 L6,5 L3,8 L18,21 Z" fill="' + wing + '" opacity="0.8"/>' +
        '<path d="M22,27 L6,43 L3,40 L18,27 Z" fill="' + wing + '" opacity="0.8"/>' +
        // Engine nacelles (podded under wings, angled with sweep)
        '<rect x="12" y="11" rx="2" ry="2" width="7" height="3.5" fill="' + engine + '" opacity="0.7" transform="rotate(-12 15.5 12.75)"/>' +
        '<rect x="12" y="33.5" rx="2" ry="2" width="7" height="3.5" fill="' + engine + '" opacity="0.7" transform="rotate(12 15.5 35.25)"/>' +
        // Fuselage — long tapered tube, rendered after wings so roots are hidden
        '<path d="M44,22.5 Q47,24 44,25.5 L10,26 Q5,26 4,24 Q5,22 10,22 Z" fill="' + body + '" opacity="0.92"/>' +
        // Fuselage spine highlight (light stripe down the center, gives 3D depth)
        '<path d="M42,23.5 Q44,24 42,24.5 L11,24.5 Q6,24.5 5,24 Q6,23.5 11,23.5 Z" fill="' + body + '" opacity="0.35"/>' +
        // Tail cone (final taper to a point)
        '<path d="M10,22 L2,24 L10,26 Z" fill="' + body + '" opacity="0.88"/>' +
        // Cockpit windows (near nose)
        '<path d="M36,23 Q39,24 36,25 Z" fill="' + glass + '" opacity="0.55"/>' +
      '</g></svg>',
    iconSize: [size, size], iconAnchor: [size/2, size/2]
  });
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

    var entry = markers.get(ac.icao24);
    if (entry) {
      entry.marker.setLatLng(latlng);
      entry.marker.setIcon(airplaneIcon(ac.heading, ac.flagged));
    } else {
      entry = {};
      entry.marker = L.marker(latlng, { icon: airplaneIcon(ac.heading, ac.flagged) }).addTo(map);
      entry.marker.on('click', function() { openDetail(ac); });
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
      return Math.min(100, r.severity_score * 15 + r.anomaly_count * 5) >= minGhostScore;
    });
  }
  currentReports = reports;

  var el = document.getElementById('reports-list');
  if (!el) return;
  if (!filtered.length) { el.innerHTML = '<div class="empty-state">No alerts above threshold.</div>'; return; }

  var existing = {};
  el.querySelectorAll('.report-card').forEach(function(c) { existing[c.dataset.id] = c; });
  var newIds = {}, html = '';

  filtered.forEach(function(r) {
    newIds[r.incident_id] = true;
    if (!existing[r.incident_id]) {
      var gs = Math.min(100, r.severity_score * 15 + r.anomaly_count * 5);
      var gsLabel = gs >= 70 ? 'high' : gs >= 40 ? 'med' : 'low';
      html += '<button class="report-card new" data-id="' + r.incident_id + '">' +
        '<div class="report-card-inner">' +
          '<div class="report-card-header">' +
            '<span class="sev sev-' + r.severity_score + '">' + r.severity_score + '</span>' +
            '<span class="gs gs-' + gsLabel + '">GS ' + gs + '</span>' +
            '<span class="time">' + formatTime(r.time_start) + '</span>' +
          '</div>' +
          '<div class="summary">' + esc(r.summary).substring(0, 200) + '</div>' +
          '<div class="report-expanded">' +
            '<div class="summary-full">' + esc(r.summary) + '</div>' +
            '<div class="action">' + esc(r.recommended_action) + '</div>' +
            '<div class="aircraft">' + r.aircraft_ids.join(', ') + ' &middot; ' + esc(r.region) + ' &middot; ' + r.anomaly_count + ' flags</div>' +
            '<div class="report-actions">' +
              '<button class="btn-fly" data-id="' + r.incident_id + '">Zoom to region</button>' +
              '<button class="btn-export" data-id="' + r.incident_id + '">Export</button>' +
            '</div>' +
          '</div>' +
        '</div></button>';
    }
  });

  if (html) {
    el.insertAdjacentHTML('afterbegin', html);
    setTimeout(function() { el.querySelectorAll('.report-card.new').forEach(function(c) { c.classList.remove('new'); }); }, 400);
  }

  Object.keys(existing).forEach(function(id) { if (!newIds[id] && existing[id].parentNode) existing[id].remove(); });

  // Cap at 50
  var cards = el.querySelectorAll('.report-card');
  for (var i = 50; i < cards.length; i++) cards[i].remove();

  // Bind events
  el.querySelectorAll('.report-card').forEach(function(card) {
    if (card._bound) return; card._bound = true;
    card.addEventListener('click', function() {
      var was = card.classList.contains('expanded');
      el.querySelectorAll('.report-card.expanded').forEach(function(c) { c.classList.remove('expanded'); });
      if (!was) card.classList.add('expanded');
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

function openDetail(ac) {
  selectedAircraft = ac;
  var panel = document.getElementById('detail-panel');
  document.getElementById('detail-title').textContent = (ac.callsign || ac.icao24);
  panel.classList.remove('detail-hidden');

  // Stats
  var alt = ac.altitude != null ? Math.round(ac.altitude) + 'm' : '--';
  var spd = ac.velocity != null ? Math.round(ac.velocity) + ' m/s' : '--';
  var hdg = ac.heading != null ? Math.round(ac.heading) + '°' : '--';
  var flagged = ac.flagged ? 'FLAGGED (' + ac.anomaly_count + ')' : 'CLEAN';

  document.getElementById('detail-stats').innerHTML =
    '<div class="detail-stat"><div class="detail-stat-label">ICAO24</div><div class="detail-stat-value">' + esc(ac.icao24) + '</div></div>' +
    '<div class="detail-stat"><div class="detail-stat-label">Status</div><div class="detail-stat-value" style="color:' + (ac.flagged ? COLOR_RED : 'inherit') + '">' + flagged + '</div></div>' +
    '<div class="detail-stat"><div class="detail-stat-label">Altitude</div><div class="detail-stat-value">' + alt + '</div></div>' +
    '<div class="detail-stat"><div class="detail-stat-label">Speed</div><div class="detail-stat-value">' + spd + '</div></div>' +
    '<div class="detail-stat"><div class="detail-stat-label">Heading</div><div class="detail-stat-value">' + hdg + '</div></div>' +
    '<div class="detail-stat"><div class="detail-stat-label">Region</div><div class="detail-stat-value">' + esc(ac.region) + '</div></div>';

  // Altitude chart
  drawAltChart(ac);

  // Anomaly history
  fetchJSON('/api/anomalies?limit=200').then(function(d) {
    var related = d.anomalies.filter(function(a) { return a.icao24 === ac.icao24; }).slice(-6);
    var html = '<h3>Recent Anomalies</h3>';
    if (!related.length) html += '<div class="detail-anomaly">None detected</div>';
    related.forEach(function(a) {
      html += '<div class="detail-anomaly"><span class="da-type">' + a.flag_type + '</span> ' +
        '<span class="da-value">mahal=' + a.mahalanobis_distance + ' gs=' + (a.ghost_score || 0) + '</span></div>';
    });
    document.getElementById('detail-anomalies').innerHTML = html;
  }).catch(function() {});

  document.getElementById('detail-export').onclick = function() {
    var text = 'AIRCRAFT REPORT\n===============\n' +
      'ICAO24: ' + ac.icao24 + '\nCallsign: ' + (ac.callsign || 'N/A') + '\n' +
      'Position: ' + ac.latitude.toFixed(4) + ', ' + ac.longitude.toFixed(4) + '\n' +
      'Altitude: ' + alt + '\nSpeed: ' + spd + '\nHeading: ' + hdg + '\n' +
      'Region: ' + ac.region + '\nStatus: ' + flagged + '\n';
    var blob = new Blob([text], { type: 'text/plain' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a'); a.href = url; a.download = 'aircraft-' + ac.icao24 + '.txt'; a.click();
  };

  // Re-render trail with selected styling (bright gold, thick)
  var entry = markers.get(ac.icao24);
  if (entry && entry.ac) _renderTrail(entry, entry.ac);

  // Zoom to fit the selected aircraft's full flight path
  if (ac.trail && ac.trail.length >= 2) {
    try {
      var bounds = L.latLngBounds(ac.trail.map(function(p) { return [p.lat, p.lon]; }));
      if (bounds.isValid()) {
        map.fitBounds(bounds, { padding: [50, 50], maxZoom: 12 });
      }
    } catch(e) { /* ignore */ }
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

document.getElementById('detail-close').addEventListener('click', function() {
  var prevSelected = selectedAircraft;
  document.getElementById('detail-panel').classList.add('detail-hidden');
  selectedAircraft = null;
  // Revert the previously selected aircraft's trail to normal styling
  if (prevSelected) {
    var entry = markers.get(prevSelected.icao24);
    if (entry && entry.ac) _renderTrail(entry, entry.ac);
  }
});

// ── Threat bar ───────────────────────────────────────────────────

function updateThreats() {
  fetchJSON('/api/threats').then(function(d) {
    var bar = document.getElementById('threat-bar');
    var html = '';
    Object.keys(d.threats).forEach(function(name) {
      var t = d.threats[name];
      html += '<div class="threat-indicator threat-' + t.level + '" title="' +
        name.replace(/_/g, ' ') + ': ' + t.label + ' (' + t.anomaly_count + ' anomalies, ' + t.aircraft_count + ' aircraft)"></div>';
    });
    bar.innerHTML = html;
  }).catch(function() {});
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
    setStatus('live', 'live');
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
    if (d.errors > 0) setStatus('error', d.errors + ' errors');
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
