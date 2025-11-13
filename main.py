import os
import json
import re
import unicodedata
import datetime
import statistics
from flask import Flask, request

import gspread
from google.oauth2.service_account import Credentials

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
import asyncio

# ==========================
# --- CONFIGURATION BOT ---
# ==========================
TOKEN = os.environ.get("TOKEN")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", 0))
SERVICE_URL = os.environ.get("SERVICE_URL")  # ex: yourservice.onrender.com

# ==========================
# --- GOOGLE SHEETS CONFIG ---
# ==========================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

creds_info = json.loads(os.environ['GOOGLE_APPLICATION_CREDENTIALS_JSON'])
creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
client = gspread.authorize(creds)
sheet = client.open("CineChocs_Notes").worksheet("Notes")

# ==========================
# --- VARIABLES GLOBALES ---
# ==========================
films = {}
concours_en_cours = False
film_concours = None
phrase_concours = ""
gagnants = []
selection_en_cours = False
top3_films = []

# ==========================
# --- FONCTIONS UTILES ---
# ==========================
def normalize(text: str) -> str:
    if not isinstance(text, str):
        return ""
    s = text.strip()
    s = re.sub(r"^[<\[\(\"']+|[>\]\)\"']+$", "", s)
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^0-9A-Za-zÀ-ÖØ-öø-ÿ\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()

def archiver_films(films_a_archiver):
    archive_sheet = client.open("CineChocs_Notes").worksheet("Archives")
    all_rows = sheet.get_all_records()
    rows_to_keep = []
    for row in all_rows:
        if row['Film'] in films_a_archiver:
            archive_sheet.append_row([row['Date'], row['Film'], row['Note'], row['Utilisateur'], row['ID_Telegram']])
        else:
            rows_to_keep.append(row)
    sheet.clear()
    sheet.append_row(["Date", "Film", "Note", "Utilisateur", "ID_Telegram"])
    for r in rows_to_keep:
        sheet.append_row([r['Date'], r['Film'], r['Note'], r['Utilisateur'], r['ID_Telegram']])

async def get_top3():
    if not films:
        return []
    film_moyennes = []
    for f, notes in films.items():
        if notes:
            film_moyennes.append({"Film": f, "Note": round(statistics.mean([v['note'] for v in notes]),1)})
    film_moyennes.sort(key=lambda x: x['Note'], reverse=True)
    return film_moyennes[:3]

# ==========================
# --- COMMANDES BOT ---
# ==========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Bienvenue sur *CinéChocs Challenge Bot !*\n\n"
        "🎬 Tu pourras voter pour des films et participer aux concours mensuels.\n\n"
        "👉 Reste connecté pour le prochain challenge !",
        parse_mode="Markdown"
    )

async def postfilm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) == 0:
        await context.bot.send_message(chat_id=CHANNEL_ID, text="⚠️ Utilise : /postfilm <nom du film>")
        return
    film_name = " ".join(context.args)
    films.setdefault(film_name, [])
    keyboard = [[
        InlineKeyboardButton(f"⭐{i}", callback_data=f"rate|{film_name}|{i}") for i in range(1,6)
    ]]
    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=f"🎬 *{film_name}*\nDonne ta note sur 5 étoiles 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def rate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    await query.answer()
    _, film, note = query.data.split("|")
    note = int(note)
    films.setdefault(film, [])
    if any(v['user_id'] == user.id for v in films[film]):
        await query.answer("❌ Tu as déjà voté pour ce film !", show_alert=True)
        return
    films[film].append({"user_id": user.id, "note": note})
    notes = [v['note'] for v in films[film]]
    avg = statistics.mean(notes)
    votes = len(notes)
    sheet.append_row([datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), film, note, user.full_name, user.id])
    keyboard = [[InlineKeyboardButton(f"⭐{i}", callback_data=f"rate|{film}|{i}") for i in range(1,6)]]
    await query.edit_message_text(
        f"🎬 *{film}*\n⭐ Moyenne : {avg:.1f}/5 ({votes} votes)",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def classement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top3 = await get_top3()
    if not top3:
        await context.bot.send_message(chat_id=CHANNEL_ID, text="📊 Aucun film noté pour le moment.")
        return
    classement_text = "🏆 *Classement actuel des films :*\n\n"
    for i, film in enumerate(top3, 1):
        classement_text += f"{i}. {film['Film']} — ⭐{film['Note']}\n"
    await context.bot.send_message(chat_id=CHANNEL_ID, text=classement_text, parse_mode="Markdown")


# ==========================
# --- CONCOURS DU MOIS ---
# ==========================
async def start_concours(update: Update, context: CallbackContext):
    global selection_en_cours, top3_films, gagnants, concours_en_cours
    top3_films = await get_top3()
    if not top3_films:
        await context.bot.send_message(chat_id=CHANNEL_ID, text="📊 Aucun film pour lancer le concours.")
        return

    classement_text = "🏆 Sélection des 3 meilleurs films pour le concours 🏆\n\n"
    for i, film in enumerate(top3_films, 1):
        classement_text += f"{i}. {film['Film']} — ⭐{film['Note']}\n"
    classement_text += "\n✅ Réponds avec le numéro du film choisi."
    await update.message.reply_text(classement_text, parse_mode="Markdown")

    selection_en_cours = True
    gagnants = []
    concours_en_cours = False

# --- CHOIX DU FILM ---
async def choose_film(update: Update, context: CallbackContext):
    global film_concours, selection_en_cours
    try:
        choix = int(update.message.text.strip())
        if 1 <= choix <= len(top3_films):
            film_concours = top3_films[choix-1]['Film']
            await update.message.reply_text(
                f"✅ Film choisi : {film_concours}\n"
                f"Maintenant, envoie la phrase du concours avec /phrase <texte>"
            )
            selection_en_cours = False
        else:
            await update.message.reply_text("⚠️ Choix invalide, entre un nombre entre 1 et 3.")
    except ValueError:
        await update.message.reply_text("⚠️ Entre le numéro correspondant au film choisi.")

# --- PHRASE DU CONCOURS ---
async def set_phrase(update: Update, context: CallbackContext):
    global phrase_concours, concours_en_cours
    if len(context.args) == 0:
        await update.message.reply_text("⚠️ Utilisation : /phrase <texte de la phrase>")
        return
    phrase_concours = " ".join(context.args)
    concours_en_cours = True

    BOT_USERNAME = "CinechocsChallengeBot"
    keyboard = [[InlineKeyboardButton("💬 Répondre au bot", url=f"https://t.me/{BOT_USERNAME}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=(
            f"🎬 **CHALLENGE CINECHOCS DU MOIS !** 🎬\n\n"
            f"Essaie de trouver le film correspondant à cette phrase :\n\n"
            f"🗣️ _« {phrase_concours} »_\n\n"
            f"Les 2 premiers à répondre correctement en **privé** au bot remportent un dépôt Mobile Money 💸 !"
        ),
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

# --- RÉPONSES PARTICIPANTS ---
async def handle_response_private(update: Update, context: CallbackContext):
    global concours_en_cours, film_concours, gagnants

    if not concours_en_cours:
        await update.message.reply_text("❌ Aucun concours en cours pour le moment.")
        return

    user = update.message.from_user
    raw_reponse = update.message.text or ""
    reponse = raw_reponse.strip()

    if user.id in [g['id'] for g in gagnants]:
        await update.message.reply_text("⚠️ Tu as déjà répondu au concours. Ta réponse est finale !")
        return

    normalized_response = normalize(reponse)
    normalized_answer = normalize(film_concours or "")

    gagnants.append({'id': user.id, 'username': user.username, 'reponse': reponse})

    is_correct = (
        normalized_response == normalized_answer
        or (normalized_answer and normalized_answer in normalized_response)
    )

    if is_correct:
        correct_count = len([g for g in gagnants if normalize(g['reponse']) == normalized_answer])
        if correct_count == 1:
            await update.message.reply_text("🎉 Bravo ! Tu es le 1er gagnant 🥇")
        elif correct_count == 2:
            await update.message.reply_text("🎉 Bravo ! Tu es le 2ᵉ gagnant 🥈")
            concours_en_cours = False
            date_next = (datetime.datetime.now() + datetime.timedelta(days=30)).strftime("%d %B %Y")

            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=(
                    f"🏁 Le challenge est terminé !\n\n"
                    f"🥇 @{gagnants[0]['username']}\n"
                    f"🥈 @{gagnants[1]['username']}\n\n"
                    f"🎥 Film mystère : *{film_concours}*\n"
                    f"💬 Phrase : _« {phrase_concours} »_\n\n"
                    f"📅 Prochain concours : {date_next}\n"
                    f"Merci à tous ! 🙌"
                ),
                parse_mode="Markdown"
            )

            # Archiver les films utilisés pour ce challenge
            archiver_films(list(films.keys()))
            films.clear()
    else:
        await update.message.reply_text("❌ Mauvaise réponse, mais bien essayé !")

# --- ANNULATION DU CONCOURS ---
async def cancel_concours(update: Update, context: CallbackContext):
    global concours_en_cours
    concours_en_cours = False
    await context.bot.send_message(chat_id=CHANNEL_ID, text="❌ Le concours en cours a été annulé.")

# ==========================
# --- LANCEMENT DU BOT ---
# ==========================
app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("postfilm", postfilm))
app.add_handler(CommandHandler("classement", classement))
app.add_handler(CallbackQueryHandler(rate_callback, pattern="^rate"))


# Commandes
app.add_handler(CommandHandler("concours", start_concours))
app.add_handler(CommandHandler("phrase", set_phrase))
app.add_handler(CommandHandler("cancel", cancel_concours))

# Route dynamique
async def route_message(update: Update, context: CallbackContext):
    global selection_en_cours, concours_en_cours
    if selection_en_cours and update.message.text.isdigit():
        await choose_film(update, context)
        return
    if concours_en_cours:
        await handle_response_private(update, context)
        return
    await update.message.reply_text("ℹ️ Aucune action en cours actuellement.")

app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_message))

print("✅ CinéChocs Bot connecté à Google Sheets et prêt à fonctionner !")


# Flask pour webhook
flask_app = Flask(__name__)

@flask_app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, application.bot)
    asyncio.get_event_loop().create_task(application.update_queue.put(update))
    return "OK", 200

@flask_app.route("/")
def index():
    return "Bot Telegram en ligne !", 200

if __name__ == "__main__":
    if SERVICE_URL:
        application.bot.set_webhook(f"https://{SERVICE_URL}/{TOKEN}")
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

