// Ghost Track — Tutorial (simple, reliable overlay)

(function() {
  var KEY = 'ghost-track-tutorial-v4';
  var STEPS = [
    { title: 'Live air traffic', body: 'Each icon is a real aircraft. Amber = clean, red = flagged. They update every 5 seconds from a live receiver network. Dashed lines show recent flight paths, dotted lines project where they are heading.' },
    { title: 'Stats at a glance', body: 'Three numbers: aircraft in view, total anomalies detected since startup, and AI-triage alerts generated. These tick up in real time as events unfold.' },
    { title: 'AI triage', body: 'When detectors flag an anomaly, an AI agent writes a plain-English summary. Each alert has a severity rating (1 to 5), a Ghost Score (0 to 100), and a recommended human action. Click any alert to expand and read the full text.' },
    { title: 'Filtering', body: 'Use the Ghost Score slider to hide low-significance alerts. Higher scores mean stronger statistical evidence. The filter combines Mahalanobis distance, CUSUM drift accumulation, cluster density, and anomaly persistence.' },
    { title: 'Ready', body: 'The system is live. Watch for red planes and expanding alerts. Click the ? button anytime to replay this tour. Switch regions with the dropdown to see different contested airspace.' }
  ];

  var step = 0;
  var overlay, titleEl, bodyEl, dotsEl, prevBtn, nextBtn, doneBtn, skipBtn, dots;

  function setup() {
    overlay = document.getElementById('tutorial-overlay');
    if (!overlay) { setTimeout(setup, 200); return; }

    titleEl = document.getElementById('tutorial-title');
    bodyEl = document.getElementById('tutorial-body');
    dotsEl = document.getElementById('tutorial-step-dots');
    prevBtn = document.getElementById('tutorial-prev');
    nextBtn = document.getElementById('tutorial-next');
    doneBtn = document.getElementById('tutorial-done');
    skipBtn = document.getElementById('tutorial-skip');

    // Build dot indicators
    var h = '';
    for (var i = 0; i < STEPS.length; i++) h += '<span class="tutorial-dot"></span>';
    dotsEl.innerHTML = h;
    dots = dotsEl.querySelectorAll('.tutorial-dot');

    // Bind buttons
    nextBtn.onclick = next;
    prevBtn.onclick = prev;
    doneBtn.onclick = close;
    skipBtn.onclick = close;
    overlay.querySelector('#tutorial-backdrop').onclick = close;

    document.addEventListener('keydown', function(e) {
      if (overlay.classList.contains('tutorial-hidden')) return;
      if (e.key === 'Escape') close();
      if (e.key === 'ArrowRight') next();
      if (e.key === 'ArrowLeft') prev();
    });

    // Tutorial only starts via the ? button, never auto-pops
  }

  function start() {
    step = 0;
    overlay.classList.remove('tutorial-hidden');
    document.body.style.overflow = 'hidden';
    showStep(0);
  }

  function showStep(i) {
    step = i;
    var s = STEPS[i];
    titleEl.textContent = (i + 1) + '. ' + s.title;
    bodyEl.textContent = s.body;

    for (var j = 0; j < dots.length; j++) {
      dots[j].className = 'tutorial-dot';
      if (j === i) dots[j].classList.add('active');
      else if (j < i) dots[j].classList.add('done');
    }

    prevBtn.classList.toggle('tutorial-hidden', i === 0);
    nextBtn.classList.toggle('tutorial-hidden', i === STEPS.length - 1);
    doneBtn.classList.toggle('tutorial-hidden', i !== STEPS.length - 1);

    positionCard();
  }

  function positionCard() {
    var card = document.getElementById('tutorial-card');
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    card.style.left = Math.round((vw - 380) / 2) + 'px';
    card.style.top = Math.round((vh - 260) / 2) + 'px';
  }

  function next() { if (step < STEPS.length - 1) showStep(step + 1); else close(); }
  function prev() { if (step > 0) showStep(step - 1); }

  function close() {
    overlay.classList.add('tutorial-hidden');
    document.body.style.overflow = '';
    localStorage.setItem(KEY, 'true');
  }

  // Run setup when DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', setup);
  } else {
    setup();
  }

  // Expose for replay button
  window.ghostTrackTour = { start: start, close: close };
})();
