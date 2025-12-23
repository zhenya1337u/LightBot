import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Dict

# Сторонние библиотеки
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger
import aiohttp
from cachetools import TTLCache

# --- CONFIGURATION LAYER ---
# Используем dataclass для типизированной конфигурации
@dataclass
class Config:
    token: str = os.getenv("BOT_TOKEN", "")
    # URL сайта (заглушка, сюда нужно будет вставить реальный API или URL парсинга)
    source_url: str = "https://svitlo.oe.if.ua/api/schedule" 

# --- SERVICE LAYER (Бизнес-логика) ---
# Этот слой отвечает ТОЛЬКО за получение данных. Он ничего не знает про Telegram.

class LightStatus(Enum):
    ON = "light_on"
    OFF = "light_off"
    POSSIBLE = "light_possible"
    UNKNOWN = "unknown"

@dataclass
class ScheduleData:
    status: LightStatus
    message: str
    next_change: str

class EnergyProvider:
    def __init__(self):
        # Кэш на 1000 записей, каждая живет 60 секунд. 
        # Это спасет нас от бана по IP сайтом-донором.
        self.cache = TTLCache(maxsize=1000, ttl=60)
        self.session: Optional[aiohttp.ClientSession] = None

    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def close(self):
        if self.session:
            await self.session.close()

    async def fetch_status(self, queue: str, subqueue: str) -> ScheduleData:
        cache_key = f"{queue}_{subqueue}"
        
        # 1. Проверяем кэш
        if cache_key in self.cache:
            logger.info(f"Cache hit for {cache_key}")
            return self.cache[cache_key]

        # 2. Если нет в кэше — идем в сеть (симуляция запроса)
        try:
            # session = await self.get_session()
            # async with session.get(...) as resp:
            #     data = await resp.json()
            
            # ТУТ БУДЕТ РЕАЛЬНЫЙ ПАРСИНГ.
            # Пока симулируем ответ API как на скриншоте пользователя
            
            # Имитация задержки сети
            await asyncio.sleep(0.5) 
            
            # Мок-данные (Mock Data)
            mock_response = ScheduleData(
                status=LightStatus.OFF,
                message=f"Черга {queue}.{subqueue}: Світла немає",
                next_change="через 1 год 49 хв (о 17:00)"
            )
            
            # 3. Сохраняем в кэш
            self.cache[cache_key] = mock_response
            return mock_response

        except Exception as e:
            logger.error(f"Error fetching data: {e}")
            return ScheduleData(LightStatus.UNKNOWN, "Ошибка получения данных", "---")

# --- FSM (Машина состояний) ---
class UserSettings(StatesGroup):
    choosing_queue = State()
    choosing_subqueue = State()
    main_menu = State()

# --- HANDLERS LAYER (Взаимодействие с пользователем) ---

async def get_main_keyboard(queue_info: str):
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="💡 Статус сейчас", callback_data="status_now"))
    builder.row(types.InlineKeyboardButton(text="📅 График на день", callback_data="schedule_day"))
    builder.row(types.InlineKeyboardButton(text=f"⚙️ Изменить ({queue_info})", callback_data="change_settings"))
    return builder.as_markup()

# Инициализация бота и провайдера
provider = EnergyProvider()
dp = Dispatcher(storage=MemoryStorage())
bot = Bot(token=Config.token)

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    logger.info(f"User {message.from_user.id} started bot")
    await message.answer(
        "👋 Привет! Я профессиональный монитор отключений.\n"
        "Давай настроим твою очередь. Выбери номер очереди:",
        reply_markup=generate_queue_kb()
    )
    await state.set_state(UserSettings.choosing_queue)

def generate_queue_kb():
    builder = InlineKeyboardBuilder()
    # Генерируем кнопки 1-6
    for i in range(1, 7):
        builder.add(types.InlineKeyboardButton(text=f"Очередь {i}", callback_data=f"queue_{i}"))
    builder.adjust(3)
    return builder.as_markup()

def generate_subqueue_kb(queue_num: str):
    builder = InlineKeyboardBuilder()
    # Генерируем под-очереди .1 - .4
    for i in range(1, 5):
        full_code = f"{queue_num}.{i}"
        builder.add(types.InlineKeyboardButton(text=f"{full_code}", callback_data=f"sub_{i}"))
    builder.row(types.InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_queue"))
    builder.adjust(2)
    return builder.as_markup()

@dp.callback_query(UserSettings.choosing_queue, F.data.startswith("queue_"))
async def process_queue_choice(callback: types.CallbackQuery, state: FSMContext):
    queue_num = callback.data.split("_")[1]
    await state.update_data(queue=queue_num)
    
    await callback.message.edit_text(
        f"✅ Очередь {queue_num} выбрана.\nТеперь выбери под-очередь:",
        reply_markup=generate_subqueue_kb(queue_num)
    )
    await state.set_state(UserSettings.choosing_subqueue)

@dp.callback_query(UserSettings.choosing_subqueue, F.data.startswith("sub_"))
async def process_subqueue_choice(callback: types.CallbackQuery, state: FSMContext):
    sub_num = callback.data.split("_")[1]
    data = await state.get_data()
    queue_num = data.get("queue")
    
    full_group = f"{queue_num}.{sub_num}"
    await state.update_data(subqueue=sub_num, full_group=full_group)
    
    await callback.message.edit_text(
        f"🎉 Настройка завершена!\nТвоя группа: **{full_group}**",
        reply_markup=await get_main_keyboard(full_group),
        parse_mode="Markdown"
    )
    await state.set_state(UserSettings.main_menu)

@dp.callback_query(F.data == "change_settings")
async def change_settings(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await cmd_start(callback.message, state)

@dp.callback_query(F.data == "status_now")
async def check_status_handler(callback: types.CallbackQuery, state: FSMContext):
    # Получаем данные пользователя из State (памяти)
    data = await state.get_data()
    q, sq = data.get("queue"), data.get("subqueue")
    
    if not q or not sq:
        await callback.answer("Сначала выберите очередь!", show_alert=True)
        return

    # Запрашиваем данные у провайдера
    # Тут происходит магия кэширования и асинхронности
    schedule_data = await provider.fetch_status(q, sq)
    
    # Визуальное оформление
    icon = "⬛" if schedule_data.status == LightStatus.OFF else "🟦"
    if schedule_data.status == LightStatus.POSSIBLE: icon = "⬜"

    text = (
        f"{icon} **СТАТУС: {schedule_data.message}**\n\n"
        f"⏳ Следующее изменение: {schedule_data.next_change}\n"
        f"🕒 Обновлено: {datetime.now().strftime('%H:%M:%S')}"
    )
    
    # Edit message text, чтобы не спамить новыми сообщениями
    try:
        await callback.message.edit_text(
            text, 
            reply_markup=await get_main_keyboard(data.get("full_group")),
            parse_mode="Markdown"
        )
    except Exception:
        # Если текст не изменился, Telegram вернет ошибку, игнорируем её
        await callback.answer("Данные актуальны")

# --- ENTRY POINT ---
async def main():
    # Настройка логирования
    logger.add(sys.stderr, format="{time} {level} {message}", level="INFO")
    
    logger.info("Starting bot...")
    try:
        await dp.start_polling(bot)
    finally:
        await provider.close() # Корректное закрытие соединений

if __name__ == "__main__":
    if not Config.token:
        logger.error("BOT_TOKEN is not set!")
        sys.exit(1)
    asyncio.run(main())
