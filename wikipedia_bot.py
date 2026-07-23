"""
Telegram bot that searches English Wikipedia and sends back an image + summary.

Bot Username:@Enwikibot
"""

import asyncio
import logging
import os
from typing import Dict, Optional, Tuple

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Aapka Telegram Bot Token set kar diya gaya hai
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8820491224:AAEqQ9Mzpc4fpRo7LpMyIw3zU-UZ6HQYkI4")

WIKI_API_URL = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "TelegramWikipediaBot/1.0 (personal project)"}

MAX_WORDS = 500             # Max words to display
MAX_MESSAGE_LENGTH = 4000   # Telegram character margin limit
THUMBNAIL_WIDTH = 600       # Image resolution limit

# Active tasks per chat to allow task cancellation for fast input
active_tasks: Dict[int, asyncio.Task] = {}


def search_title(query: str) -> Optional[str]:
    """Return the title of the best-matching English Wikipedia article."""
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


def fetch_extract_and_image(title: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (full plain-text extract, lead-image URL) for a Wikipedia page."""
    params = {
        "action": "query",
        "prop": "extracts|pageimages",
        "explaintext": 1,
        "redirects": 1,
        "pithumbsize": THUMBNAIL_WIDTH,
        "titles": title,
        "format": "json",
    }
    resp = requests.get(WIKI_API_URL, params=params, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    pages = resp.json().get("query", {}).get("pages", {})
    for page_id, page in pages.items():
        if page_id == "-1":
            return None, None
        extract = page.get("extract") or None
        thumbnail = page.get("thumbnail", {}).get("source")
        return extract, thumbnail
    return None, None


async def fetch_article(query: str) -> Optional[dict]:
    """Search & fetch article off-main-thread using asyncio.to_thread."""
    title = await asyncio.to_thread(search_title, query)
    if not title:
        return None
    extract, thumbnail = await asyncio.to_thread(fetch_extract_and_image, title)
    return {"title": title, "extract": extract, "thumbnail": thumbnail}


def truncate_to_words(text: str, max_words: int) -> Tuple[str, bool]:
    """Truncate text to specified word count."""
    words = text.split()
    if len(words) <= max_words:
        return text.strip(), False
    return " ".join(words[:max_words]) + " …", True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hi! Send me any topic and I'll fetch its English Wikipedia image + "
        "a 500-word summary -- e.g. 'Mahatma Gandhi' or 'Black holes'."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancels any previous task and starts a fresh search request."""
    query = (update.message.text or "").strip()
    if not query:
        return

    chat_id = update.effective_chat.id
    previous = active_tasks.get(chat_id)
    if previous and not previous.done():
        previous.cancel()
        logger.info("Chat %s: cancelled previous search for a newer query", chat_id)

    active_tasks[chat_id] = asyncio.create_task(process_search(update, query))


async def process_search(update: Update, query: str) -> None:
    try:
        await update.effective_chat.send_action("typing")

        result = await fetch_article(query)
        if result is None:
            await update.message.reply_text(f'No Wikipedia article found for "{query}".')
            return

        title, extract, thumbnail = result["title"], result["extract"], result["thumbnail"]
        if not extract:
            await update.message.reply_text(f'Found "{title}" but could not fetch its content.')
            return

        url = "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")
        summary, was_truncated = truncate_to_words(extract, MAX_WORDS)
        if len(summary) > MAX_MESSAGE_LENGTH:
            summary = summary[:MAX_MESSAGE_LENGTH] + " …"

        if thumbnail:
            try:
                await update.message.reply_photo(photo=thumbnail, caption=f"📖 {title}")
            except Exception:
                logger.warning("Couldn't send image for '%s', falling back to text", title)
                await update.message.reply_text(f"📖 {title}")
        else:
            await update.message.reply_text(f"📖 {title}")

        await update.message.reply_text(summary)

        note = "\n\n(Showing the first 500 words.)" if was_truncated else ""
        await update.message.reply_text(f"🔗 Full article: {url}{note}")

    except asyncio.CancelledError:
        logger.info("Search for '%s' cancelled -- a newer search took over", query)
        raise
    except requests.RequestException:
        logger.exception("Wikipedia API error")
        await update.message.reply_text("Couldn't reach Wikipedia right now -- try again shortly.")
    except Exception:
        logger.exception("Unexpected error handling message")
        await update.message.reply_text("Something went wrong. Please try again.")


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot is running... Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()

