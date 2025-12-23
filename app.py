import asyncio
import os
import sys
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

# Сторонние библиотеки
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger
import aiohttp
from cachetools import TTLCache
from bs4 import BeautifulSoup
from fake_useragent import UserAgent

# --- CONFIGURATION LAYER ---
@dataclass
class Config:
    token: str = os.getenv("BOT_TOKEN", "")
    target_url: str = "https://m.nizhyn.online/noelectro/"

# --- SERVICE LAYER (Парсинг и логика) ---

class LightStatus(Enum):
    ON = "light_on"          # Світло є
    OFF = "light_off"        # Світла немає
    POSSIBLE = "light_possible" # Можливе відключення
    UNKNOWN = "unknown"      # Не вдалося визначити

@dataclass
class ScheduleData:
    status: LightStatus
    message: str
    updated_at: str

class EnergyProvider:
    def __init__(self):
        # Кэшируем ответы на 60 секунд, чтобы не нагружать сайт
        self.cache = TTLCache(maxsize=1000, ttl=60)
        self.session: Optional[aiohttp.ClientSession] = None
        self.ua = UserAgent()

    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def close(self):
        if self.session:
            await self.session.close()

    async def fetch_real_status(self, queue: str, subqueue: str) -> ScheduleData:
        """
        Парсит сайт m.nizhyn.online.
        Ищет блоки, содержащие номер очереди (например '6.2'), и определяет статус.
        """
        full_queue = f"{queue}.{subqueue}" # Например "6.2"
        cache_key = f"q_{full_queue}"

        if cache_key in self.cache:
            logger.info(f"Cache hit for {full_queue}")
            return self.cache[cache_key]

        try:
            logger.info(f"Fetching data from {Config.target_url}")
            session = await self.get_session()
            
            # Притворяемся мобильным браузером
            headers = {'User-Agent': self.ua.random}
            
            async with session.get(Config.target_url, headers=headers, timeout=10) as resp:
                if resp.status != 200:
                    logger.error(f"Site returned status {resp.status}")
                    return ScheduleData(LightStatus.UNKNOWN, "Сайт недоступен", datetime.now().strftime("%H:%M"))
                
                html = await resp.text()

            # Парсим HTML
            soup = BeautifulSoup(html, "lxml")
            
            # Логика поиска: ищем текст, похожий на очередь
            # На сайте обычно структура: <div>Черга 6.2</div> ... <div>Статус</div>
            # Или таблица. Мы используем универсальный поиск по тексту.
            
            status = LightStatus.UNKNOWN
            details = "Дані не знайдено"

            # Ищем элемент, содержащий номер очереди (например "6.2")
            # Используем регулярку, чтобы найти именно "6.2", а не "16.20"
            target_el = soup.find(string=re.compile(fr"\b{re.escape(full_queue)}\b"))

            if target_el:
                # Обычно статус находится в родительском контейнере или соседнем элементе
                # Поднимаемся к родительскому блоку (карточке)
                parent = target_el.find_parent('div') or target_el.find_parent('tr')
                
                if parent:
                    text_content = parent.get_text(separator=" ", strip=True).lower()
                    
                    # Анализ текста на ключевые слова
                    if "немає" in text_content or "вимкнено" in text_content or "відсутнє" in text_content:
                        status = LightStatus.OFF
                        details = "Світла немає ⬛"
                    elif "є світло" in text_content or "увімкнено" in text_content or "заживлено" in text_content:
                        status = LightStatus.ON
                        details = "Світло є 🟦"
                    else:
                        # Если текст не понятен, пробуем найти цветные индикаторы (классы css)
                        # Часто используют классы red/green
                        css_classes = str(parent).lower()
                        if "red" in css_classes or "danger" in css_classes:
                            status = LightStatus.OFF
                            details = "Світла немає (визначено по кольору) ⬛"
                        elif "green" in css_classes or "success" in css_classes:
                            status = LightStatus.ON
                            details = "Світло є (визначено по кольору) 🟦"
                else:
                    details = "Знайдено чергу, але статус неясний"
            else:
                # Если прям "6.2" не нашли, возможно там формат "6 черга, 2 підчерга"
                # Тут можно добавить более сложную логику, но пока вернем базовый ответ
                details = "Чергу на сторінці не знайдено. Перевірте номер."

            result = ScheduleData(
                status=status,
                message=details,
                updated_at=datetime.now().strftime("%H:%M")
            )
            
            self.cache[cache_key] = result
            return result

        except Exception as e:
            logger.error(f"Parse error: {e}")
            return ScheduleData(LightStatus.UNKNOWN, "Помилка парсингу", datetime.now().strftime("%H:%M"))

# --- FSM & HANDLERS ---

class UserSettings(StatesGroup):
    choosing_queue = State()
    choosing_subqueue = State()
    main_menu = State()

# Инициализация
provider = EnergyProvider()
dp = Dispatcher(storage=MemoryStorage())
bot = Bot(token=Config.token)

async def get_main_keyboard(queue_info: str):
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="💡 Статус зараз", callback_data="status_now"))
    builder.row(types.InlineKeyboardButton(text=f"⚙️ Змінити ({queue_info})", callback_data="change_settings"))
    return builder.as_markup()

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await message.answer(
        "👋 Привіт! Я моніторю сайт **m.nizhyn.online**.\n"
        "Обери свою чергу:",
        reply_markup=generate_queue_kb()
    )
    await state.set_state(UserSettings.choosing_queue)

def generate_queue_kb():
    builder = InlineKeyboardBuilder()
    for i in range(1, 7):
        builder.add(types.InlineKeyboardButton(text=f"Черга {i}", callback_data=f"queue_{i}"))
    builder.adjust(3)
    return builder.as_markup()

def generate_subqueue_kb(queue_num: str):
    builder = InlineKeyboardBuilder()
    for i in range(1, 5):
        builder.add(types.InlineKeyboardButton(text=f"{queue_num}.{i}", callback_data=f"sub_{i}"))
    builder.row(types.InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_queue"))
    builder.adjust(2)
    return builder.as_markup()

@dp.callback_query(UserSettings.choosing_queue, F.data.startswith("queue_"))
async def process_queue(callback: types.CallbackQuery, state: FSMContext):
    q = callback.data.split("_")[1]
    await state.update_data(queue=q)
    await callback.message.edit_text(f"✅ Черга {q}. Обери підчергу:", reply_markup=generate_subqueue_kb(q))
    await state.set_state(UserSettings.choosing_subqueue)

@dp.callback_query(UserSettings.choosing_subqueue, F.data == "back_to_queue")
async def back_handler(callback: types.CallbackQuery, state: FSMContext):
    await cmd_start(callback.message, state)

@dp.callback_query(UserSettings.choosing_subqueue, F.data.startswith("sub_"))
async def process_subqueue(callback: types.CallbackQuery, state: FSMContext):
    sub = callback.data.split("_")[1]
    data = await state.get_data()
    q = data.get("queue")
    full = f"{q}.{sub}"
    await state.update_data(subqueue=sub, full_group=full)
    await callback.message.edit_text(
        f"✅ Налаштовано: **{full}**\nТисни кнопку нижче 👇",
        reply_markup=await get_main_keyboard(full),
        parse_mode="Markdown"
    )
    await state.set_state(UserSettings.main_menu)

@dp.callback_query(F.data == "change_settings")
async def change(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await cmd_start(callback.message, state)

@dp.callback_query(F.data == "status_now")
async def check_status(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    q, sq = data.get("queue"), data.get("subqueue")
    
    # Отправляем "печатает...", так как парсинг может занять секунду
    await bot.send_chat_action(callback.from_user.id, action="typing")
    
    info = await provider.fetch_real_status(q, sq)
    
    text = (
        f"📊 **Черга {data.get('full_group')}**\n\n"
        f"{info.message}\n"
        f"🕒 Оновлено: {info.updated_at}"
    )
    
    # Чтобы избежать ошибки "message not modified"
    try:
        await callback.message.edit_text(
            text, 
            reply_markup=await get_main_keyboard(data.get("full_group")),
            parse_mode="Markdown"
        )
    except Exception:
        await callback.answer()

async def main():
    logger.add(sys.stderr, format="{time} {level} {message}", level="INFO")
    logger.info("Bot starting on Koyeb...")
    try:
        await dp.start_polling(bot)
    finally:
        await provider.close()

if __name__ == "__main__":
    asyncio.run(main())
