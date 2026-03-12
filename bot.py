import os
import json
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
import firebase_admin
from firebase_admin import credentials, db
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
FIREBASE_URL = os.getenv('FIREBASE_URL')
FIREBASE_KEY_JSON = os.getenv('FIREBASE_KEY_JSON')

# Инициализация Firebase — из файла или из переменной окружения
if FIREBASE_KEY_JSON:
    key_dict = json.loads(FIREBASE_KEY_JSON)
    cred = credentials.Certificate(key_dict)
else:
    cred = credentials.Certificate('firebase-key.json')

firebase_admin.initialize_app(cred, {
    'databaseURL': FIREBASE_URL
})

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command('start'))
async def cmd_start(message: types.Message):
    user_id = str(message.from_user.id)
    name = message.from_user.first_name
    username = message.from_user.username or ''
    ref = db.reference(f'users/{username or user_id}')
    ref.set({'name': name, 'username': username, 'plan': 'free'})
    await message.answer(
        f'👋 Привет, {name}!\n\n'
        f'Я бот <b>Цена Рецепта</b> — твоя библиотека рецептур.\n\n'
        f'Зайди на сайт calc.pyra.com.ru, посчитай рецептуру и сохрани её.\n\n'
        f'Команды:\n'
        f'/recipes — мои рецептуры\n'
        f'/recipe 1 — полная техкарта №1\n'
        f'/help — помощь',
        parse_mode='HTML'
    )

def format_recipe(recipe):
    name = recipe.get('name', 'Без названия')
    yield_count = recipe.get('yield_count', 1)
    cost = recipe.get('cost_per_unit', 0)
    price = recipe.get('price_per_unit', 0)
    markup = recipe.get('markup_pct', 100)

    text = f'📋 <b>{name}</b>\n'
    text += f'Выход: {yield_count} шт\n'
    text += '─' * 22 + '\n'

    ingredients = recipe.get('ingredients', [])
    if ingredients:
        text += '\n🧁 <b>Ингредиенты:</b>\n'
        ing_total = 0
        for ing in ingredients:
            n = ing.get('name', '')
            price_pkg = ing.get('price', 0)
            pkg = ing.get('pkg', 1) or 1
            amount = ing.get('recipe', 0)
            cost_ing = (price_pkg / pkg) * amount / yield_count
            ing_total += cost_ing
            if n:
                text += f'  • {n}: {amount}г — {cost_ing:.2f} ₽/шт\n'
        text += f'  <i>Итого: {ing_total:.2f} ₽/шт</i>\n'

    consumables = recipe.get('consumables', [])
    if consumables:
        text += '\n📦 <b>Расходники:</b>\n'
        cons_total = 0
        for c in consumables:
            n = c.get('name', '')
            price_pkg = c.get('price', 0)
            pkg = c.get('pkg', 1) or 1
            per = c.get('per', 0)
            cost_c = (price_pkg / pkg) * per
            cons_total += cost_c
            if n:
                text += f'  • {n}: {per} шт — {cost_c:.2f} ₽/шт\n'
        text += f'  <i>Итого: {cons_total:.2f} ₽/шт</i>\n'

    decor = recipe.get('decor', [])
    if decor:
        text += '\n✨ <b>Декор и упаковка:</b>\n'
        decor_total = 0
        for d in decor:
            n = d.get('name', '')
            price_pkg = d.get('price', 0)
            pkg = d.get('pkg', 1) or 1
            per = d.get('per', 0)
            cost_d = (price_pkg / pkg) * per
            decor_total += cost_d
            if n:
                text += f'  • {n}: {per} шт — {cost_d:.2f} ₽/шт\n'
        text += f'  <i>Итого: {decor_total:.2f} ₽/шт</i>\n'

    text += '\n' + '─' * 22 + '\n'
    text += f'💰 <b>Себестоимость: {cost:.2f} ₽/шт</b>\n'
    text += f'🏷 <b>Цена продажи (х{1+markup/100:.1f}): {price:.2f} ₽/шт</b>\n'

    return text

@dp.message(Command('recipes'))
async def cmd_recipes(message: types.Message):
    username = message.from_user.username
    if not username:
        await message.answer('⚠️ У тебя не установлен username в Telegram.\nЗайди в Настройки → Изменить профиль → Имя пользователя.')
        return

    ref = db.reference(f'recipes/{username}')
    recipes = ref.get()

    if not recipes:
        await message.answer('📋 У тебя пока нет сохранённых рецептур.\n\nЗайди на сайт calc.pyra.com.ru, посчитай рецепт и сохрани его.')
        return

    text = '📚 <b>Твои рецептуры:</b>\n\n'
    for i, (recipe_id, recipe) in enumerate(recipes.items(), 1):
        name = recipe.get('name', 'Без названия')
        price = recipe.get('price_per_unit', 0)
        text += f'{i}. {name} — {price:.2f} ₽/шт\n'

    text += '\n<i>Напиши /recipe 1 чтобы открыть полную техкарту</i>'
    await message.answer(text, parse_mode='HTML')

@dp.message(Command('recipe'))
async def cmd_recipe(message: types.Message):
    username = message.from_user.username
    if not username:
        await message.answer('⚠️ У тебя не установлен username в Telegram.')
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer('Напиши номер рецептуры. Например: /recipe 1')
        return

    try:
        num = int(args[1]) - 1
    except ValueError:
        await message.answer('Напиши число. Например: /recipe 1')
        return

    ref = db.reference(f'recipes/{username}')
    recipes = ref.get()

    if not recipes:
        await message.answer('У тебя пока нет рецептур. Зайди на calc.pyra.com.ru')
        return

    items = list(recipes.values())
    if num < 0 or num >= len(items):
        await message.answer(f'Рецептура #{num+1} не найдена. У тебя {len(items)} рецептур.')
        return

    await message.answer(format_recipe(items[num]), parse_mode='HTML')

@dp.message(Command('help'))
async def cmd_help(message: types.Message):
    await message.answer(
        '❓ <b>Помощь</b>\n\n'
        '/start — начать\n'
        '/recipes — список всех рецептур\n'
        '/recipe 1 — полная техкарта №1\n\n'
        'Рецептуры сохраняются через сайт calc.pyra.com.ru',
        parse_mode='HTML'
    )

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
