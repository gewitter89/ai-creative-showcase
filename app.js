'use strict';

let catalog = { items: [], telegram_contact: 'https://t.me/NaumCreativeStudioBot' };
let activeCategory = 'all';

function telegramContact(value) {
  return typeof value === 'string' && /^https:\/\/t\.me\/[A-Za-z][A-Za-z0-9_]{4,31}\/?$/.test(value)
    ? value.replace(/\/$/, '')
    : '';
}

function previewPath(item) {
  if (!['newyear', 'photoshop', '3d'].includes(item.category)) return '';
  if (typeof item.id !== 'string' || !/^[a-z0-9-]{1,64}$/.test(item.id)) return '';
  const expected = `assets/${item.category}/${item.id}/preview.webp`;
  return item.image === expected ? item.image : '';
}

function productStatus(item) {
  return ['черновик', 'проверка качества', 'готов к продаже'].includes(item.status)
    ? item.status
    : 'черновик';
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));
}

function buildDeepLink(contact, itemId) {
  if (!contact || typeof itemId !== 'string' || !/^[a-z0-9-]{1,64}$/.test(itemId)) {
    return '#';
  }
  return `${contact}?start=${encodeURIComponent(`buy_${itemId}`)}`;
}

function buildDemoLink(contact, deeplink) {
  const param = typeof deeplink === 'string' && /^[a-z0-9_-]{1,64}$/.test(deeplink) ? deeplink : 'demo_ny';
  return contact ? `${contact}?start=${encodeURIComponent(param)}` : '#';
}

function renderDemoBanner(contact) {
  const banner = document.getElementById('demo-banner');
  if (!banner) return;
  const demo = catalog.demo_offer;
  if (!demo || !contact) {
    banner.hidden = true;
    banner.innerHTML = '';
    return;
  }
  const href = buildDemoLink(contact, demo.deeplink);
  banner.hidden = false;
  banner.innerHTML = `
    <div class="demo-info">
      <span class="demo-pill">Бесплатно · 0 Stars</span>
      <div>
        <strong>${escapeHtml(demo.title || 'Бесплатная демо-открытка')}</strong>
        <p class="demo-desc">${escapeHtml(demo.description || '')}</p>
      </div>
    </div>
    <a class="btn primary" href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">Забрать демо в боте</a>
  `;
}

function badgeMeta(status, packageReady) {
  if (status === 'готов к продаже' && packageReady) {
    return { label: 'Готов к продаже', cls: 'badge-ready' };
  }
  if (status === 'проверка качества') {
    return {
      label: packageReady ? 'ZIP собран · Проверка качества' : 'Проверка качества',
      cls: 'badge-qa',
    };
  }
  return { label: 'В разработке', cls: 'badge-draft' };
}

function render() {
  const queryEl = document.getElementById('q');
  const q = (queryEl ? queryEl.value : '').trim().toLowerCase();
  const contact = telegramContact(catalog.telegram_contact);
  renderDemoBanner(contact);
  const topLink = document.getElementById('tg-top');
  if (topLink) {
    topLink.hidden = !contact;
    if (contact) topLink.href = contact;
    else topLink.removeAttribute('href');
  }

  const list = catalog.items.filter((item) => {
    const catMatch = activeCategory === 'all' || item.category === activeCategory;
    const haystack = `${item.title || ''} ${item.description || ''} ${item.package_contents || ''} ${item.id || ''}`.toLowerCase();
    return catMatch && (!q || haystack.includes(q));
  });

  const grid = document.getElementById('grid');
  if (!list.length) {
    grid.innerHTML = '<p class="sub">По вашему запросу ничего не найдено.</p>';
    return;
  }

  grid.innerHTML = list.map((item) => {
    const status = productStatus(item);
    const preview = previewPath(item);
    const packageReady = item.package_ready === true;
    const canBuy = status === 'готов к продаже' && packageReady && Boolean(preview) && Boolean(contact);
    const badge = badgeMeta(status, packageReady);
    const catNote = item.category === '3d' ? ' · Графический референс (не STL)' : '';
    const orderHref = canBuy ? buildDeepLink(contact, item.id) : '#';
    const priceUah = Number(item.price_uah || 0);
    const priceXtr = Number(item.price_xtr || priceUah || 0);
    const contents = item.package_contents || item.format || 'Состав комплекта уточняется';

    const actionHtml = canBuy
      ? `<a class="btn primary" href="${escapeHtml(orderHref)}" target="_blank" rel="noopener noreferrer">Купить в боте</a>`
      : `<button class="btn disabled" type="button" disabled>${status === 'проверка качества' ? 'Проверка качества' : 'В разработке'}</button>`;

    return `
      <article class="card" data-id="${escapeHtml(item.id)}">
        <div class="thumb">
          ${preview ? `<img src="${escapeHtml(preview)}" alt="${escapeHtml(item.title)}" loading="lazy">` : `<span>Превью в разработке · ${escapeHtml(item.format)}</span>`}
          <span class="badge ${badge.cls}">${escapeHtml(badge.label)}</span>
        </div>
        <div class="body">
          <div class="meta-row">
            <span class="cat">${escapeHtml(item.category_label)}${escapeHtml(catNote)}</span>
            <span class="item-id">${escapeHtml(item.id)}</span>
          </div>
          <h2 class="title">${escapeHtml(item.title)}</h2>
          <p class="desc">${escapeHtml(item.description)}</p>
          <div class="package-box">
            <span class="package-label">Комплектация:</span>
            <span>${escapeHtml(contents)}</span>
            <div class="package-format">Формат: ${escapeHtml(item.format)}</div>
          </div>
          <div class="foot">
            <div class="price-col">
              <span class="price-xtr">★ ${priceXtr} Stars</span>
              <span class="price-uah">${priceUah} грн (ориентир)</span>
            </div>
            ${actionHtml}
          </div>
        </div>
      </article>
    `;
  }).join('');
}

document.querySelectorAll('[data-cat]').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('[data-cat]').forEach((el) => el.classList.remove('active'));
    btn.classList.add('active');
    activeCategory = btn.dataset.cat || 'all';
    render();
  });
});

const searchInput = document.getElementById('q');
if (searchInput) {
  searchInput.addEventListener('input', render);
}

fetch('./catalog.json')
  .then((resp) => {
    if (!resp.ok) throw new Error('catalog unavailable');
    return resp.json();
  })
  .then((data) => {
    if (!data || !Array.isArray(data.items)) throw new Error('invalid catalog');
    catalog = data;
    render();
  })
  .catch(() => {
    catalog = { items: [], telegram_contact: '' };
    render();
    document.getElementById('grid').textContent = 'Каталог временно недоступен.';
  });
