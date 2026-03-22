import os
import asyncio
import json
import random
import string
import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
import firebase_admin
from firebase_admin import db
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN    = os.getenv('BOT_TOKEN')
FIREBASE_URL = os.getenv('FIREBASE_URL')

firebase_key = json.loads(os.getenv('FIREBASE_KEY_JSON'))
cred = firebase_admin.credentials.Certificate(firebase_key)
firebase_admin.initialize_app(cred, {'databaseURL': FIREBASE_URL})

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

SITE_URL = 'https://calc.pyra.com.ru'


# ══════════════════════════════════════════════════════
# USER IDENTITY
# ══════════════════════════════════════════════════════

def _get_user_key(message: types.Message) -> str:
    """
    Возвращает стабильный ключ пользователя для Firebase.

    Стратегия (backward-compatible):
      1. Числовой telegram_id — основной ключ (неизменяемый).
         Хранится как строка "tg_{id}" чтобы не конфликтовать
         со старыми записями по username.
      2. Username — вторичный, только для связи со старыми данными.

    Старые данные (recipes/{username}/...) НЕ ломаются — бот
    продолжает их читать через _resolve_data_key().
    """
    return f"tg_{message.from_user.id}"


def _get_username(message: types.Message) -> str | None:
    """Возвращает username без символа @, или None."""
    return message.from_user.username or None


def _resolve_data_key(message: types.Message) -> str:
    """
    Определяет ключ для чтения данных (recipes, stock и т.д.).

    Логика миграции:
      - Новые пользователи (зарегистрированные после обновления)
        хранят данные под tg_{id}.
      - Старые пользователи хранят данные под username.
      - Читаем profile из /users/tg_{id} — там поле data_key
        указывает, где хранятся данные пользователя.
      - Если profile ещё не создан — fallback на username (старая логика).
    """
    tg_key = _get_user_key(message)
    try:
        profile = db.reference(f'users/{tg_key}').get()
        if profile and profile.get('data_key'):
            return profile['data_key']
    except Exception:
        pass
    # Fallback для старых пользователей без профиля
    return _get_username(message) or tg_key


def _upsert_user(message: types.Message) -> None:
    """
    Создаёт или обновляет профиль пользователя.

    Структура /users/tg_{id}/:
      telegram_id  — числовой ID (неизменяемый, primary key)
      username     — текущий @username (может меняться)
      name         — имя из Telegram
      plan         — тарифный план
      data_key     — ключ для данных (recipes, stock, etc.)
      registered   — дата первой регистрации
      last_seen    — дата последнего обращения
      schema       — версия схемы профиля

    data_key логика:
      - Новый пользователь без username → data_key = "tg_{id}"
      - Новый пользователь с username → data_key = username
        (чтобы веб-калькуляторы, которые пишут по username, работали)
      - Старый пользователь (уже есть данные по username) → data_key = username
    """
    tg_key   = _get_user_key(message)
    tg_id    = message.from_user.id
    username = _get_username(message)
    name     = message.from_user.first_name or ''
    now      = datetime.datetime.utcnow().isoformat()

    ref = db.reference(f'users/{tg_key}')

    try:
        existing = ref.get()
    except Exception:
        existing = None

    if existing:
        # Пользователь уже есть — обновляем только изменяемые поля
        updates = {
            'username':  username or existing.get('username', ''),
            'name':      name,
            'last_seen': now,
        }
        # Если username сменился — обновляем, но data_key НЕ трогаем
        if username and existing.get('username') != username:
            updates['username'] = username
            # Логируем смену username для отладки
            updates['username_prev'] = existing.get('username', '')
        ref.update(updates)
    else:
        # Новый пользователь — создаём полный профиль
        # data_key: если есть username — используем его (веб-сайт пишет по username)
        # иначе — числовой ключ
        data_key = tg_key  # всегда числовой ключ — сайт теперь привязывается через /link

        ref.set({
            'telegram_id': tg_id,          # числовой, неизменяемый
            'username':    username or '',  # может меняться
            'name':        name,
            'plan':        'free',
            'data_key':    data_key,        # ключ для recipes/stock/sales (всегда tg_{id})
            'registered':  now,
            'last_seen':   now,
            'schema':      '2.0',
        })

        # Если есть username — создаём обратный индекс username → tg_key
        # Это позволяет найти tg_key зная только username (для веб-калькуляторов)
        if username:
            try:
                db.reference(f'username_index/{username}').set({
                    'tg_key':    tg_key,
                    'telegram_id': tg_id,
                })
            except Exception:
                pass


# ══════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════

def _flatten_recipes(raw: dict) -> list:
    """
    Возвращает плоский отсортированный список рецептов (dict).
    Поддерживает два уровня вложенности Firebase:
      - recipes/{key}/{push_id}          → прямой рецепт
      - recipes/{key}/{x}/{push_id}      → вложенный
    """
    result = []
    for key, val in raw.items():
        if not isinstance(val, dict):
            continue
        if 'name' in val:
            result.append(val)
        else:
            for subkey, subval in val.items():
                if isinstance(subval, dict) and 'name' in subval:
                    result.append(subval)

    def sort_key(r):
        is_new = 'yield' in r or 'pricing' in r or 'stock_write_off' in r
        return (1 if is_new else 0, r.get('name', ''))

    result.sort(key=sort_key)
    return result


def _is_new_format(r: dict) -> bool:
    return 'yield' in r or 'pricing' in r or 'stock_write_off' in r


def _safe_list(val) -> list:
    """Гарантирует список — защита от None и неожиданных типов."""
    if isinstance(val, list):
        return val
    return []


def _safe_dict(val) -> dict:
    """Гарантирует словарь — защита от None и неожиданных типов."""
    if isinstance(val, dict):
        return val
    return {}


def _safe_float(val, default=0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


async def _send_long(message: types.Message, text: str) -> None:
    """Отправляет длинный текст, разбивая на части по 4000 символов."""
    if len(text) > 4000:
        for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
            await message.answer(chunk, parse_mode='HTML')
    else:
        await message.answer(text, parse_mode='HTML')


# ══════════════════════════════════════════════════════
# ФОРМАТИРОВАНИЕ РЕЦЕПТОВ
# ══════════════════════════════════════════════════════

def format_recipe(recipe: dict) -> str:
    if _is_new_format(recipe):
        return format_recipe_new(recipe)
    return format_recipe_old(recipe)


def format_recipe_old(recipe: dict) -> str:
    """Старый формат — кондитерский калькулятор v1"""
    name        = recipe.get('name', 'Без названия')
    yield_count = max(1, int(_safe_float(recipe.get('yield_count', 1)) or 1))
    cost        = _safe_float(recipe.get('cost_per_unit'))
    price       = _safe_float(recipe.get('price_per_unit'))
    markup      = _safe_float(recipe.get('markup_pct'))

    text  = f'📋 <b>{name}</b>\n'
    text += f'Выход: {yield_count} шт\n'
    text += '─' * 22 + '\n'

    ingredients = _safe_list(recipe.get('ingredients'))
    if ingredients:
        text += '\n🧁 <b>Ингредиенты:</b>\n'
        ing_total = 0
        for ing in ingredients:
            if not isinstance(ing, dict):
                continue
            n         = ing.get('name', '')
            price_pkg = _safe_float(ing.get('price'))
            pkg       = _safe_float(ing.get('pkg')) or 1
            amount    = _safe_float(ing.get('recipe')) / yield_count
            cost_ing  = (price_pkg / pkg) * amount
            ing_total += cost_ing
            if n:
                text += f'  • {n}: {amount:.1f}г — {cost_ing:.2f} ₽/шт\n'
        text += f'  <i>Итого: {ing_total:.2f} ₽/шт</i>\n'

    for section, label, icon in [
        ('consumables', 'Расходники', '📦'),
        ('decor',       'Декор',      '✨'),
    ]:
        items = _safe_list(recipe.get(section))
        if items:
            text += f'\n{icon} <b>{label}:</b>\n'
            total = 0
            for c in items:
                if not isinstance(c, dict):
                    continue
                n      = c.get('name', '')
                p      = _safe_float(c.get('price'))
                pkg    = _safe_float(c.get('pkg')) or 1
                per    = _safe_float(c.get('per')) / yield_count
                cost_c = (p / pkg) * per
                total += cost_c
                if n:
                    text += f'  • {n}: {per:.2f} шт — {cost_c:.2f} ₽/шт\n'
            text += f'  <i>Итого: {total:.2f} ₽/шт</i>\n'

    markup_x = 1 + markup / 100 if markup else (price / cost if cost else 0)
    text += '\n' + '─' * 22 + '\n'
    text += f'💰 <b>Себестоимость: {cost:.2f} ₽/шт</b>\n'
    text += f'🏷 <b>Цена продажи (×{markup_x:.2f}): {price:.2f} ₽/шт</b>\n'
    return text


def format_recipe_new(recipe: dict) -> str:
    """Новый формат — bakery-calc.html / catering-calc.html"""
    name   = recipe.get('name', 'Без названия')
    rtype  = recipe.get('type', 'bakery')
    group  = recipe.get('group') or recipe.get('product_group') or ''
    method = recipe.get('cooking_method') or recipe.get('method') or ''

    yld    = _safe_dict(recipe.get('yield'))
    batch  = _safe_float(yld.get('batch_weight_g'))
    units  = _safe_float(yld.get('units_count') or yld.get('portions_count')) or 1
    unit_w = _safe_float(yld.get('unit_weight_g') or yld.get('portion_weight_g'))

    unit_label = 'шт' if rtype == 'bakery' else 'пор'

    text  = f'📋 <b>{name}</b>\n'
    if group:
        text += f'Группа: {group}\n'
    if method:
        text += f'Метод: {method}\n'
    text += f'Выход: {int(units)} {unit_label}'
    if unit_w:
        text += f' по {unit_w:.0f} г'
    if batch:
        text += f' (замес {batch:.0f} г)'
    text += '\n'
    text += '─' * 22 + '\n'

    # ── Ингредиенты ──
    ingredients = _safe_list(recipe.get('ingredients'))
    if ingredients:
        text += '\n🧁 <b>Ингредиенты (на замес):</b>\n'
        for ing in ingredients:
            if not isinstance(ing, dict):
                continue
            n       = ing.get('name', '')
            brutto  = _safe_float(ing.get('brutto_g') or ing.get('brutto'))
            netto   = _safe_float(ing.get('netto_g')  or ing.get('netto')) or brutto
            waste   = _safe_float(ing.get('waste_g'))
            price_k = _safe_float(ing.get('price_per_kg'))
            cost_r  = (netto / 1000) * price_k
            if not n:
                continue
            waste_str = f' (отх. {waste:.0f}г)' if waste > 0.5 else ''
            text += (
                f'  • {n}: {brutto:.0f}г → {netto:.0f}г{waste_str}'
                f' — {cost_r:.2f} ₽\n'
            )

    # ── Расходники (bakery формат) ──
    consumables = _safe_list(recipe.get('consumables'))
    if consumables:
        text += '\n📦 <b>Расходники:</b>\n'
        for c in consumables:
            if not isinstance(c, dict):
                continue
            n   = c.get('name', '')
            qty = _safe_float(c.get('qty'))
            s   = _safe_float(c.get('sum'))
            if n:
                text += f'  • {n}: {qty:.0f} шт — {s:.2f} ₽\n'

    # ── Ценообразование ──
    pricing = _safe_dict(recipe.get('pricing'))
    cost_u  = _safe_float(pricing.get('cost_per_unit'))
    price_u = _safe_float(pricing.get('price_per_unit'))
    markup  = _safe_float(pricing.get('markup_pct'))
    vat     = _safe_float(pricing.get('vat_pct'))

    text += '\n' + '─' * 22 + '\n'
    text += f'💰 <b>Себестоимость: {cost_u:.2f} ₽/{unit_label}</b>\n'
    text += f'🏷 <b>Цена (наценка {markup:.0f}%, НДС {vat:.0f}%): {price_u:.2f} ₽/{unit_label}</b>\n'

    # ── КБЖУ ──
    nutrition = _safe_dict(recipe.get('nutrition'))
    per_unit  = _safe_dict(nutrition.get('per_unit'))
    cal = _safe_float(per_unit.get('cal'))
    if cal > 0:
        prot = _safe_float(per_unit.get('prot'))
        fat  = _safe_float(per_unit.get('fat'))
        carb = _safe_float(per_unit.get('carb'))
        text += (
            f'\n🥗 КБЖУ/{unit_label}: {cal:.0f} ккал · '
            f'Б{prot:.1f} Ж{fat:.1f} У{carb:.1f}\n'
        )

    # ── Отходы ──
    wo_raw = recipe.get('stock_write_off')

    if isinstance(wo_raw, dict):
        wo_items = _safe_list(wo_raw.get('per_portion'))
        waste_items = [
            {'name': w.get('ingredient', ''),
             'waste': _safe_float(w.get('waste_g_per_unit'))}
            for w in wo_items
            if isinstance(w, dict) and _safe_float(w.get('waste_g_per_unit')) > 0.5
        ]
    elif isinstance(wo_raw, list):
        waste_items = [
            {'name': w.get('ingredient', ''),
             'waste': _safe_float(w.get('waste_g_per_unit')
                                  or w.get('waste_g_per_batch'))}
            for w in wo_raw
            if isinstance(w, dict)
            and _safe_float(w.get('waste_g_per_unit')
                            or w.get('waste_g_per_batch')) > 0.5
        ]
    else:
        waste_items = []

    if waste_items:
        label_wo = 'порция' if rtype == 'catering' else 'замес'
        text += f'\n♻️ <b>Отходы/{label_wo}:</b>\n'
        for w in waste_items[:6]:
            text += f'  • {w["name"]}: {w["waste"]:.1f} г\n'

    return text


# ══════════════════════════════════════════════════════
# /link — привязка сайта к telegram_id
# ══════════════════════════════════════════════════════

def _generate_link_code() -> str:
    """Генерирует 6-символьный буквенно-цифровой код (upper)."""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))


@dp.message(Command('link'))
async def cmd_link(message: types.Message):
    """Генерирует одноразовый код для привязки сайта к этому аккаунту."""
    _upsert_user(message)
    tg_key = _get_user_key(message)
    tg_id  = message.from_user.id
    code   = _generate_link_code()
    now    = datetime.datetime.utcnow()
    expires = (now + datetime.timedelta(minutes=60)).isoformat() + 'Z'

    try:
        db.reference(f'link_codes/{code}').set({
            'tg_key':     tg_key,
            'telegram_id': tg_id,
            'expires':    expires,
            'used':       False,
        })
    except Exception as e:
        await message.answer(
            '❌ Ошибка генерации кода. Попробуй ещё раз.',
            parse_mode='HTML'
        )
        return

    await message.answer(
        f'🔗 <b>Код привязки сайта:</b>\n\n'
        f'<code>{code}</code>\n\n'
        f'Введи этот код на сайте в поле «Код привязки» и нажми «Проверить».\n'
        f'Код действует <b>60 минут</b>.\n\n'
        f'📌 <a href="{SITE_URL}">calc.pyra.com.ru</a>',
        parse_mode='HTML',
        disable_web_page_preview=True
    )


# ══════════════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════════════

@dp.message(Command('start'))
async def cmd_start(message: types.Message):
    # Создаём/обновляем профиль с числовым telegram_id
    _upsert_user(message)

    name = message.from_user.first_name or 'друг'

    await message.answer(
        f'👋 Привет, <b>{name}</b>!\n\n'
        f'Я бот <b>PYRA</b> — food-tech платформа.\n\n'
        f'📌 <b>Сайт:</b> {SITE_URL}\n\n'
        f'<b>Команды:</b>\n'
        f'/recipes — все рецептуры\n'
        f'/recipe 1 — техкарта №1\n'
        f'/stock — остатки склада\n'
        f'/lowstock — критические позиции\n'
        f'/sales — последние продажи\n'
        f'/link — привязать сайт к аккаунту\n'
        f'/help — помощь',
        parse_mode='HTML'
    )


# ══════════════════════════════════════════════════════
# /recipes
# ══════════════════════════════════════════════════════

@dp.message(Command('recipes'))
async def cmd_recipes(message: types.Message):
    # Обновляем last_seen при каждом обращении
    _upsert_user(message)

    data_key = _resolve_data_key(message)

    try:
        raw = db.reference(f'recipes/{data_key}').get()
    except Exception as e:
        await message.answer(f'❌ Ошибка Firebase: {e}')
        return

    if not raw:
        await message.answer(
            f'📋 Рецептур пока нет.\n\n'
            f'Зайди на {SITE_URL}, посчитай и сохрани рецепт.\n'
            f'<i>В поле «Telegram username» укажи: '
            f'@{_get_username(message) or "свой_username"}</i>',
            parse_mode='HTML'
        )
        return

    try:
        all_recipes = _flatten_recipes(raw)
    except Exception as e:
        await message.answer(f'❌ Ошибка обработки рецептов: {e}')
        return

    if not all_recipes:
        await message.answer(f'📋 Рецептур пока нет. {SITE_URL}')
        return

    old_list = []
    new_list = []
    for idx, r in enumerate(all_recipes, start=1):
        if _is_new_format(r):
            new_list.append((idx, r))
        else:
            old_list.append((idx, r))

    text = f'📚 <b>Твои рецептуры ({len(all_recipes)}):</b>\n\n'

    if old_list:
        text += '🍰 <b>Кондитерские (v1):</b>\n'
        for i, r in old_list:
            price = _safe_float(r.get('price_per_unit'))
            text += f'  {i}. {r.get("name", "—")} — {price:.2f} ₽/шт\n'

    if new_list:
        if old_list:
            text += '\n'
        text += '🎂 <b>Новый формат (Pro):</b>\n'
        for i, r in new_list:
            pricing = _safe_dict(r.get('pricing'))
            price   = _safe_float(pricing.get('price_per_unit'))
            group   = r.get('group') or r.get('product_group') or ''
            group_s = f' ({group})' if group else ''
            text += f'  {i}. {r.get("name", "—")}{group_s} — {price:.2f} ₽\n'

    text += '\n<i>/recipe N — открыть полную техкарту</i>'

    await _send_long(message, text)


# ══════════════════════════════════════════════════════
# /recipe N
# ══════════════════════════════════════════════════════

@dp.message(Command('recipe'))
async def cmd_recipe(message: types.Message):
    _upsert_user(message)

    args = message.text.split()
    if len(args) < 2:
        await message.answer('Напиши номер: /recipe 1')
        return

    try:
        num = int(args[1]) - 1
    except ValueError:
        await message.answer('Напиши число: /recipe 1')
        return

    data_key = _resolve_data_key(message)

    try:
        raw = db.reference(f'recipes/{data_key}').get()
    except Exception as e:
        await message.answer(f'❌ Ошибка Firebase: {e}')
        return

    if not raw:
        await message.answer(f'Рецептур пока нет. {SITE_URL}')
        return

    try:
        all_recipes = _flatten_recipes(raw)
    except Exception as e:
        await message.answer(f'❌ Ошибка обработки: {e}')
        return

    if num < 0 or num >= len(all_recipes):
        await message.answer(
            f'Рецептура #{num + 1} не найдена.\n'
            f'У тебя {len(all_recipes)} рецептур — напиши /recipes чтобы увидеть список.'
        )
        return

    try:
        text = format_recipe(all_recipes[num])
    except Exception as e:
        await message.answer(f'❌ Ошибка форматирования: {e}')
        return

    await _send_long(message, text)


# ══════════════════════════════════════════════════════
# /stock
# ══════════════════════════════════════════════════════

@dp.message(Command('stock'))
async def cmd_stock(message: types.Message):
    _upsert_user(message)

    data_key = _resolve_data_key(message)

    try:
        ingredients = db.reference(f'ingredients/{data_key}').get() or {}
        stock_data  = db.reference(f'stock/{data_key}/main').get() or {}
    except Exception as e:
        await message.answer(f'❌ Ошибка Firebase: {e}')
        return

    if not ingredients:
        await message.answer(
            f'📦 Склад пуст.\n\n'
            f'Добавь ингредиенты на {SITE_URL}/stock.html'
        )
        return

    by_cat: dict = {}
    for ing_id, ing in ingredients.items():
        if not isinstance(ing, dict):
            continue
        cat = ing.get('category', 'Прочее')
        if cat not in by_cat:
            by_cat[cat] = []
        qty = _safe_float(_safe_dict(stock_data.get(ing_id)).get('quantity_g'))
        th  = _safe_dict(ing.get('thresholds'))
        by_cat[cat].append({
            'name':     ing.get('name', '—'),
            'qty':      qty,
            'min':      _safe_float(th.get('min_stock_g')),
            'reorder':  _safe_float(th.get('reorder_point_g')),
            'price_kg': _safe_float(ing.get('price_per_kg')),
        })

    total_val = sum(
        _safe_float(_safe_dict(stock_data.get(iid)).get('quantity_g')) / 1000
        * _safe_float(ing.get('price_per_kg'))
        for iid, ing in ingredients.items()
        if isinstance(ing, dict)
    )

    text = '📦 <b>Склад — текущие остатки</b>\n\n'
    for cat, items in sorted(by_cat.items()):
        text += f'<b>{cat}:</b>\n'
        for it in sorted(items, key=lambda x: x['name']):
            qty_kg  = it['qty'] / 1000
            is_crit = it['min'] > 0 and it['qty'] <= it['min']
            is_low  = it['reorder'] > 0 and it['qty'] <= it['reorder']
            dot     = '🔴' if is_crit else ('🟡' if is_low else '🟢')
            text += f'  {dot} {it["name"]}: {qty_kg:.3f} кг'
            if it['price_kg']:
                text += f' ({qty_kg * it["price_kg"]:.0f} ₽)'
            text += '\n'
        text += '\n'

    text += '─' * 20 + '\n'
    text += f'💵 Стоимость склада: <b>{total_val:.0f} ₽</b>\n'
    text += f'\n<i>Управление: {SITE_URL}/stock.html</i>'

    await _send_long(message, text)


# ══════════════════════════════════════════════════════
# /lowstock
# ══════════════════════════════════════════════════════

@dp.message(Command('lowstock'))
async def cmd_lowstock(message: types.Message):
    _upsert_user(message)

    data_key = _resolve_data_key(message)

    try:
        ingredients = db.reference(f'ingredients/{data_key}').get() or {}
        stock_data  = db.reference(f'stock/{data_key}/main').get() or {}
    except Exception as e:
        await message.answer(f'❌ Ошибка Firebase: {e}')
        return

    critical, low = [], []

    for ing_id, ing in ingredients.items():
        if not isinstance(ing, dict):
            continue
        qty      = _safe_float(_safe_dict(stock_data.get(ing_id)).get('quantity_g'))
        th       = _safe_dict(ing.get('thresholds'))
        mn       = _safe_float(th.get('min_stock_g'))
        ro       = _safe_float(th.get('reorder_point_g'))
        supplier = _safe_dict(ing.get('supplier')).get('name', '')
        lead     = _safe_dict(ing.get('supplier')).get('lead_time_days', '?')
        item = {
            'name':     ing.get('name', '—'),
            'qty_kg':   qty / 1000,
            'min_kg':   mn  / 1000,
            'ro_kg':    ro  / 1000,
            'supplier': supplier,
            'lead':     lead,
        }
        if mn > 0 and qty <= mn:
            critical.append(item)
        elif ro > 0 and qty <= ro:
            low.append(item)

    if not critical and not low:
        await message.answer('✅ Все остатки в норме! Критических позиций нет.')
        return

    text = '⚠️ <b>Критические остатки</b>\n\n'
    if critical:
        text += '🔴 <b>СРОЧНО — ниже минимума:</b>\n'
        for it in critical:
            text += (
                f'  • <b>{it["name"]}</b>\n'
                f'    Остаток: {it["qty_kg"]:.3f} кг / Мин: {it["min_kg"]:.1f} кг\n'
            )
            if it['supplier']:
                text += f'    📞 {it["supplier"]} ({it["lead"]} дн.)\n'
        text += '\n'

    if low:
        text += '🟡 <b>Скоро закончится:</b>\n'
        for it in low:
            text += (
                f'  • {it["name"]}: {it["qty_kg"]:.3f} кг'
                f' (заказ от {it["ro_kg"]:.1f} кг)\n'
            )

    text += f'\n<i>Приход: {SITE_URL}/stock.html</i>'
    await message.answer(text, parse_mode='HTML')


# ══════════════════════════════════════════════════════
# /sales
# ══════════════════════════════════════════════════════

@dp.message(Command('sales'))
async def cmd_sales(message: types.Message):
    _upsert_user(message)

    data_key = _resolve_data_key(message)

    try:
        raw = db.reference(f'sales/{data_key}/main').get()
    except Exception as e:
        await message.answer(f'❌ Ошибка Firebase: {e}')
        return

    if not raw:
        await message.answer(
            f'📈 Продаж пока нет.\n\n'
            f'Внеси первую продажу на {SITE_URL}/sales.html'
        )
        return

    sales = sorted(
        [v for v in raw.values() if isinstance(v, dict)],
        key=lambda s: s.get('timestamp', ''),
        reverse=True
    )[:10]

    total_rev    = sum(_safe_float(_safe_dict(s.get('totals')).get('total_revenue')) for s in sales)
    total_profit = sum(_safe_float(_safe_dict(s.get('totals')).get('gross_profit'))  for s in sales)

    text = f'📈 <b>Последние {len(sales)} продаж</b>\n\n'
    for s in sales:
        dt     = s.get('date') or s.get('timestamp', '')[:10]
        t      = _safe_dict(s.get('totals'))
        rev    = _safe_float(t.get('total_revenue'))
        profit = _safe_float(t.get('gross_profit'))
        margin = _safe_float(t.get('margin_pct'))
        pors   = _safe_float(t.get('total_portions'))
        source = '✏️' if s.get('source') == 'manual' else '🤖'
        wo     = ' ✅' if s.get('write_off_triggered') else ''
        items_str = ''
        for item in _safe_list(s.get('items'))[:2]:
            if isinstance(item, dict):
                items_str += f'    · {item.get("recipe_name","—")} ×{item.get("portions_sold",0)}\n'
        text += (
            f'{source} <b>{dt}</b> — {rev:.0f} ₽'
            f' | прибыль {profit:.0f} ₽ ({margin:.0f}%)'
            f' | {pors:.0f} пор{wo}\n'
            f'{items_str}'
        )

    text += '─' * 20 + '\n'
    text += f'💵 Итого выручка: <b>{total_rev:.0f} ₽</b>\n'
    text += f'💚 Итого прибыль: <b>{total_profit:.0f} ₽</b>\n'
    text += f'\n<i>Аналитика: {SITE_URL}/sales.html</i>'

    await _send_long(message, text)


# ══════════════════════════════════════════════════════
# /help
# ══════════════════════════════════════════════════════

@dp.message(Command('help'))
async def cmd_help(message: types.Message):
    await message.answer(
        '❓ <b>PYRA Bot — помощь</b>\n\n'
        '<b>Рецептуры:</b>\n'
        '/recipes — список всех рецептур\n'
        '/recipe 1 — полная техкарта №1\n\n'
        '<b>Склад:</b>\n'
        '/stock — все остатки с ценами\n'
        '/lowstock — только критические позиции\n\n'
        '<b>Продажи:</b>\n'
        '/sales — последние 10 продаж\n\n'
        f'🌐 <b>Сайт:</b> {SITE_URL}\n'
        'Рецептуры сохраняются через сайт.\n\n'
        '🔗 /link — получить код привязки сайта',
        parse_mode='HTML'
    )


# ══════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
