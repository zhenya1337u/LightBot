import asyncio
import os
import sys
import time
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Dict, List

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
from fake_useragent import UserAgent

# --- CONFIGURATION LAYER ---
@dataclass
class ScheduleData:
    status: LightStatus
    message: str
    timeline: str
    next_event_time: Optional[datetime]
    next_event_type: str
    updated_at: str

# --- DATA MODELS ---
class LightStatus(Enum):
    ON = "light_on"
    OFF = "light_off"
    POSSIBLE = "light_possible"
    UNKNOWN = "unknown"

@dataclass
class ScheduleData:
    status: LightStatus
    message: str
    timeline: str
    next_event_time: Optional[datetime]
    next_event_type: str # "Включение" или "Отключение"
    raw_intervals: List[dict] = field(default_factory=list)

# --- STORAGE LAYER (In-Memory DB) ---
# Для продакшена лучше SQLite, но для Koyeb Free и 10 человек этого хватит с головой.
@dataclass
class ChatConfig:
    queue: str = "1"
    subqueue: str = "1"
    notifications_enabled: bool = False
    last_notified_event: Optional[str] = None # Чтобы не спамить об одном и том же

# Глобальное хранилище: chat_id -> Config
chats_db: Dict[int, ChatConfig] = {}

# --- SERVICE LAYER ---
import asyncio
import os
import sys
import time
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List

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
from fake_useragent import UserAgent

# --- CONFIGURATION LAYER ---
@dataclass
class Config:
    token: str = os.getenv("BOT_TOKEN", "")
    # Прямой API-путь, который мы нашли
    api_url: str = "https://m.nizhyn.online/no_electro/get_display_schedule.php"
    referer: str = "https://m.nizhyn.online/no_electro/index.php"

# --- DATA MODELS ---
class LightStatus(Enum):
    ON = "light_on"
    OFF = "light_off"
    POSSIBLE = "light_possible"
    UNKNOWN = "unknown"

@dataclass
class ScheduleData:
    status: LightStatus
    message: str
    next_event_time: Optional[datetime]
    next_event_type: str
    updated_at: str

@dataclass
class ChatConfig:
    queue: str = "1"
    subqueue: str = "1"
    notifications_enabled: bool = False
    last_notified_event: Optional[str] = None # Формат: "17:00_off"

# Временная база данных (chat_id -> ChatConfig)
chats_db: Dict[int, ChatConfig] = {}

# --- SERVICE LAYER (API & Parsing) ---
class EnergyProvider:
    def __init__(self):
        self.cache = TTLCache(maxsize=1000, ttl=60)
        self.session: Optional[aiohttp.ClientSession] = None
        self.ua = UserAgent()

    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def fetch_schedule(self, queue: str, subqueue: str) -> ScheduleData:
        cache_key = f"{queue}_{subqueue}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        try:
            session = await self.get_session()
            params = {
                "queue": queue, 
                "subqueue": subqueue, 
                "ts": int(time.time() * 1000)
            }
            headers = {
                "User-Agent": self.ua.random,
                "Referer": Config.referer,
                "X-Requested-With": "XMLHttpRequest"
            }
            
            async with session.get(Config.api_url, params=params, headers=headers, timeout=10) as resp:
                data = await resp.json()

            if not data.get("success"):
                return self._error_response("⚠️ Дані на сайті тимчасово недоступні")

            intervals = data["data"]["today"]["intervals"]
            return self._process_intervals(intervals, f"{queue}.{subqueue}")

        except Exception as e:
            logger.error(f"Fetch error: {e}")
            return self._error_response("❌ Помилка з'єднання з сервером")

    def _process_intervals(self, intervals: list, group_name: str) -> ScheduleData:
        now = datetime.now()
        now_str = now.strftime("%H:%M")
        
        current_status = LightStatus.UNKNOWN
        current_status_code = ""
        next_change_dt = None
        next_change_type = ""
        
        # 1. Формируем таймлайн (24 символа)
        timeline_list = []
        for i in range(0, len(intervals), 2):
            status = intervals[i]["status"]
            char = "🟦" if status == "on" else "⬛" if status == "off" else "⬜"
            timeline_list.append(char)
        timeline_str = "".join(timeline_list)

        # 2. Определяем текущий статус и ищем ближайшее изменение
        for i, interval in enumerate(intervals):
            start, end = interval["start"], interval["end"]
            if end == "24:00": end = "23:59"

            if start <= now_str <= end:
                current_status_code = interval["status"]
                current_status = {
                    "on": LightStatus.ON, "off": LightStatus.OFF, "maybe": LightStatus.POSSIBLE
                }.get(current_status_code, LightStatus.UNKNOWN)
                
                # Ищем следующее изменение
                for future in intervals[i+1:]:
                    if future["status"] != current_status_code:
                        f_time = future["start"]
                        next_change_type = "Включення 💡" if future["status"] == "on" else "Відключення 🔌"
                        next_change_dt = datetime.strptime(f_time, "%H:%M").replace(
                            year=now.year, month=now.month, day=now.day
                        )
                        break
                break

        # 3. Визуальное оформление
        header = {
            LightStatus.ON: "💎 СВІТЛО Є",
            LightStatus.OFF: "🌑 СВІТЛА НЕМАЄ",
            LightStatus.POSSIBLE: "⚠️ МОЖЛИВЕ ВІДКЛЮЧЕННЯ"
        }.get(current_status, "❓ СТАТУС НЕВИЗНАЧЕНИЙ")

        pointer = " " * (now.hour) + "⬆️"

        msg = f"📊 **Черга {group_name}**\n\n"
        msg += f"**{header}**\n"
        
        if next_change_dt:
            msg += f"🕔 {next_change_type} о **{next_change_dt.strftime('%H:%M')}**\n"
        else:
            msg += "✅ До кінця доби змін не планується\n"

        msg += f"\n**Графік на сьогодні:**\n"
        msg += f"`{timeline_str}`\n"
        msg += f"`{pointer}`\n"
        msg += "`00   06   12   18   24`\n\n"
        msg += "🟦 _є_ | ⬛ _нема_ | ⬜ _можливо_"

        res = ScheduleData(
            status=current_status,
            message=msg,
            timeline=timeline_str,
            next_event_time=next_change_dt,
            next_event_type=next_change_type,
            updated_at=now.strftime("%H:%M")
        )
        self.cache[group_name] = res
        return res

    def _error_response(self, text):
        return ScheduleData(LightStatus.UNKNOWN, text, "", None, "", datetime.now().strftime("%H:%M"))

# --- NOTIFICATION MONITOR ---
class NotificationManager:
    def __init__(self, bot: Bot, provider: EnergyProvider):
        self.bot = bot
        self.provider = provider

    async def start(self):
        while True:
            try:
                await self.check_all_chats()
            except Exception as e:
                logger.error(f"Monitor error: {e}")
            await asyncio.sleep(60)

    async def check_all_chats(self):
        # Чтобы не дергать API для каждого чата, группируем их
        queues: Dict[str, List[int]] = {}
        for cid, cfg in chats_db.items():
            if cfg.notifications_enabled:
                key = f"{cfg.queue}_{cfg.subqueue}"
                queues.setdefault(key, []).append(cid)

        for key, chat_ids in queues.items():
            q, sq = key.split("_")
            data = await self.provider.fetch_schedule(q, sq)
            
            if not data.next_event_time: continue

            now = datetime.now()
            diff = (data.next_event_time - now).total_seconds() / 60

            # Если до события 14-16 минут
            if 14 <= diff <= 16:
                event_id = f"{data.next_event_time.strftime('%H:%M')}_{data.next_event_type}"
                for cid in chat_ids:
                    if chats_db[cid].last_notified_event != event_id:
                        try:
                            await self.bot.send_message(
                                cid, 
                                f"⚠️ **Увага!**\nЧерез 15 хвилин планується **{data.next_event_type}**!\n"
                                f"Час: {data.next_event_time.strftime('%H:%M')}"
                            )
                            chats_db[cid].last_notified_event = event_id
                        except Exception: pass

# --- TELEGRAM HANDLERS ---
class States(StatesGroup):
    queue = State()
    subqueue = State()

dp = Dispatcher(storage=MemoryStorage())
bot = Bot(token=Config.token)
provider = EnergyProvider()
monitor = NotificationManager(bot, provider)

def get_main_kb(cid):
    cfg = chats_db.get(cid, ChatConfig())
    btn_text = "🔔 Викл. сповіщення" if cfg.notifications_enabled else "🔕 Вкл. сповіщення"
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="💡 Статус зараз", callback_data="status_now"))
    builder.row(types.InlineKeyboardButton(text=btn_text, callback_data="toggle_notify"))
    builder.row(types.InlineKeyboardButton(text=f"⚙️ Змінити ({cfg.queue}.{cfg.subqueue})", callback_data="change"))
    return builder.as_markup()

@dp.message(CommandStart())
async def start(m: types.Message, state: FSMContext):
    if m.chat.id not in chats_db: chats_db[m.chat.id] = ChatConfig()
    builder = InlineKeyboardBuilder()
    for i in range(1, 7): builder.add(types.InlineKeyboardButton(text=f"Черга {i}", callback_data=f"q_{i}"))
    builder.adjust(3)
    await m.answer("👋 Виберіть чергу для цього чату:", reply_markup=builder.as_markup())
    await state.set_state(States.queue)

@dp.callback_query(States.queue, F.data.startswith("q_"))
async def set_q(c: types.CallbackQuery, state: FSMContext):
    q = c.data.split("_")[1]
    await state.update_data(q=q)
    builder = InlineKeyboardBuilder()
    for i in range(1, 5): builder.add(types.InlineKeyboardButton(text=f"{q}.{i}", callback_data=f"s_{i}"))
    builder.adjust(2)
    await c.message.edit_text(f"✅ Черга {q}. Виберіть підчергу:", reply_markup=builder.as_markup())
    await state.set_state(States.subqueue)

@dp.callback_query(States.subqueue, F.data.startswith("s_"))
async def set_s(c: types.CallbackQuery, state: FSMContext):
    s = c.data.split("_")[1]
    data = await state.get_data()
    cfg = chats_db[c.message.chat.id]
    cfg.queue, cfg.subqueue = data['q'], s
    await c.message.edit_text(f"🎉 Налаштовано! Черга {cfg.queue}.{s}", reply_markup=get_main_kb(c.message.chat.id))
    await state.clear()

@dp.callback_query(F.data == "status_now")
async def status_now(c: types.CallbackQuery):
    cfg = chats_db.get(c.message.chat.id)
    if not cfg: return await c.answer("Натисніть /start")
    res = await provider.fetch_schedule(cfg.queue, cfg.subqueue)
    try:
        await c.message.edit_text(res.message, reply_markup=get_main_kb(c.message.chat.id), parse_mode="Markdown")
    except: await c.answer()

@dp.callback_query(F.data == "toggle_notify")
async def toggle(c: types.CallbackQuery):
    cfg = chats_db[c.message.chat.id]
    cfg.notifications_enabled = not cfg.notifications_enabled
    await c.answer(f"Сповіщення {'увімкнено' if cfg.notifications_enabled else 'вимкнено'}")
    await c.message.edit_reply_markup(reply_markup=get_main_kb(c.message.chat.id))

@dp.callback_query(F.data == "change")
async def change(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await start(c.message, state)

async def main():
    logger.add(sys.stderr, format="{time} {message}", level="INFO")
    asyncio.create_task(monitor.start())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

# --- BACKGROUND MONITOR ---
class NotificationManager:
    def __init__(self, bot: Bot, provider: EnergyProvider):
        self.bot = bot
        self.provider = provider

    async def start(self):
        logger.info("Starting background monitor...")
        while True:
            await self.check_all_chats()
            await asyncio.sleep(60) # Проверка каждую минуту

    async def check_all_chats(self):
        # Группируем чаты по очередям, чтобы не долбить API лишний раз
        # queue_key -> [list of chat_ids]
        subscriptions: Dict[str, List[int]] = {}
        
        for chat_id, config in chats_db.items():
            if config.notifications_enabled:
                key = f"{config.queue}|{config.subqueue}"
                if key not in subscriptions:
                    subscriptions[key] = []
                subscriptions[key].append(chat_id)

        # Проверяем каждую уникальную очередь
        for key, chat_ids in subscriptions.items():
            q, sq = key.split("|")
            data = await self.provider.fetch_schedule(q, sq)
            
            if not data.next_event_time:
                continue

            # Логика времени
            now = datetime.now()
            diff = data.next_event_time - now
            minutes_left = diff.total_seconds() / 60

            # Условие: от 14 до 16 минут (попадаем в окно 15 минут)
            if 14 <= minutes_left <= 16:
                event_uid = f"{data.next_event_time.strftime('%H:%M')}_{data.next_event_type}"
                
                for chat_id in chat_ids:
                    config = chats_db[chat_id]
                    # Если мы еще не оповещали об ЭТОМ событии
                    if config.last_notified_event != event_uid:
                        try:
                            await self.bot.send_message(
                                chat_id,
                                f"⚠️ **Увага!**\nЧерез 15 хвилин планується **{data.next_event_type}**!\nЧас: {data.next_event_time.strftime('%H:%M')}"
                            )
                            config.last_notified_event = event_uid
                            logger.info(f"Notification sent to {chat_id}")
                        except Exception as e:
                            logger.error(f"Failed to send to {chat_id}: {e}")
                            # Если бот кикнут, отключаем уведомления
                            if "Forbidden" in str(e):
                                config.notifications_enabled = False

# --- HANDLERS ---
class UserSettings(StatesGroup):
    choosing_queue = State()
    choosing_subqueue = State()

provider = EnergyProvider()
dp = Dispatcher(storage=MemoryStorage())
bot = Bot(token=Config.token)
monitor = NotificationManager(bot, provider)

# Клавиатуры
def get_main_kb(chat_id: int):
    config = chats_db.get(chat_id, ChatConfig())
    full_group = f"{config.queue}.{config.subqueue}"
    
    # Кнопка колокольчика меняется динамически
    bell = "🔕 Вкл. сповіщення" if not config.notifications_enabled else "🔔 Викл. сповіщення"
    
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="💡 Статус зараз", callback_data="status_now"))
    builder.row(types.InlineKeyboardButton(text=f"{bell}", callback_data="toggle_notify"))
    builder.row(types.InlineKeyboardButton(text=f"⚙️ Змінити ({full_group})", callback_data="change_settings"))
    return builder.as_markup()

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

# --- HANDLER LOGIC ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    # Регистрируем чат в базе при старте
    if message.chat.id not in chats_db:
        chats_db[message.chat.id] = ChatConfig()
    
    await message.answer(
        "👋 Привіт! Я бот для моніторингу світла.\n"
        "Оскільки я працюю в групі, налаштування спільні для всіх.\n\n"
        "Оберіть чергу для цього чату:",
        reply_markup=generate_queue_kb()
    )
    await state.set_state(UserSettings.choosing_queue)

@dp.callback_query(UserSettings.choosing_queue, F.data.startswith("queue_"))
async def process_queue(callback: types.CallbackQuery, state: FSMContext):
    q = callback.data.split("_")[1]
    await state.update_data(queue=q)
    await callback.message.edit_text(f"✅ Черга {q}. Обери підчергу:", reply_markup=generate_subqueue_kb(q))
    await state.set_state(UserSettings.choosing_subqueue)

@dp.callback_query(UserSettings.choosing_subqueue, F.data.startswith("sub_"))
async def process_subqueue(callback: types.CallbackQuery, state: FSMContext):
    sub = callback.data.split("_")[1]
    data = await state.get_data()
    q = data.get("queue")
    
    # Сохраняем в "Базу"
    chat_id = callback.message.chat.id
    if chat_id not in chats_db: chats_db[chat_id] = ChatConfig()
    
    chats_db[chat_id].queue = q
    chats_db[chat_id].subqueue = sub
    
    full = f"{q}.{sub}"
    await callback.message.edit_text(
        f"✅ Група для цього чату: **{full}**\nМеню:",
        reply_markup=get_main_kb(chat_id),
        parse_mode="Markdown"
    )
    await state.clear()

@dp.callback_query(F.data == "toggle_notify")
async def toggle_notify(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    if chat_id not in chats_db:
        # Если настройки слетели (перезапуск бота), просим настроить заново
        await callback.answer("Спочатку налаштуйте чергу /start", show_alert=True)
        return

    # Переключаем
    cfg = chats_db[chat_id]
    cfg.notifications_enabled = not cfg.notifications_enabled
    
    status = "✅ Включені" if cfg.notifications_enabled else "❌ Виключені"
    await callback.answer(f"Сповіщення {status}")
    
    # Обновляем клавиатуру
    try:
        await callback.message.edit_reply_markup(reply_markup=get_main_kb(chat_id))
    except: pass

@dp.callback_query(F.data == "status_now")
async def check_status(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    cfg = chats_db.get(chat_id)
    if not cfg:
        await callback.answer("Натисніть /start", show_alert=True)
        return

    # await bot.send_chat_action(chat_id, action="typing") # Иногда вызывает ошибки в группах, если нет прав
    info = await provider.fetch_schedule(cfg.queue, cfg.subqueue)
    
    full_group = f"{cfg.queue}.{cfg.subqueue}"
    text = f"📊 **Група {full_group}**\n\n{info.message}"
    
    try:
        await callback.message.edit_text(
            text, 
            reply_markup=get_main_kb(chat_id),
            parse_mode="Markdown"
        )
    except Exception:
        await callback.answer()

@dp.callback_query(F.data == "change_settings")
async def change(callback: types.CallbackQuery, state: FSMContext):
    await cmd_start(callback.message, state)

# --- STARTUP ---
async def main():
    logger.add(sys.stderr, format="{time} {level} {message}", level="INFO")
    logger.info("Bot starting...")
    
    # Запускаем фоновую задачу
    asyncio.create_task(monitor.start())
    
    try:
        await dp.start_polling(bot)
    finally:
        await provider.session.close()

if __name__ == "__main__":
    if not Config.token:
        logger.error("No token")
        sys.exit(1)
    asyncio.run(main())
