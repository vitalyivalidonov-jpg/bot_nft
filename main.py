import asyncio
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv

load_dotenv()

# --- 1. Веб-сервер для Render (держит порт) ---
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running 24/7!")
    
    def log_message(self, format, *args):
        return

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    print(f"🌐 Веб-сервер запущен на порту {port}")
    server.serve_forever()

# --- 2. Запуск твоего бота ---
async def start_bot():
    print("🤖 Запускаем логику бота...")
    # Сюда подключай своего бота (например, импорт твоего диспетчера и запуск polling)
    # Например: from bot_file import dp, bot
    # await dp.start_polling(bot)

if __name__ == "__main__":
    # Запускаем веб-сервер в фоновом потоке
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    # Запускаем бота в главном потоке
    asyncio.run(start_bot())
