/* The charge curve, as the house meter drew it.
 *
 * Deliberately the same picture as in the energy analyzer's own charge log:
 * same colours, same stacking order (sun at the bottom, so the free part
 * carries the curve and the bought part sits visibly on top), same hatching for
 * the stretches no supply meter saw, same measured wallbox line over the bands.
 * Two programs showing the same charge must not show it two different ways —
 * a reader comparing them would have to work out which one to believe.
 *
 * The payload is passed through from the analyzer untouched; everything here is
 * drawing, no arithmetic on the numbers.
 */
(function () {
  'use strict';

  var SRC = { solar: '#fdd835', battery: '#22c55e', grid: '#ef4444' };
  var cache = {};      // charge id -> payload, so a repaint costs no round trip

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function T(key, fallback) {
    var m = window.WB_I18N || {};
    return m[key] || fallback;
  }

  function fmtMin(sec) {
    var m = Math.round((sec || 0) / 60);
    return m >= 60 ? Math.floor(m / 60) + ' h ' + (m % 60) + ' min' : m + ' min';
  }

  function chip(color, label, value) {
    return '<span style="display:inline-flex;align-items:center;gap:4px">' +
      '<span style="width:9px;height:9px;border-radius:2px;background:' + color + '"></span>' +
      '<span>' + esc(label) + (value ? ' <strong>' + esc(value) + '</strong>' : '') + '</span></span>';
  }

  function legend(parts, style) {
    var chips = parts.map(function (p) { return chip(p[0], p[1], p[2]); });
    return '<div style="display:flex;flex-wrap:wrap;gap:4px 14px;align-items:center;font-size:12px;' +
      (style || '') + '">' + chips.join('') + '</div>';
  }

  function hhmm(tsec) {
    return new Date(tsec * 1000).toLocaleTimeString(undefined,
      { hour: '2-digit', minute: '2-digit' });
  }

  function paint(id, hoverX) {
    var d = cache[id];
    var cv = document.getElementById('wbcv-' + id);
    if (!cv || !d) return;
    var dpr = window.devicePixelRatio || 1;
    var rect = cv.getBoundingClientRect();
    // A canvas in a modal that has not finished opening measures 0x0; painting
    // then throws and takes the rest of the render with it.
    if (!(rect.width > 0 && rect.height > 0)) return;
    cv.width = rect.width * dpr;
    cv.height = rect.height * dpr;
    var ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    var W = rect.width, H = rect.height, PL = 40, PR = 8, PT = 10, PB = 18;
    var iw = W - PL - PR, ih = H - PT - PB;
    ctx.clearRect(0, 0, W, H);

    var ts = d.ts, n = ts.length;
    var peak = Math.max.apply(null, d.load_w) || 1;
    var yMax = peak * 1.08;
    function x(i) { return PL + (ts[i] - ts[0]) / Math.max(1, ts[n - 1] - ts[0]) * iw; }
    function y(v) { return PT + ih - (v / yMax) * ih; }

    var dark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
    var grid = dark ? 'rgba(255,255,255,.18)' : 'rgba(0,0,0,.14)';
    var muted = dark ? '#9aa4b2' : '#6c757d';

    ctx.strokeStyle = grid;
    ctx.fillStyle = muted;
    ctx.font = '10px system-ui,sans-serif';
    ctx.textAlign = 'right';
    ctx.lineWidth = 1;
    for (var k = 0; k <= 3; k++) {
      var v = yMax * k / 3, yy = Math.round(y(v)) + 0.5;
      ctx.globalAlpha = 0.6;
      ctx.beginPath(); ctx.moveTo(PL, yy); ctx.lineTo(W - PR, yy); ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillText((v / 1000).toFixed(1) + ' kW', PL - 5, yy + 3);
    }

    var bands = [[d.solar_w, SRC.solar], [d.battery_w, SRC.battery], [d.grid_w, SRC.grid]];
    var base = new Float64Array(n);
    bands.forEach(function (b) {
      var arr = b[0] || [];
      ctx.fillStyle = b[1]; ctx.globalAlpha = 0.85;
      ctx.beginPath();
      for (var i = 0; i < n; i++) ctx.lineTo(x(i), y(base[i] + (arr[i] || 0)));
      for (var j = n - 1; j >= 0; j--) ctx.lineTo(x(j), y(base[j]));
      ctx.closePath(); ctx.fill(); ctx.globalAlpha = 1;
      for (var m = 0; m < n; m++) base[m] += (arr[m] || 0);
    });

    ctx.strokeStyle = muted; ctx.lineWidth = 1.3; ctx.globalAlpha = 0.85;
    ctx.beginPath();
    for (var q = 0; q < n; q++) ctx.lineTo(x(q), y(d.load_w[q]));
    ctx.stroke(); ctx.globalAlpha = 1;

    // Stretches no supply meter saw: shaded, never coloured. Painting them in a
    // source colour would claim a measurement that does not exist.
    if (d.measured) {
      ctx.fillStyle = muted; ctx.globalAlpha = 0.12;
      for (var r = 0; r < n; r++) {
        if (!d.measured[r]) {
          var x0 = x(r), x1 = (r + 1 < n ? x(r + 1) : x0 + 1);
          ctx.fillRect(x0, PT, Math.max(1, x1 - x0), ih);
        }
      }
      ctx.globalAlpha = 1;
    }

    ctx.fillStyle = muted; ctx.font = '10px system-ui,sans-serif';
    ctx.textAlign = 'left'; ctx.fillText(hhmm(ts[0]), PL, H - 5);
    ctx.textAlign = 'right'; ctx.fillText(hhmm(ts[n - 1]), W - PR, H - 5);

    var tip = document.getElementById('wbct-' + id);
    if (hoverX != null) {
      var best = 1e9, bi = 0;
      for (var c2 = 0; c2 < n; c2++) {
        var dd = Math.abs(x(c2) - hoverX);
        if (dd < best) { best = dd; bi = c2; }
      }
      ctx.strokeStyle = muted; ctx.globalAlpha = 0.6; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x(bi), PT); ctx.lineTo(x(bi), PT + ih); ctx.stroke();
      ctx.globalAlpha = 1;
      if (tip) {
        var kw = function (val) { return ((val || 0) / 1000).toFixed(2) + ' kW'; };
        tip.innerHTML = hhmm(ts[bi]) + ' · ' + kw(d.load_w[bi]) + ' ' +
          legend([[SRC.solar, T('wb.solar', 'Solar'), kw(d.solar_w[bi])],
                  [SRC.battery, T('wb.battery', 'Batterie'), kw(d.battery_w[bi])],
                  [SRC.grid, T('wb.grid', 'Netz'), kw(d.grid_w[bi])]],
                 'display:inline-flex;vertical-align:middle;margin-left:8px');
      }
    }
  }

  function render(id, payload) {
    var box = document.getElementById('wbCurveBody');
    if (!box) return;
    var d = payload.curve || {};
    var r = payload.reading || {};
    cache[id] = d;

    var head = '';
    if (r.energy_kwh != null) {
      var bits = [Number(r.energy_kwh).toFixed(2) + ' kWh'];
      if (r.cost_eur != null) bits.push(Number(r.cost_eur).toFixed(2) + ' €');
      if (r.measured && r.solar_share != null) {
        bits.push(Math.round(r.solar_share * 100) + ' % ' +
                  T('wb.self_supplied', 'selbst erzeugt'));
      }
      head = '<div class="text-muted small mb-2">' + esc(bits.join(' · ')) + '</div>';
    }

    if (!d.available || !d.ts || d.ts.length < 2) {
      // Not an error: the charge is real and its kWh are real, only the source
      // measurement does not reach it. Saying "no curve" beats an empty chart.
      box.innerHTML = head + '<div class="alert alert-secondary small mb-0">' +
        esc(T('wb.curve_none', 'Für diesen Ladevorgang liegt keine Quellen-Messung vor.')) +
        '</div>';
      return;
    }
    var sec = d.seconds || {};
    box.innerHTML = head +
      '<div class="text-muted small mb-1">' +
        esc(T('wb.curve_flowed', 'Wie lange welche Quelle geflossen ist')) +
        ' · ' + esc(fmtMin(sec.total)) + '</div>' +
      '<canvas id="wbcv-' + esc(id) + '" style="width:100%;height:190px;display:block"></canvas>' +
      '<div id="wbct-' + esc(id) + '" class="small text-muted" style="min-height:18px"></div>' +
      legend([[SRC.solar, T('wb.solar', 'Solar'), fmtMin(sec.solar)],
              [SRC.battery, T('wb.battery', 'Batterie'), fmtMin(sec.battery)],
              [SRC.grid, T('wb.grid', 'Netz'), fmtMin(sec.grid)]], 'margin-top:6px');
    paint(id);
  }

  function open(chargeId) {
    var el = document.getElementById('wbCurveModal');
    if (!el || !window.bootstrap) return;
    var modal = window.bootstrap.Modal.getOrCreateInstance(el);
    var box = document.getElementById('wbCurveBody');
    box.innerHTML = '<div class="text-muted small py-3">' +
      esc(T('wb.loading', 'Ladekurve wird geholt…')) + '</div>';
    modal.show();
    fetch('/api/wallbox/charge/' + chargeId + '/curve')
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) {
          box.innerHTML = '<div class="alert alert-warning small mb-0">' +
            esc(j.error || T('wb.curve_failed', 'Die Ladekurve konnte nicht geholt werden.')) +
            '</div>';
          return;
        }
        render(chargeId, j);
      })
      .catch(function (e) {
        box.innerHTML = '<div class="alert alert-danger small mb-0">' + esc(e) + '</div>';
      });
  }

  // One delegated listener rather than one per row: the history table is
  // re-rendered on every filter change and per-row listeners would pile up.
  document.addEventListener('click', function (e) {
    var b = e.target && e.target.closest ? e.target.closest('[data-wb-curve]') : null;
    if (!b) return;
    e.preventDefault();
    e.stopPropagation();          // the row itself opens the edit modal
    open(b.getAttribute('data-wb-curve'));
  });

  document.addEventListener('mousemove', function (e) {
    var cv = e.target;
    if (!cv || !cv.id || cv.id.indexOf('wbcv-') !== 0) return;
    var rect = cv.getBoundingClientRect();
    paint(cv.id.slice(5), e.clientX - rect.left);
  });

  window.addEventListener('resize', function () {
    Object.keys(cache).forEach(function (id) { paint(id); });
  });

  window.WallboxCurve = { open: open };
})();
