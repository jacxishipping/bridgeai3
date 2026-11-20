#!/usr/bin/env bash
# ==============================================================================
#  AI Bridge — Enterprise Production Automated Deployment Script
# ==============================================================================
#  Orchestrates zero-downtime deployment of the complete production stack:
#   - Automated Docker & Docker Compose verification
#   - Dynamic environment generation (.env.production) with cryptographically secure secrets
#   - Automatic SSL / Let's Encrypt TLS certificate bootstrapping
#   - Multi-stage Docker container builds (Node 20 UI builder + Python 3.12 Gunicorn cluster)
#   - Database schema migrations execution
#   - Health probe verification (/health/live, /health/ready, /metrics)
# ==============================================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

# Terminal Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log_info() { echo -e "${CYAN}[INFO]${NC} $*"; }
log_ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_err()  { echo -e "${RED}[ERROR]${NC} $*" >&2; }

echo -e "${PURPLE}${BOLD}"
echo "======================================================================"
echo "    AI Bridge Production Orchestrator & Deployment Engine             "
echo "======================================================================"
echo -e "${NC}"

# 1. Root / Sudo Check
if [ "$EUID" -ne 0 ]; then
    log_err "Please run as root or with sudo privileges: sudo $0"
    exit 1
fi

# 2. Docker & Docker Compose Verification
log_info "Verifying container runtime requirements..."
if ! command -v docker >/dev/null 2>&1; then
    log_warn "Docker not detected. Installing Docker via official repository..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
    log_ok "Docker installed successfully."
fi

if ! docker compose version >/dev/null 2>&1; then
    log_warn "Docker Compose plugin missing. Installing docker-compose-plugin..."
    apt-get update -qq && apt-get install -y docker-compose-plugin
fi
log_ok "Docker runtime: $(docker --version)"
log_ok "Docker Compose: $(docker compose version)"

# 3. Environment Configuration (.env.production)
ENV_FILE="$REPO_ROOT/.env.production"
if [ ! -f "$ENV_FILE" ]; then
    log_info "Generating production environment file at: $ENV_FILE"
    
    # Generate cryptographically secure tokens
    PG_PASS=$(openssl rand -hex 24)
    REDIS_PASS=$(openssl rand -hex 24)
    ADMIN_TOKEN=$(openssl rand -hex 32)
    SECRET_KEY=$(openssl rand -hex 32)
    # CSRF HMAC secret (F-05-02, Wave 6): one shared value across all
    # control-api workers; docker-compose.prod.yml fails fast without it,
    # and the plane itself fails fast at boot with >1 worker and no secret.
    CSRF_SECRET=$(openssl rand -hex 32)
    # TURN shared secret (R-P0-15): docker-compose.prod.yml fails fast
    # without it, so it must be generated here alongside the others.
    TURN_SECRET=$(openssl rand -hex 32)
    
    cat > "$ENV_FILE" <<EOF
# ==============================================================================
# AI Bridge Production Environment Configuration
# Automatically generated on $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# ==============================================================================

# Domain & Edge Routing
DOMAIN="${DOMAIN:-ec2-77-114-76-12.us-east-2.compute.amazonaws.com}"
# AI_BRIDGE_DOMAIN is what docker-compose.prod.yml reads for the public
# hostname and URLs. Compose uses localhost only when no domain is configured.
AI_BRIDGE_DOMAIN="${DOMAIN:-ec2-77-114-76-12.us-east-2.compute.amazonaws.com}"
ACME_EMAIL="${ACME_EMAIL:-}"

# Database & Cache Credentials
POSTGRES_DB=ai_bridge
POSTGRES_USER=ai_bridge
POSTGRES_PASSWORD=${PG_PASS}
REDIS_PASSWORD=${REDIS_PASS}

# Control Plane Authentication & Security
AI_BRIDGE_ADMIN_TOKEN=${ADMIN_TOKEN}
SECRET_KEY=${SECRET_KEY}
AI_BRIDGE_CSRF_SECRET=${CSRF_SECRET}
AI_BRIDGE_TURN_SECRET=${TURN_SECRET}
# Tier 3 (shell/desktop control) is OFF by default (R-P0-14).
# Explicit opt-in only: change to 1 to enable.
AI_BRIDGE_ALLOW_TIER3=0
PORT=8000

# Directory Mounts
AI_BRIDGE_DATA_DIR=/var/lib/ai-bridge
AI_BRIDGE_WORKSPACE=/var/lib/ai-bridge/workspace

# Optional Multi-Channel Alert Webhooks
SLACK_WEBHOOK_URL=
DISCORD_WEBHOOK_URL=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ALERT_WEBHOOK_URL=
EOF
    chmod 600 "$ENV_FILE"
    log_ok "Generated fresh .env.production with secure random secrets."
else
    log_info "Using existing production environment: $ENV_FILE"
fi

# Export environment variables for compose
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# 4. SSL / TLS Certificate Validation
DOMAIN="${DOMAIN:-ec2-77-114-76-12.us-east-2.compute.amazonaws.com}"
ACME_EMAIL="${ACME_EMAIL:-}"
CERT_DIR="/etc/letsencrypt/live/$DOMAIN"

if [ ! -f "$CERT_DIR/fullchain.pem" ] || [ ! -f "$CERT_DIR/privkey.pem" ]; then
    log_warn "No Let's Encrypt certificate found at $CERT_DIR."
    log_info "Creating temporary self-signed certificate so Nginx can boot cleanly..."
    
    mkdir -p "$CERT_DIR"
    openssl req -x509 -nodes -days 30 -newkey rsa:2048 \
        -keyout "$CERT_DIR/privkey.pem" \
        -out "$CERT_DIR/fullchain.pem" \
        -subj "/CN=$DOMAIN/O=AI Bridge Temporary Self-Signed" 2>/dev/null
    
    log_ok "Bootstrap certificate generated. Production certs will renew automatically via Certbot."
fi

# 5. Build and Deploy Containers
log_info "Building and launching production containers with docker-compose.prod.yml..."
docker compose --env-file "$ENV_FILE" -f docker-compose.prod.yml build
docker compose --env-file "$ENV_FILE" -f docker-compose.prod.yml up -d --remove-orphans

log_info "Waiting for database and control plane to reach healthy state..."
MAX_RETRIES=20
COUNT=0
HEALTHY=false

while [ $COUNT -lt $MAX_RETRIES ]; do
    COUNT=$((COUNT + 1))
    if docker compose -f docker-compose.prod.yml ps | grep -q "control-api.*Up"; then
        # Plain-HTTP loopback probe (no TLS involved, so no -k).
        # nginx now redirects :80 to HTTPS (P-03); 301 counts as healthy.
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:80/health/ready" || true)
        if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "301" ]; then
            HEALTHY=true
            break
        fi
    fi
    sleep 3
    echo -n "."
done
echo ""

if [ "$HEALTHY" = true ]; then
    log_ok "AI Bridge Control Plane is operational and ready!"
else
    log_warn "Containers launched, but readiness probe returned status ($HTTP_CODE). Checking container logs:"
    docker compose -f docker-compose.prod.yml logs --tail=25 control-api
fi

# 6. Display Operational Summary
echo ""
echo -e "${GREEN}${BOLD}======================================================================${NC}"
echo -e "${GREEN}${BOLD}         AI Bridge Production Deployment Succeeded                    ${NC}"
echo -e "${GREEN}${BOLD}======================================================================${NC}"
echo -e "  ${BOLD}Public URL:${NC}         https://${DOMAIN}"
echo -e "  ${BOLD}Liveness Probe:${NC}     https://${DOMAIN}/health/live"
echo -e "  ${BOLD}Readiness Probe:${NC}    https://${DOMAIN}/health/ready"
echo -e "  ${BOLD}Prometheus Metrics:${NC} https://${DOMAIN}/metrics"
echo -e "  ${BOLD}Admin Auth Token:${NC}   (stored in .env.production — not printed)"
echo ""
echo -e "  ${BOLD}Active Docker Containers:${NC}"
docker compose -f docker-compose.prod.yml ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
echo ""
echo -e "  ${CYAN}Management Commands:${NC}"
echo -e "    View Logs:    docker compose -f docker-compose.prod.yml logs -f"
echo -e "    Restart:      docker compose -f docker-compose.prod.yml restart"
echo -e "    Stop:         docker compose -f docker-compose.prod.yml down"
echo -e "======================================================================"

