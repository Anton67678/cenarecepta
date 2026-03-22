/**
 * pyra-link.js — модуль привязки аккаунта Telegram к сайту PYRA.
 *
 * Логика:
 *  1. Пользователь пишет /link боту → получает 6-значный код.
 *  2. Вводит код на сайте → JS читает /link_codes/{code} из Firebase.
 *  3. Если код валидный (не просрочен, не использован) →
 *     - сохраняет tg_key и telegram_id в localStorage
 *     - помечает код как использованный (used: true)
 *  4. Все функции сохранения данных используют tg_key из localStorage
 *     вместо ручного ввода username.
 *
 * Использование:
 *  - Подключить скрипт после Firebase SDK:
 *    <script src="pyra-link.js"></script>
 *  - Вставить блок привязки в HTML:
 *    <div id="pyra-link-block"></div>
 *    <script>PYRALink.renderBlock('pyra-link-block');</script>
 *  - Получить текущий data_key:
 *    const key = PYRALink.getDataKey(); // "tg_123456789" или null
 */

window.PYRALink = (function() {

  const LS_KEY_TG_KEY  = 'pyra_tg_key';
  const LS_KEY_TG_ID   = 'pyra_tg_id';
  const LS_KEY_NAME    = 'pyra_tg_name';

  // ── Чтение из localStorage ────────────────────────────────────────────────

  function getDataKey() {
    return localStorage.getItem(LS_KEY_TG_KEY) || null;
  }

  function getTelegramId() {
    return localStorage.getItem(LS_KEY_TG_ID) || null;
  }

  function isLinked() {
    return !!getDataKey();
  }

  function clearLink() {
    localStorage.removeItem(LS_KEY_TG_KEY);
    localStorage.removeItem(LS_KEY_TG_ID);
    localStorage.removeItem(LS_KEY_NAME);
  }

  // ── Проверка кода через Firebase ─────────────────────────────────────────

  /**
   * Проверяет код и, если он валидный, сохраняет привязку.
   * Возвращает Promise<{ ok: boolean, message: string }>
   */
  async function verifyCode(code, db, ref, get, update) {
    if (!code || code.length !== 6) {
      return { ok: false, message: 'Введи 6-значный код из бота.' };
    }

    const upperCode = code.trim().toUpperCase();
    const codeRef   = ref(db, `link_codes/${upperCode}`);

    let snap;
    try {
      snap = await get(codeRef);
    } catch (e) {
      return { ok: false, message: 'Ошибка соединения с сервером.' };
    }

    if (!snap.exists()) {
      return { ok: false, message: '❌ Код не найден. Запроси новый через /link.' };
    }

    const data = snap.val();

    // Проверка срока действия
    const expires = new Date(data.expires);
    if (new Date() > expires) {
      return { ok: false, message: '⏱ Код истёк. Запроси новый через /link.' };
    }

    // Проверка что не использован
    if (data.used) {
      return { ok: false, message: '✅ Этот код уже использован. Запроси новый через /link.' };
    }

    // Помечаем как использованный
    try {
      await update(codeRef, { used: true });
    } catch (e) {
      // Некритично — продолжаем
    }

    // Сохраняем привязку
    localStorage.setItem(LS_KEY_TG_KEY, data.tg_key);
    localStorage.setItem(LS_KEY_TG_ID,  String(data.telegram_id));

    return { ok: true, message: '✅ Аккаунт привязан! Теперь можно сохранять рецептуры.' };
  }

  // ── Рендер блока привязки ─────────────────────────────────────────────────

  /**
   * Вставляет готовый HTML-блок привязки в элемент с заданным id.
   * Вызывать после загрузки DOM и инициализации Firebase.
   *
   * @param {string}   containerId  — id контейнера
   * @param {object}   firebaseDb   — объект database из Firebase SDK
   * @param {function} fbRef        — ref из Firebase SDK
   * @param {function} fbGet        — get из Firebase SDK
   * @param {function} fbUpdate     — update из Firebase SDK
   * @param {function} onLinked     — callback(tg_key) после успешной привязки
   */
  function renderBlock(containerId, firebaseDb, fbRef, fbGet, fbUpdate, onLinked) {
    const container = document.getElementById(containerId);
    if (!container) return;

    function _render() {
      const linked = isLinked();
      const tgKey  = getDataKey();

      if (linked) {
        container.innerHTML = `
          <div class="link-status link-ok">
            <span>🔗 Аккаунт привязан</span>
            <button class="link-unlink-btn" onclick="PYRALink._unlink('${containerId}', arguments[0])">Отвязать</button>
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
                     style="text-transform:uppercase; letter-spacing:0.15em;">
              <button id="pyra-link-verify-btn" onclick="PYRALink._doVerify('${containerId}')">
                Проверить
              </button>
            </div>
            <div id="pyra-link-msg" class="link-msg"></div>
          </div>`;
      }
    }

    // Сохраняем ссылки на Firebase-функции для использования в _doVerify
    window._pyraLinkFirebase = { db: firebaseDb, ref: fbRef, get: fbGet, update: fbUpdate };
    window._pyraLinkCallback = onLinked || function() {};

    _render();
  }

  // ── Внутренние обработчики (вызываются из inline onclick) ─────────────────

  async function _doVerify(containerId) {
    const input = document.getElementById('pyra-link-code-input');
    const msgEl = document.getElementById('pyra-link-msg');
    const btn   = document.getElementById('pyra-link-verify-btn');

    if (!input) return;
    const code = input.value.trim().toUpperCase();

    if (btn) { btn.disabled = true; btn.textContent = 'Проверяю...'; }
    if (msgEl) msgEl.textContent = '';

    const fb = window._pyraLinkFirebase || window._pyraFirebase;
    if (!fb) {
      if (msgEl) msgEl.textContent = '❌ Firebase не инициализирован.';
      if (btn) { btn.disabled = false; btn.textContent = 'Проверить'; }
      return;
    }

    const result = await verifyCode(code, fb.db, fb.ref, fb.get, fb.update);

    if (msgEl) {
      msgEl.textContent = result.message;
      msgEl.className   = 'link-msg ' + (result.ok ? 'link-msg-ok' : 'link-msg-err');
    }

    if (result.ok) {
      const tgKey = getDataKey();
      if (window._pyraLinkCallback) window._pyraLinkCallback(tgKey);
      // Перерендеривае блок через 1.2с
      setTimeout(() => renderBlock(
        containerId,
        fb.db, fb.ref, fb.get, fb.update,
        window._pyraLinkCallback
      ), 1200);
    } else {
      if (btn) { btn.disabled = false; btn.textContent = 'Проверить'; }
    }
  }

  function _unlink(containerId) {
    clearLink();
    const fb = window._pyraLinkFirebase;
    renderBlock(containerId, fb.db, fb.ref, fb.get, fb.update, window._pyraLinkCallback);
  }

  // ── CSS ───────────────────────────────────────────────────────────────────

  function injectStyles() {
    if (document.getElementById('pyra-link-styles')) return;
    const style = document.createElement('style');
    style.id = 'pyra-link-styles';
    style.textContent = `
      .link-block { margin: 12px 0; }
      .link-instruction { font-size: 13px; color: #555; margin-bottom: 8px; }
      .link-instruction code { background: #f0f0f0; padding: 1px 5px; border-radius: 4px; font-size: 13px; }
      .link-instruction a { color: #2563eb; }
      .link-input-row { display: flex; gap: 8px; align-items: center; }
      .link-input-row input {
        width: 110px; padding: 8px 10px; border: 1.5px solid #d1d5db;
        border-radius: 8px; font-size: 16px; font-weight: 600;
      }
      .link-input-row input:focus { outline: none; border-color: #2563eb; }
      .link-input-row button, #pyra-link-verify-btn {
        padding: 8px 16px; background: #2563eb; color: #fff;
        border: none; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 600;
      }
      .link-input-row button:disabled { background: #93c5fd; cursor: not-allowed; }
      .link-msg { margin-top: 6px; font-size: 13px; }
      .link-msg-ok  { color: #16a34a; }
      .link-msg-err { color: #dc2626; }
      .link-status { display: flex; align-items: center; gap: 10px; font-size: 13px; padding: 8px 12px;
                     background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; }
      .link-status.link-ok { color: #15803d; }
      .link-unlink-btn { background: none; border: none; color: #6b7280;
                         cursor: pointer; font-size: 12px; text-decoration: underline; }
    `;
    document.head.appendChild(style);
  }

  // Инжектируем стили сразу при загрузке модуля
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', injectStyles);
  } else {
    injectStyles();
  }

  // ── Public API ────────────────────────────────────────────────────────────
  return {
    getDataKey,
    getTelegramId,
    isLinked,
    clearLink,
    verifyCode,
    renderBlock,
    _doVerify,
    _unlink,
  };

})();
