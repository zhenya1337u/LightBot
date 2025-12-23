import asyncio
import os
import sys
import time
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
    # Новый endpoint, который вы нашли
    api_url: str = "https://m.nizhyn.online/no_electro/get_display_schedule.php"
    # Referrer нужен, чтобы сервер не блокировал запрос
    referer: str = "https://m.nizhyn.online/no_electro/index.php"

# --- SERVICE LAYER (API и логика) ---

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
        self.cache = TTLCache(maxsize=1000, ttl=60)
        self.session: Optional[aiohttp.ClientSession] = None
        self.ua = UserAgent()

    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def fetch_real_status(self, queue: str, subqueue: str) -> ScheduleData:
        full_queue_id = f"{queue}.{subqueue}"
        cache_key = f"{queue}_{subqueue}"

        if cache_key in self.cache:
            return self.cache[cache_key]

        try:
            session = await self.get_session()
            params = {"queue": queue, "subqueue": subqueue, "ts": int(time.time() * 1000)}
            headers = {"User-Agent": self.ua.random, "Referer": Config.referer}
            
            async with session.get(Config.api_url, params=params, headers=headers, timeout=10) as resp:
                data = await resp.json() # Работаем напрямую с JSON

            if not data.get("success"):
                return ScheduleData(LightStatus.UNKNOWN, "⚠️ Помилка сайту", "")

            intervals = data["data"]["today"]["intervals"]
            now_str = datetime.now().strftime("%H:%M")
            
            current_status = LightStatus.UNKNOWN
            next_event_time = None
            timeline = ""
            
            # Логика обработки интервалов
            for i, interval in enumerate(intervals):
                start, end = interval["start"], interval["end"]
                status = interval["status"] # "on", "off" или "maybe"
                
                # Формируем шкалу (каждый символ = 1 час, т.е. 2 интервала по 30 мин)
                if i % 2 == 0:
                    char = "🟦" if status == "on" else "⬛" if status == "off" else "⬜"
                    timeline += char

                # Определяем текущий статус
                if start <= now_str < end:
                    current_status = LightStatus.ON if status == "on" else LightStatus.OFF if status == "off" else LightStatus.POSSIBLE
                    # Ищем, когда статус изменится
                    for future in intervals[i+1:]:
                        if future["status"] != status:
                            next_event_time = future["start"]
                            break
            
            # Красивый вывод
            status_map = {
                LightStatus.ON: ("🟢 Світло зараз є", "Відключення"),
                LightStatus.OFF: ("🔴 Світла зараз немає", "Включення"),
                LightStatus.POSSIBLE: ("🟡 Можливе відключення", "Зміна")
            }
            
            status_text, event_name = status_map.get(current_status, ("❓ Невідомо", "Зміна"))
            
            msg = f"**{status_text}**\n"
            if next_event_time:
                msg += f"⏳ {event_name} планується о **{next_event_time}**\n"
            
            msg += f"\nГрафік на сьогодні (00:00 - 24:00):\n`{timeline}`\n"
            msg += "🟦-є | ⬛-немає | ⬜-можливо"

            result = ScheduleData(
                status=current_status,
                message=msg,
                updated_at=datetime.now().strftime("%H:%M")
            )
            self.cache[cache_key] = result
            return result

        except Exception as e:
            logger.error(f"JSON Parse error: {e}")
            return ScheduleData(LightStatus.UNKNOWN, "❌ Помилка обробки даних", "")

# --- FSM & HANDLERS ---
# (Эта часть остается без изменений, она идеальна)

class UserSettings(StatesGroup):
    choosing_queue = State()
    choosing_subqueue = State()
    main_menu = State()

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
        "👋 Привіт! Я моніторю **m.nizhyn.online**.\n"
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
    
    # Отправляем "печатает...", чтобы пользователь видел реакцию
    await bot.send_chat_action(callback.from_user.id, action="typing")
    
    info = await provider.fetch_real_status(q, sq)
    
    text = (
        f"📊 **Черга {data.get('full_group')}**\n\n"
        f"{info.message}\n\n"
        f"🕒 Оновлено: {info.updated_at}"
    )
    
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
    logger.info("Bot starting on Koyeb (API Direct Mode)...")
    try:
        await dp.start_polling(bot)
    finally:
        await provider.close()

if __name__ == "__main__":
    if not Config.token:
        logger.error("BOT_TOKEN is not set!")
        sys.exit(1)
    asyncio.run(main())
