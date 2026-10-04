/* Small SVG time-series charts with keyboard and pointer inspection. */
window.OrbitCharts = (() => {
  const escape = (value) => String(value).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
  const time = (iso) => new Date(iso).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  const W = 360, H = 160, LEFT = 42, RIGHT = 350, TOP = 12, BOTTOM = 132;

  function render(container, report, series, format, readout) {
    const points = report.points;
    const values = points.flatMap((point) => series.map((line) => point[line.key])).filter((value) => value != null);
    const max = Math.max(1, ...values) * 1.15;
    const x = (index) => LEFT + (index + 0.5) / points.length * (RIGHT - LEFT);
    const y = (value) => BOTTOM - value / max * (BOTTOM - TOP);
    const focusWithin = container.contains(document.activeElement);
    const remembered = Number(container.dataset.inspected ?? points.length - 1);
    let svg = '';
    for (const fraction of [0, 0.5, 1]) {
      const value = max * fraction;
      svg += `<line class="grid-line" x1="${LEFT}" x2="${RIGHT}" y1="${y(value)}" y2="${y(value)}"/>`;
      svg += `<text class="axis-label" x="${LEFT - 7}" y="${y(value) + 3}" text-anchor="end">${escape(format(value))}</text>`;
    }
    for (const line of series) {
      let connected = false;
      let path = '';
      for (let i = 0; i < points.length; i++) {
        const value = points[i][line.key];
        if (value == null) { connected = false; continue; }
        path += `${connected ? 'L' : 'M'}${x(i).toFixed(2)},${y(value).toFixed(2)} `;
        connected = true;
        // Dots keep isolated latency samples visible without bridging data gaps.
        if (line.dots) svg += `<circle cx="${x(i)}" cy="${y(value)}" r="2.8" fill="${line.color}"/>`;
      }
      svg += `<path class="series-line" d="${path}" stroke="${line.color}"/>`;
    }
    const tick = (iso) => new Date(iso).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    svg += `<text class="axis-label" x="${LEFT}" y="154">${escape(tick(report.start_at))}</text>`;
    svg += `<text class="axis-label" x="${RIGHT}" y="154" text-anchor="end">${escape(tick(report.measured_at))}</text>`;
    if (!values.length) svg += `<text class="axis-label" x="${(LEFT + RIGHT) / 2}" y="70" text-anchor="middle">No completed tasks in this window</text>`;
    svg += `<line class="chart-cursor" x1="${x(remembered)}" x2="${x(remembered)}" y1="${TOP}" y2="${BOTTOM}" visibility="hidden"/>`;
    container.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" tabindex="0" role="img" aria-label="${escape(series.map((line) => line.label).join(' and '))} trend. Use left and right arrow keys to inspect intervals."><title>Measured intervals; use the table below for all values.</title>${svg}</svg>`;
    const element = container.querySelector('svg');
    const cursor = container.querySelector('.chart-cursor');
    function inspect(index) {
      const i = Math.max(0, Math.min(points.length - 1, index));
      container.dataset.inspected = i;
      cursor.setAttribute('x1', x(i));
      cursor.setAttribute('x2', x(i));
      cursor.setAttribute('visibility', 'visible');
      const label = time(points[i].at) + ' · ' + series.map((line) => line.label + ' ' + (points[i][line.key] == null ? '—' : format(points[i][line.key]))).join(' · ');
      readout.textContent = label;
      element.setAttribute('aria-label', label + '. Use left and right arrow keys to inspect intervals.');
    }
    element.addEventListener('pointermove', (event) => {
      const rect = element.getBoundingClientRect();
      inspect(Math.floor(((event.clientX - rect.left) / rect.width * W - LEFT) / (RIGHT - LEFT) * points.length));
    });
    element.addEventListener('pointerleave', () => {
      if (document.activeElement !== element) cursor.setAttribute('visibility', 'hidden');
    });
    element.addEventListener('focus', () => inspect(Number(container.dataset.inspected ?? points.length - 1)));
    element.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const current = Number(container.dataset.inspected ?? points.length - 1);
      inspect(event.key === 'Home' ? 0 : event.key === 'End' ? points.length - 1 : current + (event.key === 'ArrowRight' ? 1 : -1));
    });
    if (focusWithin) element.focus({preventScroll: true});
    else if (container.dataset.inspected !== undefined) inspect(remembered);
  }
  return {render};
})();
