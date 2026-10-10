// Shared helpers for the Hospital Maintenance system.
(function () {
  window.hm = window.hm || {};

  function toast(msg, type) {
    var area = document.getElementById('toast-area');
    if (!area) return;
    var el = document.createElement('div');
    el.className = 'toast ' + (type ? 'toast-' + type : 'toast-info');
    el.textContent = msg;
    area.appendChild(el);
    setTimeout(function () { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, 6000);
    setTimeout(function () { el.remove(); }, 6400);
  }
  window.hm.toast = toast;

  function statusLabel(key) {
    var T = window.HM_T || {};
    var map = T.status || {};
    return map[key] || key;
  }
  window.hm.statusLabel = statusLabel;

  function priorityMeta(key) {
    var T = window.HM_T || {};
    var labels = T.prio || {};
    var clsMap = {
      critical: 'bg-soft-red', high: 'bg-soft-orange',
      medium: 'bg-soft-amber', low: 'bg-soft-green'
    };
    return { label: labels[key] || key, cls: clsMap[key] || 'bg-soft-gray' };
  }
  window.hm.priorityMeta = priorityMeta;

  var _audio;
  function playSound() {
    try {
      if (!_audio) {
        var ctx = new (window.AudioContext || window.webkitAudioContext)();
        _audio = ctx;
        var o = ctx.createOscillator();
        var g = ctx.createGain();
        o.connect(g); g.connect(ctx.destination);
        o.frequency.value = 880; g.gain.value = 0.06;
        o.start();
        g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.5);
        o.stop(ctx.currentTime + 0.5);
      }
    } catch (e) {}
  }
  window.hm.playSound = playSound;

  function setLive(id, on) {
    var el = document.getElementById(id);
    if (el) {
      var T = window.HM_T || {};
      el.textContent = on ? (T.connected || 'Connected live') : (T.disconnected || 'No live connection');
      el.style.color = on ? '#16a34a' : '#dc2626';
    }
  }
  window.hm.setLive = setLive;
})();