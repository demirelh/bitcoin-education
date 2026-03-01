#!/usr/bin/env bash
# btcedu production deployment setup
# Installs system deps, configures .env + API keys, sets up systemd services,
# and configures Caddy reverse proxy.
#
# Run from: /home/pi/AI-Startup-Lab/bitcoin-education
# Idempotent: safe to re-run — only installs/configures what's missing.
set -euo pipefail

PROJ="/home/pi/AI-Startup-Lab/bitcoin-education"
VENV="$PROJ/.venv"
DEPLOY="$PROJ/deploy"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }
err()  { echo -e "  ${RED}✗${NC} $1"; }

# Helper: Key in .env setzen (ersetzt Platzhalter oder leeren Wert)
set_env_key() {
  local key="$1" value="$2"
  if grep -q "^${key}=" "$PROJ/.env"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$PROJ/.env"
  else
    echo "${key}=${value}" >> "$PROJ/.env"
  fi
}

# Helper: Prüfe ob Key in .env gesetzt ist (nicht leer, nicht Platzhalter)
key_is_set() {
  local key="$1"
  local val
  val=$(grep "^${key}=" "$PROJ/.env" 2>/dev/null | cut -d'=' -f2- | xargs 2>/dev/null || true)
  [[ -n "$val" && "$val" != "sk-ant-xxx" && "$val" != "sk-xxx" && "$val" != '""' && "$val" != "''" ]]
}

echo ""
echo "=== btcedu production setup ==="
echo ""

# ═══════════════════════════════════════════════════════════════════
# 0/7  System-Abhängigkeiten
# ═══════════════════════════════════════════════════════════════════
echo "[0/7] System-Abhängigkeiten..."

# ffmpeg
if command -v ffmpeg &>/dev/null; then
  ok "ffmpeg $(ffmpeg -version 2>&1 | head -1 | cut -d' ' -f3)"
else
  warn "ffmpeg wird installiert..."
  sudo apt-get update -qq && sudo apt-get install -y -qq ffmpeg
  ok "ffmpeg installiert"
fi

# sqlite3
if command -v sqlite3 &>/dev/null; then
  ok "sqlite3 vorhanden"
else
  warn "sqlite3 wird installiert..."
  sudo apt-get install -y -qq sqlite3
  ok "sqlite3 installiert"
fi

# Noto Font (für Video-Rendering)
if fc-list 2>/dev/null | grep -qi "NotoSans-Bold"; then
  ok "NotoSans-Bold Font vorhanden"
else
  warn "Noto Fonts werden installiert..."
  sudo apt-get install -y -qq fonts-noto-core 2>/dev/null \
    || sudo apt-get install -y -qq fonts-noto 2>/dev/null \
    || true
  fc-cache -f 2>/dev/null || true
  if fc-list 2>/dev/null | grep -qi "Noto"; then
    ok "Noto Fonts installiert"
  else
    warn "Noto Font nicht gefunden — RENDER_FONT in .env ggf. anpassen"
  fi
fi

# ═══════════════════════════════════════════════════════════════════
# 1/7  Python venv & Packages
# ═══════════════════════════════════════════════════════════════════
echo "[1/7] Python venv & Packages..."

if [ ! -d "$VENV" ]; then
  warn "Virtual environment nicht gefunden — wird erstellt..."
  python3 -m venv "$VENV"
  ok "venv erstellt: $VENV"
fi

# Web dependencies (gunicorn + flask)
if ! "$VENV/bin/python" -c "import gunicorn" 2>/dev/null; then
  warn "Web-Dependencies werden installiert..."
  "$VENV/bin/pip" install -q -e ".[web]"
fi
ok "gunicorn $("$VENV/bin/gunicorn" --version 2>&1 | head -1)"

# YouTube dependencies
if "$VENV/bin/python" -c "import googleapiclient" 2>/dev/null; then
  ok "YouTube Packages vorhanden"
else
  warn "YouTube Packages werden installiert..."
  "$VENV/bin/pip" install -q -e ".[youtube]" 2>/dev/null \
    && ok "YouTube Packages installiert" \
    || warn "YouTube Packages konnten nicht installiert werden"
fi

# ═══════════════════════════════════════════════════════════════════
# 2/7  .env Datei & API Keys
# ═══════════════════════════════════════════════════════════════════
echo "[2/7] .env Konfiguration..."

if [[ -f "$PROJ/.env" ]]; then
  ok ".env existiert"
else
  if [[ -f "$PROJ/.env.example" ]]; then
    cp "$PROJ/.env.example" "$PROJ/.env"
    ok ".env aus .env.example erstellt"
  else
    touch "$PROJ/.env"
    warn ".env neu erstellt (kein .env.example gefunden)"
  fi
fi

# Pipeline Version
current_pv=$(grep "^PIPELINE_VERSION=" "$PROJ/.env" 2>/dev/null | cut -d'=' -f2 || echo "1")
if [[ "$current_pv" == "2" ]]; then
  ok "PIPELINE_VERSION=2"
else
  set_env_key "PIPELINE_VERSION" "2"
  ok "PIPELINE_VERSION auf 2 gesetzt"
fi

echo ""
echo "  API Keys konfigurieren (Enter = überspringen):"
echo ""

# Anthropic
if key_is_set "ANTHROPIC_API_KEY"; then
  ok "ANTHROPIC_API_KEY gesetzt"
else
  echo -n "  Anthropic API Key (sk-ant-...): "
  read -r _key
  if [[ -n "$_key" ]]; then
    set_env_key "ANTHROPIC_API_KEY" "$_key"
    ok "ANTHROPIC_API_KEY gesetzt"
  else
    warn "ANTHROPIC_API_KEY übersprungen (nötig für: correct, translate, adapt, chapterize)"
  fi
fi

# OpenAI
if key_is_set "OPENAI_API_KEY"; then
  ok "OPENAI_API_KEY gesetzt"
else
  echo -n "  OpenAI API Key (sk-...): "
  read -r _key
  if [[ -n "$_key" ]]; then
    set_env_key "OPENAI_API_KEY" "$_key"
    ok "OPENAI_API_KEY gesetzt"
  else
    warn "OPENAI_API_KEY übersprungen (nötig für: Whisper + DALL-E 3)"
  fi
fi

# ElevenLabs API Key
if key_is_set "ELEVENLABS_API_KEY"; then
  ok "ELEVENLABS_API_KEY gesetzt"
else
  echo ""
  echo "  ElevenLabs (TTS / Sprachausgabe):"
  echo "    → Account: https://elevenlabs.io"
  echo "    → API Key: https://elevenlabs.io/app/settings/api-keys"
  echo -n "  ElevenLabs API Key: "
  read -r _key
  if [[ -n "$_key" ]]; then
    set_env_key "ELEVENLABS_API_KEY" "$_key"
    ok "ELEVENLABS_API_KEY gesetzt"
  else
    warn "ELEVENLABS_API_KEY übersprungen (nötig für: TTS Stage)"
  fi
fi

# ElevenLabs Voice ID
if key_is_set "ELEVENLABS_VOICE_ID"; then
  ok "ELEVENLABS_VOICE_ID gesetzt"
else
  echo "    → Voice Library: https://elevenlabs.io/app/voice-library"
  echo "    → Empfehlung: Türkischsprachige Stimme"
  echo -n "  ElevenLabs Voice ID: "
  read -r _key
  if [[ -n "$_key" ]]; then
    set_env_key "ELEVENLABS_VOICE_ID" "$_key"
    ok "ELEVENLABS_VOICE_ID gesetzt"
  else
    warn "ELEVENLABS_VOICE_ID übersprungen (nötig für: TTS Stage)"
  fi
fi

# Podcast YouTube Channel ID
if key_is_set "PODCAST_YOUTUBE_CHANNEL_ID"; then
  ok "PODCAST_YOUTUBE_CHANNEL_ID gesetzt"
else
  echo -n "  YouTube Channel ID des Quell-Podcasts (UC...): "
  read -r _key
  if [[ -n "$_key" ]]; then
    set_env_key "PODCAST_YOUTUBE_CHANNEL_ID" "$_key"
    ok "PODCAST_YOUTUBE_CHANNEL_ID gesetzt"
  else
    warn "PODCAST_YOUTUBE_CHANNEL_ID übersprungen"
  fi
fi

# ═══════════════════════════════════════════════════════════════════
# 3/7  YouTube OAuth (optional)
# ═══════════════════════════════════════════════════════════════════
echo ""
echo "[3/7] YouTube OAuth..."

if [[ -f "$PROJ/data/client_secret.json" ]]; then
  ok "data/client_secret.json vorhanden"
  if [[ -f "$PROJ/data/.youtube_credentials.json" ]]; then
    ok "YouTube OAuth Credentials vorhanden"
  else
    echo -n "  OAuth Token erstellen? (j/N): "
    read -r _do_auth
    if [[ "$_do_auth" =~ ^[jJyY]$ ]]; then
      "$VENV/bin/btcedu" youtube-auth \
        && ok "YouTube OAuth erfolgreich" \
        || warn "YouTube OAuth fehlgeschlagen"
    else
      warn "YouTube OAuth übersprungen — später: btcedu youtube-auth"
    fi
  fi
else
  warn "data/client_secret.json fehlt — YouTube-Upload nicht möglich"
  echo "    → Google Cloud Console → YouTube Data API v3 → OAuth Desktop Client"
  echo "    → JSON nach data/client_secret.json, dann Skript erneut ausführen"
fi

# ═══════════════════════════════════════════════════════════════════
# 4/7  Systemd Services installieren
# ═══════════════════════════════════════════════════════════════════
echo "[4/7] Systemd Services..."

UNITS=(
    btcedu-web.service
    btcedu-detect.service
    btcedu-detect.timer
    btcedu-run.service
    btcedu-run.timer
)
for unit in "${UNITS[@]}"; do
  sudo cp "$DEPLOY/$unit" /etc/systemd/system/
  ok "$unit"
done
sudo systemctl daemon-reload
ok "systemd reloaded"

# ═══════════════════════════════════════════════════════════════════
# 5/7  Services & Timers aktivieren
# ═══════════════════════════════════════════════════════════════════
echo "[5/7] Services aktivieren..."
sudo systemctl enable btcedu-web.service
sudo systemctl enable btcedu-detect.timer
sudo systemctl enable btcedu-run.timer
ok "btcedu-web, btcedu-detect.timer, btcedu-run.timer enabled"

# ═══════════════════════════════════════════════════════════════════
# 6/7  Caddy Basic Auth
# ═══════════════════════════════════════════════════════════════════
echo "[6/7] Caddy Basic Auth..."

if command -v caddy &>/dev/null; then
  echo "  Passwort für Dashboard (User: pi):"
  read -r -s -p "  Password: " PASSWORD
  echo ""
  HASH=$(caddy hash-password --plaintext "$PASSWORD")
  ok "Hash generiert"
  echo ""
  echo "  Caddyfile aktualisieren — Block in lnodebtc.duckdns.org { } einfügen:"
  echo ""
  echo "    redir /dashboard /dashboard/ permanent"
  echo "    @dashboard path /dashboard/*"
  echo "    handle @dashboard {"
  echo "        uri strip_prefix /dashboard"
  echo "        basicauth {"
  echo "            pi $HASH"
  echo "        }"
  echo "        reverse_proxy 127.0.0.1:8091"
  echo "    }"
  echo "    header {"
  echo "        X-Content-Type-Options nosniff"
  echo "        X-Frame-Options DENY"
  echo "        Referrer-Policy no-referrer"
  echo "    }"
  echo ""
else
  warn "caddy nicht installiert — Basic Auth manuell einrichten"
fi

# ═══════════════════════════════════════════════════════════════════
# 7/7  Services starten & Zusammenfassung
# ═══════════════════════════════════════════════════════════════════
echo "[7/7] Services starten..."
sudo systemctl start btcedu-web.service
sudo systemctl start btcedu-detect.timer
sudo systemctl start btcedu-run.timer
ok "Alle Services gestartet"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Zusammenfassung"
echo "═══════════════════════════════════════════════════"
echo ""

all_ok=true

_check() {
  local label="$1" ok_cond="$2"
  if eval "$ok_cond"; then
    ok "$label"
  else
    err "$label"
    all_ok=false
  fi
}

_check "PIPELINE_VERSION=2"         'grep -q "^PIPELINE_VERSION=2" "$PROJ/.env" 2>/dev/null'
_check "ANTHROPIC_API_KEY"          'key_is_set ANTHROPIC_API_KEY'
_check "OPENAI_API_KEY"             'key_is_set OPENAI_API_KEY'
_check "ELEVENLABS_API_KEY"         'key_is_set ELEVENLABS_API_KEY'
_check "ELEVENLABS_VOICE_ID"        'key_is_set ELEVENLABS_VOICE_ID'
_check "PODCAST_YOUTUBE_CHANNEL_ID" 'key_is_set PODCAST_YOUTUBE_CHANNEL_ID'
_check "ffmpeg"                     'command -v ffmpeg &>/dev/null'
_check "Font (Noto)"                'fc-list 2>/dev/null | grep -qi noto'
_check "btcedu-web"                 'systemctl is-active btcedu-web &>/dev/null'
_check "btcedu-detect.timer"        'systemctl is-active btcedu-detect.timer &>/dev/null'
_check "btcedu-run.timer"           'systemctl is-active btcedu-run.timer &>/dev/null'

echo ""
if [[ "$all_ok" == "true" ]]; then
  ok "Alles konfiguriert!"
else
  warn "Fehlende Einträge oben nachholen, dann erneut ausführen."
fi

echo ""
echo "  Timer:     sudo systemctl list-timers btcedu-*"
echo "  Logs:      sudo journalctl -u btcedu-web -f"
echo "  Dashboard: https://lnodebtc.duckdns.org/dashboard/"
echo "  Caddy:     sudo systemctl reload caddy"
echo ""
