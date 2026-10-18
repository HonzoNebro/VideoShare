# VideoShare Bot

Bot de Telegram para recibir enlaces publicos de video o imagenes, descargarlos con `yt-dlp` y devolverlos como contenido nativo con titulo, descripcion y enlace original.

## Requisitos

- Raspberry Pi 4 con Raspberry Pi OS/Raspbian, preferiblemente 64-bit.
- Python 3.10 o superior.
- `ffmpeg`.
- Token de bot creado con BotFather.
- IDs de usuarios o chats permitidos.

Comprobaciones recomendadas en la Raspberry:

```bash
uname -m
cat /etc/os-release
python3 --version
ffmpeg -version
df -h
free -h
vcgencmd measure_temp
```

## Instalacion

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip ffmpeg
sudo mkdir -p /opt/videoshare-bot
sudo chown "$USER":"$USER" /opt/videoshare-bot
cp -a . /opt/videoshare-bot/
cd /opt/videoshare-bot
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

Edita `.env` y configura al menos:

```bash
TELEGRAM_BOT_TOKEN=...
ALLOWED_USER_IDS=...
ALLOWED_CHAT_IDS=
```

Si no configuras `ALLOWED_USER_IDS` ni `ALLOWED_CHAT_IDS`, el bot aceptara mensajes de cualquiera. No se recomienda.
Por defecto, los chats privados reciben el texto completo del video y los grupos reciben solo el video. Puedes cambiarlo con `CAPTION_MODE_PRIVATE` y `CAPTION_MODE_GROUP`, usando `full`, `link` o `none`.
Si quieres recibir por privado errores operativos del bot, configura `ERROR_REPORT_USER_ID` con tu user id.

## Ejecucion manual

```bash
cd /opt/videoshare-bot
bash scripts/run.sh
```

Para instalar o actualizar dependencias antes de arrancar:

```bash
bash scripts/run.sh --install
```

Para instalar dependencias sin arrancar:

```bash
bash scripts/run.sh --install-only
```

## Servicio systemd

La unidad incluida asume que el proyecto vive en `/opt/videoshare-bot` y se ejecuta con el usuario `pi`. Si usas otro usuario, cambia `User=`.

```bash
sudo cp /opt/videoshare-bot/systemd/videoshare-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now videoshare-bot
sudo journalctl -u videoshare-bot -f
```

## Actualizar extractores

Las plataformas cambian a menudo. Actualiza `yt-dlp` cuando una web deje de funcionar:

```bash
cd /opt/videoshare-bot
. .venv/bin/activate
pip install --upgrade yt-dlp
sudo systemctl restart videoshare-bot
```

Tambien puedes usar:

```bash
bash scripts/update-ytdlp.sh
```

## Uso

En chat privado, solo los usuarios incluidos en `ALLOWED_USER_IDS` pueden usar el bot:

```text
https://youtu.be/...
```

En grupos, cualquier usuario puede usarlo solo si el grupo esta en `ALLOWED_CHAT_IDS` y el mensaje menciona al bot:

```text
@compartirvideosbot https://youtu.be/...
```

Para que el bot lea mensajes normales en grupos, revisa en BotFather:

```text
/mybots -> tu bot -> Bot Settings -> Group Privacy -> Turn off
```

Mientras trabaja, el bot edita un unico mensaje de estado con una barra sencilla de progreso.
Tras leer cada enlace, muestra botones para elegir `Video` o `Audio` y despues `Alta`, `Media` o `Baja`. Si el enlace contiene imagenes, permite elegir `Primera` o `Todas` y la calidad de imagen. Si contiene video e imagenes, primero pregunta que tipo descargar.
Si otro contenido pesado se esta procesando, el siguiente queda en cola y el mensaje de estado lo indica.
Las actualizaciones de progreso no criticas se limitan por defecto a una cada 10 segundos.

Comandos utiles:

```text
/id      muestra user_id y chat_id
/status  muestra cola, capacidad y estadisticas de cache
```

`/id` responde en cualquier chat donde el bot vea el comando para facilitar la configuracion de allowlists. No permite procesar videos por si solo.

## Comportamiento

- Soporta enlaces publicos de sitios admitidos por `yt-dlp`.
- Puede usar sesiones dedicadas de X e Instagram para contenido publico bloqueado tras inicio de sesion.
- Permite elegir video o solo audio y tres niveles de calidad por enlace.
- Permite descargar imagenes individuales y albumes/carruseles como fotos nativas de Telegram.
- Permite recortar video o audio indicando un rango `MM:SS.cc-MM:SS.cc`.
- Intenta producir MP4 para video y M4A/MP3 para audio.
- Comprueba que exista una pista de audio y analiza su volumen sin recodificar el video. Si falta o toda la pista es practicamente silenciosa, intenta otra seleccion de formatos y avisa si no encuentra audio audible. Puede tratarse de un silencio intencionado; ruido audible no se distingue de voz o musica.
- Cuando el video H.264 es compatible, copia su pista y convierte solo el audio a AAC si hace falta; para otros codecs de video puede ser necesaria una conversion completa.
- Convierte y recomprime imagenes con Pillow cuando hace falta para que Telegram las acepte como foto.
- Si el archivo supera `MAX_UPLOAD_MB`, intenta reducir calidad con `ffmpeg`.
- Si no puede reducirlo lo suficiente, devuelve un aviso y el enlace original.
- Cachea durante 30 dias el `file_id` devuelto por Telegram para reenviar copias de la misma variante sin descargarla ni recomprimirla de nuevo. Los albumes guardan la lista ordenada de `file_id`.
- Un mismo enlace puede tener entradas separadas para `video-high`, `video-medium`, `video-low`, `audio-high`, `audio-medium`, `audio-low`, `image-*-count1` e `image-*-countall`; solo se crean cuando alguien pide esa variante.
- Las variantes de audio y video validadas utilizan claves de cache versionadas, por lo que no reutilizan archivos anteriores que no hayan pasado la comprobacion de audio.
- Mantiene alias de URL normalizadas para vincular enlaces repetidos con la clave base del video.
- Recuerda durante el mismo TTL los videos rechazados por duracion o tamano estimado para no volver a procesarlos.
- No guarda videos procesados de forma permanente; la cache por defecto es SQLite en `DOWNLOAD_DIR/cache.sqlite3`.
- Controla el texto enviado junto al video con `CAPTION_MODE_PRIVATE=full` y `CAPTION_MODE_GROUP=none` por defecto. Valores admitidos: `full`, `link`, `none`.
- Envia errores operativos de Telegram, codigo o configuracion al usuario definido en `ERROR_REPORT_USER_ID`, sin responder esos detalles en el chat original.
- Limita descargas/conversiones pesadas con `MAX_CONCURRENT_JOBS=1` por defecto, recomendado para Raspberry Pi 4.
- Aplica limites preventivos configurables de duracion y tamano estimado antes de descargar.
- El limite por defecto es `MAX_UPLOAD_MB=1024`, `MAX_ESTIMATED_DOWNLOAD_MB=1024` y `MAX_VIDEO_DURATION_SECONDS=1200`.
- Para subir archivos nuevos de mas de 50 MB necesitas usar un servidor local de Telegram Bot API y configurar `TELEGRAM_API_BASE_URL`, `TELEGRAM_API_BASE_FILE_URL` y `TELEGRAM_LOCAL_MODE=true`.

## Bot API local

Telegram limita las subidas nuevas mediante `api.telegram.org` a 50 MB. Con un servidor local de Bot API, Telegram admite subidas de hasta 2000 MB, por lo que el limite de 1 GB del bot solo es viable con esa configuracion.

Ejemplo de `.env` si tu Bot API local escucha en `127.0.0.1:8081`:

```env
MAX_UPLOAD_MB=1024
TELEGRAM_API_BASE_URL=http://127.0.0.1:8081/bot
TELEGRAM_API_BASE_FILE_URL=http://127.0.0.1:8081/file/bot
TELEGRAM_LOCAL_MODE=true
TELEGRAM_READ_TIMEOUT_SECONDS=600
TELEGRAM_WRITE_TIMEOUT_SECONDS=600
```

## Cookies opcionales para yt-dlp

Algunos enlaces publicos de YouTube pueden devolver:

```text
Sign in to confirm you're not a bot
```

En ese caso exporta cookies en formato Netscape y configura:

```env
YTDLP_COOKIES_FILE=/opt/videoshare-bot/cookies.txt
YTDLP_JS_RUNTIMES=deno
YTDLP_REMOTE_COMPONENTS=ejs:npm
```

Despues reinicia el bot. No compartas ese archivo: equivale a una sesion del navegador.
Para YouTube moderno, instala tambien un runtime JavaScript compatible. Deno 2+ es el recomendado por `yt-dlp`.

### Sesiones dedicadas de X e Instagram

Para enlaces publicos que X o Instagram obligan a abrir con sesion, usa cuentas tecnicas dedicadas y exporta las cookies de cada dominio por separado, en formato Netscape:

```env
YTDLP_X_COOKIES_FILE=/opt/videoshare-bot/cookies-x.txt
YTDLP_INSTAGRAM_COOKIES_FILE=/opt/videoshare-bot/cookies-instagram.txt
```

Las cookies especificas tienen prioridad sobre `YTDLP_COOKIES_FILE`. Si una de ellas esta configurada pero no puede leerse, el bot avisa para renovarla y no prueba otra cuenta. Esta opcion solo permite acceder al contenido que la cuenta dedicada puede ver legitimamente; no habilita contenido privado ni evita restricciones de la plataforma.

Protege los archivos y no los añadas a Git:

```bash
chmod 600 /opt/videoshare-bot/cookies-x.txt /opt/videoshare-bot/cookies-instagram.txt
```

Cuando caduque una sesion, reemplaza solo el archivo de esa plataforma y reinicia el servicio.
