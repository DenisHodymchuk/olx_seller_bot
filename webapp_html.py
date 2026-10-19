DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="uk">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <title>OLX Кабінет Оголошень</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {
      --bg: #0f172a;
      --card: #1e293b;
      --border: #334155;
      --text: #f8fafc;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --green: #22c55e;
      --amber: #f59e0b;
      --red: #ef4444;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      padding: 16px;
      padding-bottom: 60px;
    }
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
    }
    .header h1 {
      font-size: 20px;
      font-weight: 700;
    }
    .refresh-btn {
      background: var(--card);
      color: var(--accent);
      border: 1px solid var(--border);
      padding: 8px 14px;
      border-radius: 10px;
      font-weight: 600;
      cursor: pointer;
    }
    .stats-bar {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 8px;
      margin-bottom: 16px;
    }
    .stat-box {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px 8px;
      text-align: center;
      cursor: pointer;
      transition: 0.15s;
    }
    .stat-box.active-filter {
      border-color: var(--accent);
      box-shadow: 0 0 0 1px var(--accent);
    }
    .stat-num {
      font-size: 18px;
      font-weight: 700;
    }
    .stat-label {
      font-size: 11px;
      color: var(--muted);
      margin-top: 2px;
    }
    .cards {
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 12px;
      display: flex;
      gap: 12px;
      align-items: flex-start;
    }
    .thumb {
      width: 76px;
      height: 76px;
      border-radius: 10px;
      object-fit: cover;
      background: #0b1120;
      flex-shrink: 0;
    }
    .card-body {
      flex: 1;
      min-width: 0;
    }
    .badge {
      display: inline-block;
      font-size: 11px;
      font-weight: 600;
      padding: 3px 8px;
      border-radius: 6px;
      margin-bottom: 6px;
    }
    .badge.active { background: rgba(34, 197, 94, 0.18); color: #4ade80; }
    .badge.new, .badge.disabled { background: rgba(56, 189, 248, 0.18); color: #38bdf8; }
    .badge.limited { background: rgba(245, 158, 11, 0.2); color: #fbbf24; }
    .badge.other { background: rgba(148, 163, 184, 0.15); color: #94a3b8; }
    .badge.rejected { background: rgba(239, 68, 68, 0.2); color: #f87171; }
    .title {
      font-size: 14px;
      font-weight: 600;
      line-height: 1.3;
      margin-bottom: 4px;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
    .price {
      font-size: 15px;
      font-weight: 700;
      color: var(--accent);
      margin-bottom: 8px;
    }
    .actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .btn {
      font-size: 12px;
      font-weight: 600;
      padding: 6px 10px;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: #0f172a;
      color: var(--text);
      text-decoration: none;
      cursor: pointer;
    }
    .btn.pay {
      background: rgba(245, 158, 11, 0.2);
      border-color: #f59e0b;
      color: #fbbf24;
    }
    .btn.del {
      color: #f87171;
    }
    .empty {
      text-align: center;
      color: var(--muted);
      padding: 40px 16px;
    }
  </style>
</head>
<body>
  <div class="header">
    <h1>📊 Мої оголошення OLX</h1>
    <button class="refresh-btn" onclick="loadAdverts()">🔄 Оновити</button>
  </div>

  <div class="stats-bar">
    <div class="stat-box active-filter" id="flt-all" onclick="setFilter('all')">
      <div class="stat-num" id="cnt-all">-</div>
      <div class="stat-label">Всі</div>
    </div>
    <div class="stat-box" id="flt-active" onclick="setFilter('active')">
      <div class="stat-num" id="cnt-active" style="color:#4ade80">-</div>
      <div class="stat-label">Активні</div>
    </div>
    <div class="stat-box" id="flt-pending" onclick="setFilter('pending')">
      <div class="stat-num" id="cnt-pending" style="color:#38bdf8">-</div>
      <div class="stat-label">Модерація</div>
    </div>
    <div class="stat-box" id="flt-limited" onclick="setFilter('limited')">
      <div class="stat-num" id="cnt-limited" style="color:#fbbf24">-</div>
      <div class="stat-label">До оплати</div>
    </div>
  </div>

  <div id="cards" class="cards">
    <div class="empty">Завантаження оголошень з OLX...</div>
  </div>

  <script>
    if (window.Telegram && window.Telegram.WebApp) {
      window.Telegram.WebApp.ready();
      window.Telegram.WebApp.expand();
    }

    let allAdverts = [];
    let currentFilter = 'all';

    function setFilter(flt) {
      currentFilter = flt;
      document.querySelectorAll('.stat-box').forEach(el => el.classList.remove('active-filter'));
      document.getElementById('flt-' + flt).classList.add('active-filter');
      renderAdverts();
    }

    function getBadgeInfo(status) {
      if (status === 'active') return { cls: 'active', text: '✅ Активне' };
      if (status === 'new' || status === 'disabled') return { cls: 'new', text: '⏳ На модерації' };
      if (status === 'limited') return { cls: 'limited', text: '💳 Очікує оплати / Ліміт' };
      if (status === 'removed_by_moderator') return { cls: 'rejected', text: '⛔ Відхилено' };
      if (status === 'outdated') return { cls: 'other', text: '📦 Термін вийшов' };
      return { cls: 'other', text: status };
    }

    async function deleteAdvert(id) {
      if (!confirm('Видалити оголошення #' + id + ' з OLX?')) return;
      await fetch('/api/adverts/' + id + '/delete', { method: 'POST' });
      await loadAdverts();
    }

    async function activateAdvert(id) {
      const res = await fetch('/api/adverts/' + id + '/activate', { method: 'POST' });
      const data = await res.json();
      alert(data.message || 'Запит надіслано');
      await loadAdverts();
    }

    function renderAdverts() {
      const container = document.getElementById('cards');
      const filtered = allAdverts.filter(a => {
        if (currentFilter === 'active') return a.status === 'active';
        if (currentFilter === 'pending') return a.status === 'new' || a.status === 'disabled';
        if (currentFilter === 'limited') return a.status === 'limited';
        return a.status !== 'removed_by_user';
      });

      if (!filtered.length) {
        container.innerHTML = '<div class="empty">Немає оголошень у цій категорії</div>';
        return;
      }

      container.innerHTML = filtered.map(a => {
        const badge = getBadgeInfo(a.status);
        const img = (a.images && a.images.length) ? a.images[0].url : '';
        const price = a.price ? a.price.value + ' грн' : '';
        let extraBtns = '';
        if (a.status === 'limited') {
          extraBtns += `<a class="btn pay" href="https://www.olx.ua/d/uk/myaccount/waiting/" target="_blank">💳 Оплатити на OLX</a>`;
          extraBtns += `<button class="btn" onclick="activateAdvert(${a.id})">⚡ З пакета</button>`;
        }
        if (a.status !== 'active') {
          extraBtns += `<button class="btn del" onclick="deleteAdvert(${a.id})">🗑 Видалити</button>`;
        }
        return `
          <div class="card">
            <img class="thumb" src="${img}" alt="" />
            <div class="card-body">
              <span class="badge ${badge.cls}">${badge.text}</span>
              <div class="title">${a.title}</div>
              <div class="price">${price} <span style="font-size:11px;color:#94a3b8;font-weight:400;">#${a.id}</span></div>
              <div class="actions">
                <a class="btn" href="${a.url}" target="_blank">🔗 Відкрити</a>
                ${extraBtns}
              </div>
            </div>
          </div>
        `;
      }).join('');
    }

    async function loadAdverts() {
      try {
        const res = await fetch('/api/adverts');
        const data = await res.json();
        allAdverts = data.adverts || [];
        const visible = allAdverts.filter(a => a.status !== 'removed_by_user');
        document.getElementById('cnt-all').innerText = visible.length;
        document.getElementById('cnt-active').innerText = visible.filter(a => a.status === 'active').length;
        document.getElementById('cnt-pending').innerText = visible.filter(a => a.status === 'new' || a.status === 'disabled').length;
        document.getElementById('cnt-limited').innerText = visible.filter(a => a.status === 'limited').length;
        renderAdverts();
      } catch (e) {
        document.getElementById('cards').innerHTML = '<div class="empty">Помилка завантаження даних</div>';
      }
    }

    loadAdverts();
  </script>
</body>
</html>
"""
