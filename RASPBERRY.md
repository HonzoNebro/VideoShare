# Version reducida para Raspberry Pi

Esta carpeta contiene solo lo necesario para ejecutar el bot:

- `bot/`: codigo del bot.
- `requirements.txt`: dependencias Python.
- `.env.example`: plantilla de configuracion.
- `scripts/run.sh`: arranque manual simple.
- `scripts/install-raspi.sh`: instalacion en `/opt/videoshare-bot` y unidad systemd.
- `systemd/videoshare-bot.service`: servicio para ejecutar al arrancar.

No incluye `.env`, `.venv`, tests ni caches.

## Copia rapida

En tu PC:

```bash
scp dist/videoshare-bot-raspi.tar.gz pi@RASPBERRY_IP:/tmp/
```

En la Raspberry:

```bash
sudo mkdir -p /opt/videoshare-bot
sudo chown "$USER":"$USER" /opt/videoshare-bot
tar -xzf /tmp/videoshare-bot-raspi.tar.gz -C /opt/videoshare-bot
cd /opt/videoshare-bot
sudo apt update
sudo apt install -y python3-venv python3-pip ffmpeg
bash scripts/install-raspi.sh
nano .env
```

Configura:

```env
TELEGRAM_BOT_TOKEN=tu_token
ALLOWED_USER_IDS=tu_user_id
ALLOWED_CHAT_IDS=chat_id_del_grupo
ERROR_REPORT_USER_ID=tu_user_id
```

Para obtener el `chat_id` del grupo, añade el bot y escribe:

```text
/id
```

Ese comando responde aunque el grupo aun no este en `ALLOWED_CHAT_IDS`.

En privado, solo podran usarlo los usuarios de `ALLOWED_USER_IDS`.
En grupos permitidos, cualquier usuario podra usarlo escribiendo:

```text
@compartirvideosbot https://youtu.be/...
```

Por defecto, en privado se envia el video con titulo, descripcion y enlace, y en grupos se envia solo el video. Puedes cambiarlo en `.env`:

```env
CAPTION_MODE_PRIVATE=full
CAPTION_MODE_GROUP=none
```

Valores admitidos: `full`, `link`, `none`.

Al recibir un enlace, el bot muestra botones para elegir `Video` o `Audio` y despues `Alta`, `Media` o `Baja`.
Los archivos enviados se recuerdan durante 30 dias usando el `file_id` de Telegram en `DOWNLOAD_DIR/cache.sqlite3`. Esto permite mandar una copia de la misma variante sin mostrar el autor del mensaje original y sin volver a procesarla.
Cada enlace puede crear hasta seis entradas de cache, una por variante, pero solo cuando alguien la pide.
Los videos rechazados por duracion o tamano estimado tambien se recuerdan durante ese TTL para responder rapido si se repiten.
Los errores operativos de Telegram, codigo o configuracion se envian por privado a `ERROR_REPORT_USER_ID` si esta configurado.

Por defecto solo se procesa un video pesado a la vez con `MAX_CONCURRENT_JOBS=1`. Si llega otro enlace no cacheado, el bot muestra que esta en cola.
El progreso se actualiza como maximo cada 10 segundos salvo cambios de fase importantes.

Para Raspberry Pi 4, valores razonables:

```env
MAX_UPLOAD_MB=1024
MAX_VIDEO_DURATION_SECONDS=1200
MAX_ESTIMATED_DOWNLOAD_MB=1024
```

Para subir archivos nuevos de mas de 50 MB, usa un servidor local de Telegram Bot API y configura:

```env
TELEGRAM_API_BASE_URL=http://127.0.0.1:8081/bot
TELEGRAM_API_BASE_FILE_URL=http://127.0.0.1:8081/file/bot
TELEGRAM_LOCAL_MODE=true
```

Si el bot no ve mensajes del grupo, desactiva Privacy Mode en BotFather:

```text
/mybots -> tu bot -> Bot Settings -> Group Privacy -> Turn off
```

Prueba manual:

```bash
bash scripts/run.sh
```

Si acabas de copiar una version con dependencias nuevas:

```bash
bash scripts/run.sh --install
```

Servicio:

```bash
sudo systemctl enable --now videoshare-bot
sudo journalctl -u videoshare-bot -f
```

La unidad incluye `/home/pi/.deno/bin` en el `PATH` para que `yt-dlp` pueda usar Deno tambien cuando se ejecuta bajo `systemd`. Si instalaste Deno en otra ruta o usas otro usuario, ajusta `Environment=PATH=...` en `/etc/systemd/system/videoshare-bot.service`.

Actualizar solo `yt-dlp` cuando una plataforma falle:

```bash
cd /opt/videoshare-bot
bash scripts/update-ytdlp.sh
```

Si YouTube pide verificar que no eres un bot, copia un archivo de cookies exportado en formato Netscape y configura en `.env`:

```env
YTDLP_COOKIES_FILE=/opt/videoshare-bot/cookies.txt
YTDLP_JS_RUNTIMES=deno
YTDLP_REMOTE_COMPONENTS=ejs:npm
```

Reinicia el bot despues. Protege ese archivo con permisos restrictivos.
Si ves `n challenge solving failed` o `Only images are available`, instala Deno 2+ en la Raspberry y vuelve a ejecutar `bash scripts/run.sh --install`.

Diagnostico rapido:

```bash
bash scripts/diagnose-ytdlp.sh
```
