import os
import logging
from datetime import datetime, timedelta
import dateparser
import aiohttp
from bs4 import BeautifulSoup
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from ics import Calendar, Event
from dotenv import load_dotenv
from urllib.parse import urljoin, urlparse, parse_qs
import io
from fpdf import FPDF  # Importamos FPDF

# Cargar variables de entorno desde .env
load_dotenv()

# Configuración del logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Obtén el token del bot desde una variable de entorno
bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
if not bot_token:
    logger.error("El token del bot no está configurado. Establece la variable de entorno TELEGRAM_BOT_TOKEN en el archivo .env.")
    exit(1)

# URL de la página que quieres scrapear
url = 'https://sistemas.undc.edu.pe/bienesyservicios/'

# Variables globales
subscribers = set()
previous_publications = set()
data_cache = {}  # Añadimos la caché global

# Función para hacer el scraping y obtener los datos
async def scrape_page():
    global data_cache
    data_cache = {}  # Reinicia la caché cada vez que scrapeas
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as response:
                text = await response.text()
        except Exception as e:
            logger.error(f"Error al obtener la página: {e}")
            return []

    soup = BeautifulSoup(text, 'html.parser')

    # Buscar la tabla en la página
    table = soup.find('table', id='datatable_publicaciones')  # Encuentra la tabla por su id
    if not table:
        logger.error("No se encontró la tabla de publicaciones.")
        return []

    tbody = table.find('tbody')
    if not tbody:
        logger.error("No se encontró el cuerpo de la tabla de publicaciones.")
        return []

    rows = tbody.find_all('tr')  # Encuentra todas las filas de la tabla dentro del tbody

    data = []
    for row in rows:
        cols = row.find_all('td')
        if len(cols) < 6:
            continue  # Saltar filas que no tengan suficientes columnas

        cols_text = [ele.text.strip() for ele in cols]

        # Obtener el enlace al PDF
        pdf_link = "No disponible"
        pdf_link_tag = cols[2].find('a', href=True)
        if pdf_link_tag:
            pdf_link = pdf_link_tag['href']
            # Asegurarnos de que el enlace es absoluto
            if not pdf_link.startswith('http'):
                pdf_link = urljoin(url, pdf_link)

        # Agregar el enlace al PDF al final de los datos de la fila
        cols_text.append(pdf_link)
        data.append(cols_text)

        # Almacenar en la caché
        pub_id = cols_text[0]
        data_cache[pub_id] = {
            'row': cols_text,
            'pdf_url': pdf_link,
            'description': cols_text[1],
            'published_date': cols_text[3],
            'expires_date': cols_text[4],
            'status': cols_text[5],
        }

    return data

# Función para filtrar los elementos vigentes
def filter_vigente(data):
    vigente_data = [row for row in data if "Vigente" in row[5]]  # Filtra solo las filas con estado 'Vigente'
    return vigente_data

# Función para formatear una publicación individual
def format_single_publication(row):
    pdf_url = row[6] if row[6] != "No disponible" else "No disponible"
    published_date = dateparser.parse(row[3], settings={'DATE_ORDER': 'DMY'})
    expires_date = dateparser.parse(row[4], settings={'DATE_ORDER': 'DMY'})
    if expires_date:
        delta = expires_date - datetime.now()
        if delta.total_seconds() < 0:
            delta = timedelta(0)
        days = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        available_time = f"{days} días, {hours} horas, {minutes} minutos"
    else:
        available_time = "Desconocido"

    message = (
        f"📢 <b>Publicación #{row[0]}</b>\n"
        f"📝 <b>Descripción:</b>\n{row[1]}\n\n"
    )
    if pdf_url != "No disponible":
        message += "📄 <b>PDF:</b> Disponible para descargar\n"
    else:
        message += "📄 <b>PDF:</b> No disponible\n"
    message += (
        f"📅 <b>Publicado:</b> {row[3]}\n"
        f"⏳ <b>Vence:</b> {row[4]}\n"
        f"⏱ <b>Tiempo disponible:</b> {available_time}\n"
        f"🗑 <b>Estado:</b> {row[5]}\n"

    )

    # Botones interactivos
    buttons = []
    if pdf_url != "No disponible":
        buttons.append(InlineKeyboardButton("PDF Original", callback_data=f"download_{row[0]}"))
    buttons.append(InlineKeyboardButton("PDF de la Publicación", callback_data=f"sharepdf_{row[0]}"))
    buttons.append(InlineKeyboardButton("Agregar al Calendario", callback_data=f"calendar_{row[0]}"))
    reply_markup = InlineKeyboardMarkup([buttons])
    return message, reply_markup

# Handlers de comandos y mensajes

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envía un mensaje de bienvenida cuando se utiliza /start"""
    await update.message.reply_text(
        '¡Hola! Soy tu bot de notificaciones. '
        'Usa /vigentes para ver los elementos vigentes.\n'
        'Usa /subscribe para suscribirte a notificaciones automáticas.\n'
        'Usa /unsubscribe para darte de baja de las notificaciones.'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envía un mensaje de ayuda"""
    help_text = (
        "Comandos disponibles:\n"
        "/start - Mensaje de bienvenida\n"
        "/vigentes - Mostrar elementos vigentes\n"
        "/subscribe - Suscribirse a notificaciones automáticas\n"
        "/unsubscribe - Darse de baja de las notificaciones\n"
        "/help - Mostrar este mensaje de ayuda"
    )
    await update.message.reply_text(help_text)

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responde con el mismo mensaje que el usuario envía"""
    await update.message.reply_text(update.message.text)

async def vigentes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envía los elementos vigentes al chat"""
    current_data = await scrape_page()
    vigente_data = filter_vigente(current_data)
    if not vigente_data:
        await update.message.reply_text("No hay elementos vigentes en este momento.")
        return

    for row in vigente_data:
        message, reply_markup = format_single_publication(row)
        await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='HTML')

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Suscribe al usuario a las notificaciones automáticas"""
    user_id = update.effective_user.id
    subscribers.add(user_id)
    await update.message.reply_text("Te has suscrito a las notificaciones de nuevas publicaciones.")

async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Darse de baja de las notificaciones automáticas"""
    user_id = update.effective_user.id
    subscribers.discard(user_id)
    await update.message.reply_text("Te has dado de baja de las notificaciones.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja las acciones de los botones interactivos"""
    query = update.callback_query
    await query.answer()
    data = query.data
    pub_id = None
    if data.startswith("download_"):
        pub_id = data.replace("download_", "")
        pub_data = data_cache.get(pub_id)
        if pub_data and pub_data['pdf_url'] != "No disponible":
            pdf_url = pub_data['pdf_url']
            # Verificar si es un enlace de Google Drive y convertirlo
            if 'drive.google.com' in pdf_url:
                pdf_url = convert_drive_url(pdf_url)
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.get(pdf_url) as resp:
                        if resp.status == 200:
                            pdf_data = await resp.read()
                            await context.bot.send_document(
                                chat_id=update.effective_chat.id,
                                document=pdf_data,
                                filename=f"{pub_id}.pdf"
                            )
                        else:
                            await update.effective_message.reply_text("No se pudo descargar el PDF.")
                except Exception as e:
                    logger.error(f"Error al descargar el PDF: {e}")
                    await update.effective_message.reply_text("Ocurrió un error al descargar el PDF.")
        else:
            await update.effective_message.reply_text("El PDF no está disponible.")
    elif data.startswith("calendar_"):
        pub_id = data.replace("calendar_", "")
        pub_data = data_cache.get(pub_id)
        if pub_data:
            try:
                # Ajustar el formato de acuerdo con las fechas recibidas
                published_date = datetime.strptime(pub_data['published_date'], '%Y-%m-%d %H:%M:%S')
                expires_date = datetime.strptime(pub_data['expires_date'], '%Y-%m-%d %H:%M:%S')
            except ValueError as e:
                logger.error(f"Error al parsear las fechas: {e}")
                await update.effective_message.reply_text("Error al procesar las fechas de la publicación.")
                return


            description = pub_data['description']
            c = Calendar()
            e = Event()
            e.name = f"Publicación #{pub_id} - {description[:30]}..."
            e.begin = published_date  # Asignar objeto datetime directamente
            e.end = expires_date  # Asignar objeto datetime directamente
            e.description = description
            c.events.add(e)

            # Usar serialize para obtener el contenido del calendario
            ics_content = c.serialize()
            ics_file = io.BytesIO(ics_content.encode('utf-8'))
            ics_file.name = f"{pub_id}.ics"
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=ics_file,
                filename=ics_file.name
            )
            ics_file.close()
        else:
            await update.effective_message.reply_text("No se pudo encontrar los datos de la publicación.")

    
    elif data.startswith("sharepdf_"):
        pub_id = data.replace("sharepdf_", "")
        pub_data = data_cache.get(pub_id)
        if pub_data:
            # Generar el PDF
            pdf = FPDF()
            pdf.add_page()
            pdf.set_font("Arial", 'B', 16)
            pdf.cell(0, 10, f"Publicación #{pub_id}", ln=True)
            pdf.set_font("Arial", '', 12)
            pdf.multi_cell(0, 10, f"Descripción:\n{pub_data['description']}")
            pdf.ln(5)
            pdf.cell(0, 10, f"Publicado: {pub_data['published_date']}", ln=True)
            pdf.cell(0, 10, f"Vence: {pub_data['expires_date']}", ln=True)
            pdf.cell(0, 10, f"Estado: {pub_data['status']}", ln=True)

            # Obtener el contenido del PDF en bytes
            pdf_output = pdf.output(dest='S').encode('latin-1')

            # Enviar el PDF al usuario
            with io.BytesIO(pdf_output) as pdf_file:
                pdf_file.name = f"Publicacion_{pub_id}.pdf"
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=pdf_file,
                    filename=pdf_file.name
                )
        else:
            await update.effective_message.reply_text("No se pudo generar el PDF de la publicación.")

# Función para convertir el enlace de Google Drive en un enlace de descarga directa
def convert_drive_url(url):
    parsed_url = urlparse(url)
    if 'drive.google.com' in parsed_url.netloc:
        # Para enlaces del tipo '/file/d/FILE_ID/view'
        if '/file/d/' in parsed_url.path:
            file_id = parsed_url.path.split('/')[3]
        # Para enlaces compartidos
        elif '/open' in parsed_url.path:
            query_params = parse_qs(parsed_url.query)
            file_id = query_params.get('id', [None])[0]
        else:
            file_id = None
        if file_id:
            download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
            return download_url
    # Si no es un enlace de Google Drive válido, devolvemos el URL original
    return url

# Función para verificar nuevas publicaciones y notificar a los suscriptores
async def check_for_new_publications(application: Application):
    global previous_publications
    current_data = await scrape_page()
    vigente_data = filter_vigente(current_data)

    # Obtener IDs de las publicaciones actuales
    current_publications = set(row[0] for row in vigente_data)

    # Detectar nuevas publicaciones
    new_publications = current_publications - previous_publications

    if new_publications:
        previous_publications = current_publications
        for pub_id in new_publications:
            pub_details = next((row for row in vigente_data if row[0] == pub_id), None)
            if pub_details:
                message, reply_markup = format_single_publication(pub_details)
                for user_id in subscribers:
                    try:
                        await application.bot.send_message(
                            chat_id=user_id,
                            text="¡Nueva publicación disponible!",
                            parse_mode='HTML'
                        )
                        await application.bot.send_message(
                            chat_id=user_id,
                            text=message,
                            reply_markup=reply_markup,
                            parse_mode='HTML'
                        )
                    except Exception as e:
                        logger.error(f"Error al enviar mensaje a {user_id}: {e}")

# Manejo de errores
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Loguea los errores que ocurren"""
    logger.error(msg="Excepción durante la actualización.", exc_info=context.error)

async def post_init(application: Application):
    """Función para ejecutar después de la inicialización de la aplicación"""
    await application.bot.set_my_commands([
        BotCommand("start", "Iniciar el bot"),
        BotCommand("help", "Mostrar ayuda"),
        BotCommand("vigentes", "Mostrar elementos vigentes"),
        BotCommand("subscribe", "Suscribirse a notificaciones automáticas"),
        BotCommand("unsubscribe", "Darse de baja de las notificaciones"),
    ])
    logger.info("Comandos del bot establecidos correctamente.")

def main():
    """Inicia el bot"""
    application = Application.builder().token(bot_token).post_init(post_init).build()

    # Registrar comandos
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("vigentes", vigentes))
    application.add_handler(CommandHandler("subscribe", subscribe))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe))

    # Registrar handlers adicionales
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_error_handler(error_handler)

    # Configurar el scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_for_new_publications,
        args=(application,),
        trigger=IntervalTrigger(minutes=5),
        next_run_time=datetime.now()
    )
    scheduler.start()

    # Inicia el bot
    application.run_polling()

if __name__ == '__main__':
    main()
