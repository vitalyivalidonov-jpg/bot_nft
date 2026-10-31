import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# 1. Простой веб-сервер, чтобы Render видел открытый порт и не ругался
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running 24/7!")

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    print(f"🌐 Веб-сервер запущен на порту {port}")
    server.serve_forever()

if __name__ == "__main__":
    # Запускаем веб-сервер в отдельном потоке, чтобы он не блокировал бота
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    # Сюда вставляй запуск твоего бота (или импортируй из другого файла)
    print("🤖 Бот запущен!")
    # Пример:
    # from aiogram import executor
    # executor.start_polling(dp)
