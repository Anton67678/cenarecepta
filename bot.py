import os
import asyncio
import json
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
# HELPERS
# ══════════════════════════════════════════════════════

def _flatten_recipes(raw: dict) -> list:
    """
    Возвращает плоский список рецептов (dict).
    Поддерживает два уровня вложенности Firebase:
      - recipes/{username}/{push_id}           → прямой рецепт
      - recipes/{username}/{outlet}/{push_id}  → вложенный
    Сортировка: сначала старый формат (v1), потом новый (Pro), внутри — по имени.
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


# ══════════════════════════════════════════════════════
# ФОРМАТИРОВАНИЕ РЕЦЕПТА
# ══════════════════════════════════════════════════════

def format_recipe(recipe: dict) -> str:
    if _is_new_format(recipe):
        return format_recipe_new(recipe)
    return format_recipe_old(recipe)


def format_recipe_old(recipe: dict) -> str:
    """Старый формат — кондитерский калькулятор v1"""
    name        = recipe.get('name', 'Без названия')
    yield_count = max(1, recipe.get('yield_count', 1) or 1)
    cost        = recipe.get('cost_per_unit', 0) or 0
    price       = recipe.get('price_per_unit', 0) or 0
    markup      = recipe.get('markup_pct', 0) or 0

    text  = f'📋 <b>{name}</b>\n'
    text += f'Выход: {yield_count} шт\n'
    text += '─' * 22 + '\n'

    ingredients = recipe.get('ingredients', []) or []
    if ingredients:
        text += '\n🧁 <b>Ингредиенты:</b>\n'
        ing_total = 0
        for ing in ingredients:
            n         = ing.get('name', '')
            price_pkg = ing.get('price', 0) or 0
            pkg       = ing.get('pkg', 1) or 1
            amount    = (ing.get('recipe', 0) or 0) / yield_count
            cost_ing  = (price_pkg / pkg) * amount
            ing_total += cost_ing
            if n:
                text += f'  • {n}: {amount:.1f}г — {cost_ing:.2f} ₽/шт\n'
        text += f'  <i>Итого: {ing_total:.2f} ₽/шт</i>\n'

    for section, label, icon in [
        ('consumables', 'Расходники', '📦'),
        ('decor',       'Декор',      '✨'),
    ]:
        items = recipe.get(section, []) or []
        if items:
            text += f'\n{icon} <b>{label}:</b>\n'
            total = 0
            for c in items:
                n      = c.get('name', '')
                p      = c.get('price', 0) or 0
                pkg    = c.get('pkg', 1) or 1
                per    = (c.get('per', 0) or 0) / yield_count
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

    yld    = recipe.get('yield', {}) or {}
    batch  = yld.get('batch_weight_g', 0) or 0
    units  = yld.get('units_count') or yld.get('portions_count', 1) or 1
    unit_w = yld.get('unit_weight_g') or yld.get('portion_weight_g', 0) or 0

    unit_label = 'шт' if rtype == 'bakery' else 'пор'

    text  = f'📋 <b>{name}</b>\n'
    if group:
        text += f'Группа: {group}\n'
    if method:
        text += f'Метод: {method}\n'
    text += f'Выход: {units} {unit_label}'
    if unit_w:
        text += f' по {unit_w:.0f} г'
    if batch:
        text += f' (замес {batch:.0f} г)'
    text += '\n'
    text += '─' * 22 + '\n'

    ingredients = recipe.get('ingredients', []) or []
    if ingredients:
        text += '\n🧁 <b>Ингредиенты (на замес):</b>\n'
        for ing in ingredients:
            n       = ing.get('name', '')
            brutto  = ing.get('brutto_g') or ing.get('brutto', 0) or 0
            netto   = ing.get('netto_g')  or ing.get('netto', brutto) or brutto
            waste   = ing.get('waste_g', 0) or 0
            price_k = ing.get('price_per_kg', 0) or 0
            cost_r  = (netto / 1000) * price_k
            if not n:
                continue
            waste_str = f' (отх. {waste:.0f}г)' if waste > 0.5 else ''
            text += (
                f'  • {n}: {brutto:.0f}г → {netto:.0f}г{waste_str}'
                f' — {cost_r:.2f} ₽\n'
            )

    pricing  = recipe.get('pricing', {}) or {}
    cost_u   = pricing.get('cost_per_unit', 0) or 0
    price_u  = pricing.get('price_per_unit', 0) or 0
    markup   = pricing.get('markup_pct', 0) or 0
    vat      = pricing.get('vat_pct', 0) or 0

    text += '\n' + '─' * 22 + '\n'
    text += f'💰 <b>Себестоимость: {cost_u:.2f} ₽/{unit_label}</b>\n'
    text += f'🏷 <b>Цена (наценка {markup:.0f}%, НДС {vat:.0f}%): {price_u:.2f} ₽/{unit_label}</b>\n'

    nutrition = recipe.get('nutrition', {}) or {}
    per_unit  = nutrition.get('per_unit', {}) or {}
    if per_unit.get('cal'):
        text += (
            f'\n🥗 КБЖУ/{unit_label}: '
            f'{per_unit["cal"]:.0f} ккал · '
            f'Б{per_unit.get("prot", 0):.1f} '
            f'Ж{per_unit.get("fat", 0):.1f} '
            f'У{per_unit.get("carb", 0):.1f}\n'
        )

    # Отходы — из catering формата
    wo = (recipe.get('stock_write_off', {}) or {}).get('per_portion', []) or []
    waste_items = [i for i in wo if (i.get('waste_g_per_unit') or 0) > 0.5]
    if waste_items:
        text += '\n♻️ <b>Отходы/порция:</b>\n'
        for w in waste_items[:5]:
            text += f'  • {w.get("ingredient","")}: {w["waste_g_per_unit"]:.1f} г\n'

    return text


# ══════════════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════════════

@dp.message(Command('start'))
async def cmd_start(message: types.Message):
    username = message.from_user.username or ''
    name     = message.from_user.first_name
    uid      = str(message.from_user.id)
    key      = username or uid

    import datetime
    db.reference(f'users/{key}').set({
        'name': name,
        'username': username,
        'plan': 'free',
        'registered': datetime.datetime.utcnow().isoformat()
    })

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
        f'/help — помощь',
        parse_mode='HTML'
    )


# ══════════════════════════════════════════════════════
# /recipes
# ══════════════════════════════════════════════════════

@dp.message(Command('recipes'))
async def cmd_recipes(message: types.Message):
    username = message.from_user.username
    if not username:
        await message.answer(
            '⚠️ У тебя не установлен username в Telegram.\n'
            'Настройки → Изменить профиль → Имя пользователя.'
        )
        return

    try:
        raw = db.reference(f'recipes/{username}').get()
    except Exception as e:
        await message.answer(f'❌ Ошибка Firebase: {e}')
        return

    if not raw:
        await message.answer(
            f'📋 Рецептур пока нет.\n\n'
            f'Зайди на {SITE_URL}, посчитай и сохрани рецепт.'
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

    # Разделяем на старый и новый формат с нумерацией
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
            price = r.get('price_per_unit', 0) or 0
            text += f'  {i}. {r.get("name", "—")} — {price:.2f} ₽/шт\n'

    if new_list:
        if old_list:
            text += '\n'
        text += '🎂 <b>Новый формат (Pro):</b>\n'
        for i, r in new_list:
            pricing = r.get('pricing', {}) or {}
            price   = pricing.get('price_per_unit', 0) or 0
            group   = r.get('group') or r.get('product_group') or ''
            group_s = f' ({group})' if group else ''
            text += f'  {i}. {r.get("name", "—")}{group_s} — {price:.2f} ₽\n'

    text += '\n<i>/recipe N — открыть полную техкарту</i>'

    if len(text) > 4000:
        for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
            await message.answer(chunk, parse_mode='HTML')
    else:
        await message.answer(text, parse_mode='HTML')


# ══════════════════════════════════════════════════════
# /recipe N
# ══════════════════════════════════════════════════════

@dp.message(Command('recipe'))
async def cmd_recipe(message: types.Message):
    username = message.from_user.username
    if not username:
        await message.answer('⚠️ Установи username в Telegram.')
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer('Напиши номер: /recipe 1')
        return

    try:
        num = int(args[1]) - 1  # 0-based
    except ValueError:
        await message.answer('Напиши число: /recipe 1')
        return

    try:
        raw = db.reference(f'recipes/{username}').get()
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

    if len(text) > 4000:
        for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
            await message.answer(chunk, parse_mode='HTML')
    else:
        await message.answer(text, parse_mode='HTML')


# ══════════════════════════════════════════════════════
# /stock
# ══════════════════════════════════════════════════════

@dp.message(Command('stock'))
async def cmd_stock(message: types.Message):
    username = message.from_user.username
    if not username:
        await message.answer('⚠️ Установи username в Telegram.')
        return

    try:
        ingredients = db.reference(f'ingredients/{username}').get() or {}
        stock_data  = db.reference(f'stock/{username}/main').get() or {}
    except Exception as e:
        await message.answer(f'❌ Ошибка Firebase: {e}')
        return

    if not ingredients:
        await message.answer(
            f'📦 Склад пуст.\n\n'
            f'Добавь ингредиенты на {SITE_URL}/stock.html'
        )
        return

    by_cat = {}
    for ing_id, ing in ingredients.items():
        if not isinstance(ing, dict):
            continue
        cat = ing.get('category', 'Прочее')
        if cat not in by_cat:
            by_cat[cat] = []
        qty = (stock_data.get(ing_id, {}) or {}).get('quantity_g', 0) or 0
        th  = ing.get('thresholds', {}) or {}
        by_cat[cat].append({
            'name':     ing.get('name', '—'),
            'qty':      qty,
            'min':      th.get('min_stock_g', 0) or 0,
            'reorder':  th.get('reorder_point_g', 0) or 0,
            'price_kg': ing.get('price_per_kg', 0) or 0,
        })

    total_val = sum(
        ((stock_data.get(iid, {}) or {}).get('quantity_g', 0) or 0) / 1000
        * (ing.get('price_per_kg', 0) or 0)
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
            dot = '🔴' if is_crit else ('🟡' if is_low else '🟢')
            text += f'  {dot} {it["name"]}: {qty_kg:.3f} кг'
            if it['price_kg']:
                text += f' ({qty_kg * it["price_kg"]:.0f} ₽)'
            text += '\n'
        text += '\n'

    text += '─' * 20 + '\n'
    text += f'💵 Стоимость склада: <b>{total_val:.0f} ₽</b>\n'
    text += f'\n<i>Управление: {SITE_URL}/stock.html</i>'

    if len(text) > 4000:
        for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
            await message.answer(chunk, parse_mode='HTML')
    else:
        await message.answer(text, parse_mode='HTML')


# ══════════════════════════════════════════════════════
# /lowstock
# ══════════════════════════════════════════════════════

@dp.message(Command('lowstock'))
async def cmd_lowstock(message: types.Message):
    username = message.from_user.username
    if not username:
        await message.answer('⚠️ Установи username в Telegram.')
        return

    try:
        ingredients = db.reference(f'ingredients/{username}').get() or {}
        stock_data  = db.reference(f'stock/{username}/main').get() or {}
    except Exception as e:
        await message.answer(f'❌ Ошибка Firebase: {e}')
        return

    critical, low = [], []

    for ing_id, ing in ingredients.items():
        if not isinstance(ing, dict):
            continue
        qty      = (stock_data.get(ing_id, {}) or {}).get('quantity_g', 0) or 0
        th       = ing.get('thresholds', {}) or {}
        mn       = th.get('min_stock_g', 0) or 0
        ro       = th.get('reorder_point_g', 0) or 0
        supplier = (ing.get('supplier', {}) or {}).get('name', '')
        lead     = (ing.get('supplier', {}) or {}).get('lead_time_days', '?')
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
    username = message.from_user.username
    if not username:
        await message.answer('⚠️ Установи username в Telegram.')
        return

    try:
        raw = db.reference(f'sales/{username}/main').get()
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

    total_rev    = sum((s.get('totals', {}) or {}).get('total_revenue', 0) or 0 for s in sales)
    total_profit = sum((s.get('totals', {}) or {}).get('gross_profit', 0)  or 0 for s in sales)

    text = f'📈 <b>Последние {len(sales)} продаж</b>\n\n'

    for s in sales:
        dt     = s.get('date') or s.get('timestamp', '')[:10]
        t      = s.get('totals', {}) or {}
        rev    = t.get('total_revenue', 0) or 0
        profit = t.get('gross_profit', 0) or 0
        margin = t.get('margin_pct', 0) or 0
        pors   = t.get('total_portions', 0) or 0
        source = '✏️' if s.get('source') == 'manual' else '🤖'
        wo     = ' ✅' if s.get('write_off_triggered') else ''
        items_str = ''
        for item in (s.get('items') or [])[:2]:
            if isinstance(item, dict):
                items_str += f'    · {item.get("recipe_name","—")} ×{item.get("portions_sold",0)}\n'
        text += (
            f'{source} <b>{dt}</b> — {rev:.0f} ₽'
            f' | прибыль {profit:.0f} ₽ ({margin}%)'
            f' | {pors} пор{wo}\n'
            f'{items_str}'
        )

    text += '─' * 20 + '\n'
    text += f'💵 Итого выручка: <b>{total_rev:.0f} ₽</b>\n'
    text += f'💚 Итого прибыль: <b>{total_profit:.0f} ₽</b>\n'
    text += f'\n<i>Аналитика: {SITE_URL}/sales.html</i>'

    if len(text) > 4000:
        for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
            await message.answer(chunk, parse_mode='HTML')
    else:
        await message.answer(text, parse_mode='HTML')


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
        'Рецептуры сохраняются через сайт.',
        parse_mode='HTML'
    )


# ══════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())