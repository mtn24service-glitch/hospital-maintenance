// Live dashboard: SSE stream + chart rendering.
(function () {
  var liveFeed = document.getElementById('live-feed');
  if (!liveFeed) return;

  // Recency label (locale follows the active UI language)
  function when(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return '';
    var loc = (window.HM_LANG === 'en') ? 'en-GB' : 'ar-SA';
    return d.toLocaleString(loc, { dateStyle: 'medium', timeStyle: 'short' });
  }

  function T(key, fallback) {
    var dict = window.HM_T || {};
    return dict[key] || fallback || '';
  }

  function rowHtml(cd) {
    var pm = window.hm.priorityMeta(cd.priority);
    var warm = (cd.priority === 'critical' || cd.priority === 'high');
    return (
      '<div class="card" style="padding:12px 14px;border' + (warm ? '-right:4px solid #dc2626' : '') + '">' +
      '<div class="row-between">' +
      '<div><span class="ticket" style="direction:ltr">' + cd.ticket_no + '</span> ' +
      '<span class="pill ' + pm.cls + '">' + pm.label + '</span> ' +
      '<span class="chip">' + (cd.department || '') + '</span></div>' +
      '<span class="small muted">' + when(cd.created_at) + '</span></div>' +
      '<div class="small mt" style="color:var(--ink)"><b>' + (cd.title || cd.fault_type || '') + '</b> — ' +
      (cd.reporter_name || '') + '</div>' +
      '<a href="/complaints/' + cd.id + '" class="btn btn-outline btn-xs mt">' + T('open_complaint', 'Open complaint') + ' 📄</a>' +
      '</div><br>'
    );
  }

  // Initial feed of the already-listed complaints (server-rendered rows shown above);
  // we show only *live* arrivals here. Add a placeholder explanation.
  liveFeed.innerHTML = '<div class="small muted">' + T('waiting', 'Waiting for new complaints...') + '</div>';

  function onNew(data) {
    window.hm.playSound();
    window.hm.toast('🚨 ' + T('new_complaint', 'New complaint') + ': ' + data.ticket_no + ' — ' + (data.department || ''),
                    data.priority === 'critical' ? 'critical' : (data.priority === 'high' ? 'high' : 'info'));
    var el = document.createElement('div');
    el.id = 'live-' + data.id;
    el.style.animation = 'slideIn .3s ease';
    el.innerHTML = rowHtml(data);
    liveFeed.insertBefore(el, liveFeed.firstChild);
    if (liveFeed.children.length > 6) liveFeed.lastChild.remove();
  }

  function onStatus(data) {
    window.hm.toast(T('updated', 'Complaint updated') + ' #' + (data.id || '') + ' → ' + window.hm.statusLabel(data.status), 'info');
    var row = document.getElementById('row-' + data.id);
    if (row) {
      row.style.background = data.status === 'closed' ? 'rgba(100,116,139,.1)' : '';
      row.style.transition = 'background .4s';
    }
  }

  var es;
  function connect() {
    es = new EventSource('/api/stream');
    es.addEventListener('new_complaint', function (ev) {
      try { onNew(JSON.parse(ev.data)); } catch (e) {}
    });
    es.addEventListener('status_change', function (ev) {
      try { onStatus(JSON.parse(ev.data)); } catch (e) {}
    });
    es.onopen = function () { window.hm.setLive('live-status', true); };
    es.onerror = function () {
      window.hm.setLive('live-status', false);
      es.close();
      setTimeout(connect, 4000); // auto-reconnect
    };
  }
  connect();

  // Charts
  function drawCharts() {
    if (!window.chartData || typeof Chart === 'undefined') return;
    var dep = document.getElementById('chart-dept');
    var typ = document.getElementById('chart-type');
    var tim = document.getElementById('chart-time');
    var chartJs = '/static/js/chart.umd.min.js';
    if (!dep || !typ || !tim) return;

    var base = {
      type: 'bar',
      options: { indexAxis: 'y', responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { font: { family: 'Cairo' } } } } }
    };
    new Chart(dep, Object.assign({}, base, {
      data: { labels: window.chartData.departments.map(function (r) { return r.name; }),
              datasets: [{ label: T('chart_label', 'Complaints'), data: window.chartData.departments.map(function (r) { return r.count; }),
                backgroundColor: '#0b6b42' }] }
    }));
    new Chart(typ, Object.assign({}, base, {
      data: { labels: window.chartData.types.map(function (r) { return r.name; }),
              datasets: [{ label: T('chart_label', 'Complaints'), data: window.chartData.types.map(function (r) { return r.count; }),
                backgroundColor: '#0e7490' }] }
    }));
    new Chart(tim, { type: 'line', data: {
      labels: window.chartData.overTime.map(function (r) { return r.day; }),
      datasets: [{ label: T('chart_label', 'Complaints'), data: window.chartData.overTime.map(function (r) { return r.count; }),
        borderColor: '#0b6b42', backgroundColor: 'rgba(11,107,66,.15)', fill: true, tension: .3 }]
    }, options: { responsive: true, maintainAspectRatio: false } });
  }

  if (typeof Chart !== 'undefined') drawCharts();
  else {
    var s = document.createElement('script');
    s.src = '/static/js/chart.umd.min.js';
    s.onload = drawCharts;
    document.head.appendChild(s);
  }
})();