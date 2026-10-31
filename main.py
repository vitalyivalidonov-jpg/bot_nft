import os
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv

load_dotenv()

# --- 1. Мини-веб-сервер для Render ---
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
    print(f"🌐 Веб-сервер успешно запущен на порту {port}")
    server.serve_forever()

# --- 2. Основная логика ---
async def main():
    print("🚀 Бот запущен и работает!")
    # Здесь держим бесконечный цикл, чтобы приложение не выключалось
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    # Запускаем веб-сервер в отдельном потоке
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    # Запускаем бесконечный асинхронный цикл в главном потоке
    asyncio.run(main())
