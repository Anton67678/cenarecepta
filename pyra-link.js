/**
 * pyra-link.js — модуль привязки Telegram-аккаунта к сайту PYRA.
 * Использует Firebase REST API с анонимной аутентификацией.
 */
(function() {
  'use strict';

  const API_KEY  = "AIzaSyCEWWRUKpJ2tJdsUkTfMTKTH7Lfmc9dZs0";
  const DB_URL   = "https://cenarecepta-calc-default-rtdb.europe-west1.firebasedatabase.app";
  const LS_TG_KEY = 'pyra_tg_key';
  const LS_TG_ID  = 'pyra_tg_id';
  const LS_TOKEN  = 'pyra_fb_token';
  const LS_TOKEN_EXP = 'pyra_fb_token_exp';

  // ── Анонимная аутентификация ───────────────────────────────────────────────

  async function getAuthToken() {
    // Используем кэшированный токен если он ещё действует (> 5 мин до истечения)
    const cached = localStorage.getItem(LS_TOKEN);
    const exp    = parseInt(localStorage.getItem(LS_TOKEN_EXP) || '0');
    if (cached && Date.now() < exp - 5 * 60 * 1000) {
      return cached;
    }

    const res = await fetch(
      `https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=${API_KEY}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ returnSecureToken: true })
      }
    );
    if (!res.ok) throw new Error(`Auth failed: HTTP ${res.status}`);
    const data = await res.json();
    if (!data.idToken) throw new Error('Auth failed: no token');

    // Кэшируем на 50 минут (Firebase токены живут 60 мин)
    localStorage.setItem(LS_TOKEN, data.idToken);
    localStorage.setItem(LS_TOKEN_EXP, String(Date.now() + 50 * 60 * 1000));
    return data.idToken;
  }

  // ── Firebase REST API с токеном ────────────────────────────────────────────

  async function dbGet(path) {
    const token = await getAuthToken();
    const res = await fetch(`${DB_URL}/${path}.json?auth=${token}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  async function dbPatch(path, data) {
    const token = await getAuthToken();
    const res = await fetch(`${DB_URL}/${path}.json?auth=${token}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data)
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  // ── localStorage helpers ───────────────────────────────────────────────────

  function getDataKey()    { return localStorage.getItem(LS_TG_KEY) || null; }
  function getTelegramId() { return localStorage.getItem(LS_TG_ID)  || null; }
  function isLinked()      { return !!getDataKey(); }

  function clearLink() {
    localStorage.removeItem(LS_TG_KEY);
    localStorage.removeItem(LS_TG_ID);
  }

  // ── Проверка кода ──────────────────────────────────────────────────────────

  async function verifyCode(code) {
    const upper = (code || '').trim().toUpperCase();
    if (upper.length !== 6) {
      return { ok: false, message: 'Введи 6-значный код из бота.' };
    }

    let data;
    try {
      data = await dbGet(`link_codes/${upper}`);
    } catch(e) {
      return { ok: false, message: '❌ Ошибка соединения: ' + e.message };
    }

    if (!data) {
      return { ok: false, message: '❌ Код не найден. Запроси новый через /link.' };
    }

    if (new Date() > new Date(data.expires)) {
      return { ok: false, message: '⏱ Код истёк. Запроси новый через /link.' };
    }

    if (data.used) {
      return { ok: false, message: '✅ Код уже использован. Запроси новый через /link.' };
    }

    // Помечаем как использованный
    try { await dbPatch(`link_codes/${upper}`, { used: true }); } catch(e) {}

    // Читаем data_key из профиля — там может быть username (для старых пользователей)
    let dataKey = data.tg_key;
    try {
      const profile = await dbGet(`users/${data.tg_key}`);
      if (profile && profile.data_key) dataKey = profile.data_key;
    } catch(e) {}

    // Сохраняем привязку
    localStorage.setItem(LS_TG_KEY, dataKey);
    localStorage.setItem(LS_TG_ID,  String(data.telegram_id));

    return { ok: true, message: '✅ Аккаунт привязан! Теперь можно сохранять рецептуры.' };
  }

  // ── Рендер блока ───────────────────────────────────────────────────────────

  function renderBlock(containerId, _a, _b, _c, _d, onLinked) {
    const container = document.getElementById(containerId);
    if (!container) return;

    window._pyraLinkCallback = onLinked || function() {};

    if (isLinked()) {
      container.innerHTML = `
        <div class="link-status link-ok">
          <span>🔗 Аккаунт привязан</span>
          <button class="link-unlink-btn" onclick="PYRALink._unlink('${containerId}')">Отвязать</button>
        </div>`;
    } else {
      container.innerHTML = `
        <div class="link-block">
          <div class="link-instruction">
            Напиши боту <a href="https://t.me/cenarecepta_bot" target="_blank">@cenarecepta_bot</a>
            команду <code>/link</code> и введи полученный код:
          </div>
          <div class="link-input-row">
            <input type="text" id="pyra-link-code-input"
                   maxlength="6" placeholder="ABC123"
                   autocomplete="off" autocorrect="off"
                   style="text-transform:uppercase;letter-spacing:0.15em;"
                   onkeydown="if(event.key==='Enter') PYRALink._doVerify('${containerId}')">
            <button id="pyra-link-verify-btn" onclick="PYRALink._doVerify('${containerId}')">
              Проверить
            </button>
          </div>
          <div id="pyra-link-msg" class="link-msg"></div>
        </div>`;
    }
  }

  // ── Обработчики ────────────────────────────────────────────────────────────

  async function _doVerify(containerId) {
    const input = document.getElementById('pyra-link-code-input');
    const msgEl = document.getElementById('pyra-link-msg');
    const btn   = document.getElementById('pyra-link-verify-btn');

    if (!input) return;

    if (btn) { btn.disabled = true; btn.textContent = 'Проверяю...'; }
    if (msgEl) { msgEl.textContent = ''; msgEl.className = 'link-msg'; }

    const result = await verifyCode(input.value);

    if (msgEl) {
      msgEl.textContent = result.message;
      msgEl.className   = 'link-msg ' + (result.ok ? 'link-msg-ok' : 'link-msg-err');
    }

    if (result.ok) {
      if (window._pyraLinkCallback) window._pyraLinkCallback(getDataKey());
      setTimeout(() => renderBlock(containerId, null, null, null, null, window._pyraLinkCallback), 1000);
    } else {
      if (btn) { btn.disabled = false; btn.textContent = 'Проверить'; }
    }
  }

  function _unlink(containerId) {
    clearLink();
    renderBlock(containerId, null, null, null, null, window._pyraLinkCallback);
  }

  // ── CSS ────────────────────────────────────────────────────────────────────

  function injectStyles() {
    if (document.getElementById('pyra-link-styles')) return;
    const s = document.createElement('style');
    s.id = 'pyra-link-styles';
    s.textContent = `
      .link-block { margin: 12px 0; }
      .link-instruction { font-size: 13px; color: #888; margin-bottom: 8px; }
      .link-instruction code { background: rgba(255,255,255,.1); padding: 1px 6px; border-radius: 4px; font-size: 13px; }
      .link-instruction a { color: #60a5fa; }
      .link-input-row { display: flex; gap: 8px; align-items: center; }
      .link-input-row input {
        width: 110px; padding: 8px 10px; border: 1.5px solid #444;
        border-radius: 8px; font-size: 16px; font-weight: 700;
        background: rgba(255,255,255,.07); color: #fff;
      }
      .link-input-row input:focus { outline: none; border-color: #60a5fa; }
      #pyra-link-verify-btn {
        padding: 8px 18px; background: #2563eb; color: #fff;
        border: none; border-radius: 8px; cursor: pointer;
        font-size: 14px; font-weight: 600;
      }
      #pyra-link-verify-btn:hover { background: #1d4ed8; }
      #pyra-link-verify-btn:disabled { background: #1e3a6e; cursor: not-allowed; }
      .link-msg { margin-top: 7px; font-size: 13px; min-height: 18px; }
      .link-msg-ok  { color: #4ade80; }
      .link-msg-err { color: #f87171; }
      .link-status { display: flex; align-items: center; gap: 10px; font-size: 13px;
                     padding: 8px 12px; background: rgba(74,222,128,.1);
                     border: 1px solid rgba(74,222,128,.3); border-radius: 8px; color: #4ade80; }
      .link-unlink-btn { background: none; border: none; color: #6b7280;
                         cursor: pointer; font-size: 12px; text-decoration: underline; }
    `;
    document.head.appendChild(s);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', injectStyles);
  } else {
    injectStyles();
  }

  // ── Public API ─────────────────────────────────────────────────────────────
  window.PYRALink = { getDataKey, getTelegramId, isLinked, clearLink, verifyCode, renderBlock, _doVerify, _unlink };

})();
