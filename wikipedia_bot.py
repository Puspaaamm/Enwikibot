import asyncio
import logging
import os
from threading import Thread
from typing import Dict, List, Optional, Tuple

import requests
from flask import Flask
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Render port binding ke liye Flask Server
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# Logging Setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Environment variable se Token fetch karein
BOT_TOKEN = os.environ.get("BOT_TOKEN")

WIKI_API_URL = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "TelegramWikipediaBot/1.0 (personal project)"}

MAX_MESSAGE_LENGTH = 3800  # Telegram limit margin
active_tasks: Dict[int, asyncio.Task] = {}


def search_title(query: str) -> Optional[str]:
    """Search best matching article title."""
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": 1,
        "format": "json",
    }
    resp = requests.get(WIKI_API_URL, params=params, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    results = resp.json().get("query", {}).get("search", [])
    return results[0]["title"] if results else None


def fetch_article_data(title: str) -> Tuple[Optional[str], List[Tuple[str, str]]]:
    """Fetch text extract and all images (with titles/captions) from pageimages API."""
    params = {
        "action": "query",
        "prop": "extracts|images",
        "explaintext": 1,
        "redirects": 1,
        "titles": title,
        "format": "json",
        "imlimit": 10  # Max 10 images to avoid spamming
    }
    resp = requests.get(WIKI_API_URL, params=params, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    pages = resp.json().get("query", {}).get("pages", {})

    extract = None
    image_files = []

    for page_id, page in pages.items():
        if page_id == "-1":
            return None, []
        extract = page.get("extract") or None
        images = page.get("images", [])
        
        # Valid image formats filter karein (SVG diagrams, JPG, PNG)
        for img in images:
            img_name = img.get("title", "")
            if any(img_name.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".svg"]):
                # Skip common icons/logos
                if not any(skip in img_name.lower() for skip in ["wiki", "icon", "symbol", "logo", "padlock"]):
                    image_files.append(img_name)

    # Fetch direct image URLs for found images
    image_urls_with_captions = []
    if image_files:
        img_params = {
            "action": "query",
            "titles": "|".join(image_files[:8]),  # Top 8 images
            "prop": "imageinfo",
            "iiprop": "url",
            "format": "json"
        }
        img_resp = requests.get(WIKI_API_URL, params=img_params, headers=HEADERS, timeout=15)
        if img_resp.status_code == 200:
            img_pages = img_resp.json().get("query", {}).get("pages", {})
            for p_id, p_data in img_pages.items():
                img_info = p_data.get("imageinfo", [])
                if img_info:
                    url = img_info[0].get("url")
                    caption = p_data.get("title", "").replace("File:", "").replace(".jpg", "").replace(".png", "")
                    if url and not url.endswith(".svg"):  # Telegram SVG support nahi karta direct photo me
                        image_urls_with_captions.append((url, caption))

    return extract, image_urls_with_captions


async def fetch_article(query: str) -> Optional[dict]:
    title = await asyncio.to_thread(search_title, query)
    if not title:
        return None
    extract, images = await asyncio.to_thread(fetch_article_data, title)
    return {"title": title, "extract": extract, "images": images}


def split_text_into_chunks(text: str, max_length: int = MAX_MESSAGE_LENGTH):
    chunks = []
    while len(text) > max_length:
        split_at = text.rfind('\n', 0, max_length)
        if split_at == -1:
            split_at = text.rfind(' ', 0, max_length)
        if split_at == -1:
            split_at = max_length

        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()

    if text:
        chunks.append(text)
    return chunks


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hi! Send me any topic and I'll fetch its English Wikipedia article text along with diagrams & images with captions!"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = (update.message.text or "").strip()
    if not query:
        return

    chat_id = update.effective_chat.id
    previous = active_tasks.get(chat_id)
    if previous and not previous.done():
        previous.cancel()

    active_tasks[chat_id] = asyncio.create_task(process_search(update, query))


async def process_search(update: Update, query: str) -> None:
    try:
        await update.effective_chat.send_action("typing")
        result = await fetch_article(query)
        if result is None:
            await update.message.reply_text(f'No Wikipedia article found for "{query}".')
            return

        title, extract, images = result["title"], result["extract"], result["images"]
        if not extract:
            await update.message.reply_text(f'Found "{title}" but could not fetch content.')
            return

        url = "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")

        await update.message.reply_text(f"📖 *{title}*", parse_mode="Markdown")

        # 1. Full Article Text Send Karein
        chunks = split_text_into_chunks(extract)
        for chunk in chunks:
            await update.message.reply_text(chunk)
            await asyncio.sleep(0.4)

        # 2. Article ki Images aur Diagrams With Caption Send Karein
        if images:
            await update.message.reply_text(" *Available Images & Diagrams from article:*", parse_mode="Markdown")
            for img_url, caption in images:
                try:
                    await update.message.reply_photo(photo=img_url, caption=f" {caption}")
                    await asyncio.sleep(0.5)
                except Exception:
                    continue

        # 3. Web Link
        await update.message.reply_text(f"🔗 Full original article web link: {url}")

    except asyncio.CancelledError:
        raise
    except Exception:
        await update.message.reply_text("Something went wrong. Please try again.")


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing!")

    app_bot = Application.builder().token(BOT_TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot is running...")
    app_bot.run_polling()


if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    main()

