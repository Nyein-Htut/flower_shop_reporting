/* Monthly report charts. Reads window.MC_DATA (set in monthly.html).
   Chart instances keep the same ids in window.mcCharts so the PDF export works unchanged. */
document.addEventListener('DOMContentLoaded', function () {
  const D = window.MC_DATA;
  if (!D || typeof Chart === 'undefined') return;

  const P = { iris: '#8b78e6', soft: '#c9bdf8', rose: '#f08fb8', mint: '#6fcfa8', peach: '#f6b98f',
              mute: '#8a84a3', grid: 'rgba(139,120,230,.10)' };
  const fmt = (v) => Number(v || 0).toLocaleString() + ' MMK';
  const short = (v) => v >= 1e6 ? +(v / 1e6).toFixed(1) + 'M' : v >= 1e3 ? +(v / 1e3).toFixed(0) + 'k' : v;
  const sum = (a) => a.reduce((x, y) => x + y, 0);
  const isYear = D.viewMode === 'year';

  Chart.defaults.font.family = "'Inter','Noto Sans Myanmar','Noto Sans SC',system-ui,sans-serif";
  Chart.defaults.color = P.mute;
  Chart.defaults.animation.duration = 900;
  Chart.defaults.animation.easing = 'easeOutQuart';

  const tooltip = {
    backgroundColor: 'rgba(39,35,58,.94)', titleColor: '#fff', bodyColor: '#e9e4ff',
    padding: 12, cornerRadius: 12, displayColors: false, boxPadding: 4,
    titleFont: { weight: '600' }, bodyFont: { size: 13 }, footerColor: '#bfb4f5', footerFont: { weight: '500', size: 11 }
  };
  const axes = {
    y: { beginAtZero: true, border: { display: false }, grid: { color: P.grid },
         ticks: { callback: short, padding: 8, maxTicksLimit: 6 } },
    x: { border: { display: false }, grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true,
         maxTicksLimit: isYear ? 12 : 15 } }
  };
  const base = { responsive: true, maintainAspectRatio: false, interaction: { intersect: false, mode: 'index' } };

  // vertical guide line that follows the hovered point
  const crosshair = { id: 'crosshair', afterDatasetsDraw(c) {
    const a = c.tooltip && c.tooltip.getActiveElements();
    if (!a || !a.length) return;
    const x = a[0].element.x, { top, bottom } = c.chartArea, g = c.ctx;
    g.save(); g.beginPath(); g.setLineDash([4, 4]); g.strokeStyle = 'rgba(139,120,230,.45)';
    g.moveTo(x, top); g.lineTo(x, bottom); g.stroke(); g.restore();
  }};
  // total shown in the middle of the doughnut
  const centerText = { id: 'centerText', afterDraw(c) {
    const { left, right, top, bottom } = c.chartArea, g = c.ctx, cx = (left + right) / 2, cy = (top + bottom) / 2;
    g.save(); g.textAlign = 'center'; g.textBaseline = 'middle';
    g.fillStyle = '#27233a'; g.font = "700 24px 'Inter',sans-serif"; g.fillText(sum(D.customerSplit), cx, cy - 6);
    g.fillStyle = P.mute; g.font = "500 11px 'Inter',sans-serif"; g.fillText('customers', cx, cy + 14);
    g.restore();
  }};
  const vGrad = (chart, a, b) => {
    const area = chart.chartArea; if (!area) return a;
    const g = chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
    g.addColorStop(0, a); g.addColorStop(1, b); return g;
  };

  window.mcCharts = window.mcCharts || {};

  /* ---- revenue trend ---- */
  const trendTotal = sum(D.trendData), peak = Math.max(...D.trendData, 0);
  const peakIdx = peak > 0 ? D.trendData.indexOf(peak) : -1;
  window.mcCharts.revenueTrendChart = new Chart(document.getElementById('revenueTrendChart'), {
    type: 'line',
    plugins: [crosshair],
    data: { labels: D.trendLabels, datasets: [{
      label: isYear ? 'Monthly Revenue' : 'Daily Revenue', data: D.trendData, fill: true, tension: 0.38, borderWidth: 3,
      borderColor: (c) => {
        const a = c.chart.chartArea; if (!a) return P.iris;
        const g = c.chart.ctx.createLinearGradient(a.left, 0, a.right, 0);
        g.addColorStop(0, P.iris); g.addColorStop(1, P.rose); return g;
      },
      backgroundColor: (c) => vGrad(c.chart, 'rgba(139,120,230,.30)', 'rgba(240,143,184,0)'),
      pointRadius: (c) => c.dataIndex === peakIdx ? 6 : (isYear ? 4 : 0),
      pointHoverRadius: 8, pointBackgroundColor: '#fff', pointBorderWidth: 3,
      pointBorderColor: (c) => c.dataIndex === peakIdx ? P.rose : P.iris
    }]},
    options: { ...base, plugins: { legend: { display: false }, tooltip: { ...tooltip, callbacks: {
      title: (i) => isYear ? i[0].label : D.period + '-' + String(i[0].label).padStart(2, '0'),
      label: (c) => fmt(c.parsed.y),
      footer: (i) => trendTotal ? (i[0].parsed.y / trendTotal * 100).toFixed(1) + '% of period' + (i[0].dataIndex === peakIdx ? ' · best' : '') : ''
    }}}, scales: axes }
  });

  /* ---- new vs returning ---- */
  const custTotal = sum(D.customerSplit);
  window.mcCharts.customerChart = new Chart(document.getElementById('customerChart'), {
    type: 'doughnut',
    plugins: [centerText],
    data: { labels: ['New Customers', 'Returning Customers'], datasets: [{
      data: D.customerSplit, backgroundColor: [P.peach, P.mint], borderWidth: 0, spacing: 4, borderRadius: 10, hoverOffset: 8 }] },
    options: { responsive: true, maintainAspectRatio: false, cutout: '72%',
      plugins: { legend: { position: 'bottom', labels: { usePointStyle: true, pointStyle: 'circle', boxWidth: 8, padding: 14, font: { size: 11 } } },
        tooltip: { ...tooltip, callbacks: { label: (c) => c.label + ': ' + c.parsed + (custTotal ? ' (' + Math.round(c.parsed / custTotal * 100) + '%)' : '') } } } }
  });

  /* ---- weekday pattern ---- */
  const wMax = Math.max(...D.weekdayData, 0), wPeak = wMax > 0 ? D.weekdayData.indexOf(wMax) : -1, wTotal = sum(D.weekdayData);
  window.mcCharts.weekdayChart = new Chart(document.getElementById('weekdayChart'), {
    type: 'bar',
    data: { labels: D.weekdayLabels, datasets: [{
      label: 'Revenue', data: D.weekdayData, borderRadius: 10, borderSkipped: false, barPercentage: 0.66, categoryPercentage: 0.8,
      backgroundColor: (c) => c.dataIndex === wPeak ? vGrad(c.chart, '#f6a8c9', '#fbd3e3') : vGrad(c.chart, '#b7a6f5', '#e6e0fc'),
      hoverBackgroundColor: (c) => vGrad(c.chart, '#8b78e6', '#bfb0f7')
    }]},
    options: { ...base, plugins: { legend: { display: false }, tooltip: { ...tooltip, callbacks: {
      label: (c) => fmt(c.parsed.y),
      footer: (i) => wTotal ? (i[0].parsed.y / wTotal * 100).toFixed(1) + '% of period' + (i[0].dataIndex === wPeak ? ' · busiest day' : '') : ''
    }}}, scales: { ...axes, x: { ...axes.x, ticks: { font: { weight: '500' } } } } }
  });
});
