#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# podman-run.sh — Rembg Web UI başlatma scripti
#
# Kullanım:
#   chmod +x podman-run.sh
#   ./podman-run.sh             # Build + çalıştır
#   ./podman-run.sh --update    # ⚡ HIZLI: Rebuild olmadan kodu güncelle (5sn!)
#   ./podman-run.sh --rebuild   # Zorla yeniden build et
#   ./podman-run.sh --stop      # Container'ı durdur
#   ./podman-run.sh --restart   # Yeniden başlat
#   ./podman-run.sh --logs      # Log'ları takip et
#   ./podman-run.sh --status    # Durum göster
#   ./podman-run.sh --shell     # Container içine gir
#   ./podman-run.sh --clean     # Her şeyi sil
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Renkler ────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
PURPLE='\033[0;35m'
BOLD='\033[1m'
NC='\033[0m'

# ── Değişkenler ─────────────────────────────────────────────────────────────
IMAGE_NAME="rembg-web"
CONTAINER_NAME="rembg"
PORT="${REMBG_PORT:-7000}"
VOLUME_NAME="rembg-models"
CONTAINERFILE="${CONTAINERFILE:-Containerfile}"
# Projenin gerçek yolu (bu script'in bulunduğu dizin)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Yardım ─────────────────────────────────────────────────────────────────
usage() {
    echo -e "${BOLD}${PURPLE}🎨 Rembg Web UI — Podman Başlatıcı${NC}"
    echo ""
    echo -e "${BOLD}Kullanım:${NC}"
    echo -e "  ${CYAN}./podman-run.sh${NC}            → Build + başlat"
    echo -e "  ${CYAN}./podman-run.sh --update${NC}   → ⚡ Kodu güncelle (rebuild YOK, 5sn)"
    echo -e "  ${CYAN}./podman-run.sh --rebuild${NC}  → Yeniden build et + başlat"
    echo -e "  ${CYAN}./podman-run.sh --stop${NC}     → Durdur"
    echo -e "  ${CYAN}./podman-run.sh --restart${NC}  → Yeniden başlat"
    echo -e "  ${CYAN}./podman-run.sh --logs${NC}     → Canlı log takibi (Ctrl+C ile çık)"
    echo -e "  ${CYAN}./podman-run.sh --status${NC}   → Durum bilgisi"
    echo -e "  ${CYAN}./podman-run.sh --shell${NC}    → Container içine gir"
    echo -e "  ${CYAN}./podman-run.sh --clean${NC}    → Her şeyi sil"
    echo ""
    echo -e "${BOLD}Ortam değişkenleri:${NC}"
    echo -e "  ${CYAN}REMBG_PORT${NC}=7000   → Port (varsayılan: 7000)"
}

# ── Podman kontrolü ─────────────────────────────────────────────────────────
check_podman() {
    if ! command -v podman &>/dev/null; then
        echo -e "${RED}❌ Podman bulunamadı!${NC}"
        echo -e "   Ubuntu/Debian: ${CYAN}sudo apt-get install podman${NC}"
        echo -e "   Fedora/RHEL:   ${CYAN}sudo dnf install podman${NC}"
        exit 1
    fi
    echo -e "${GREEN}✅ Podman: $(podman --version)${NC}"
}

# ── Volume oluştur (yoksa) ─────────────────────────────────────────────────
ensure_volume() {
    if ! podman volume inspect "${VOLUME_NAME}" &>/dev/null; then
        echo -e "${YELLOW}📦 Volume oluşturuluyor: ${VOLUME_NAME}${NC}"
        podman volume create "${VOLUME_NAME}"
    else
        echo -e "${GREEN}✅ Volume mevcut: ${VOLUME_NAME}${NC}"
    fi
}

# ── Build ───────────────────────────────────────────────────────────────────
build_image() {
    echo -e "\n${BOLD}${PURPLE}🔨 Image build ediliyor...${NC}"
    echo -e "${YELLOW}   İlk build ~5-10 dakika sürer (bağımlılıklar + u2net modeli).${NC}\n"

    podman build \
        -f "${CONTAINERFILE}" \
        -t "${IMAGE_NAME}:latest" \
        --label "build-date=$(date -Iseconds)" \
        "${SCRIPT_DIR}"

    echo -e "\n${GREEN}✅ Image hazır: ${IMAGE_NAME}:latest${NC}"
}

# ── Mevcut container'ı durdur ve kaldır ────────────────────────────────────
remove_existing() {
    if podman container exists "${CONTAINER_NAME}" 2>/dev/null; then
        echo -e "${YELLOW}⏹️  Mevcut container durduruluyor...${NC}"
        podman stop "${CONTAINER_NAME}" 2>/dev/null || true
        podman rm "${CONTAINER_NAME}" 2>/dev/null || true
    fi
}

# ── Container başlat ────────────────────────────────────────────────────────
# DEV_MODE=true → rembg/ kaynak dizini volume olarak mount edilir (rebuild gerekmez)
start_container() {
    local dev_mode="${1:-false}"
    echo -e "\n${BOLD}${PURPLE}🚀 Container başlatılıyor...${NC}"

    local EXTRA_VOLS=""
    if [ "${dev_mode}" = "true" ]; then
        echo -e "${CYAN}⚡ DEV MODE: Kaynak kodu volume olarak mount ediliyor...${NC}"
        EXTRA_VOLS="-v ${SCRIPT_DIR}/rembg:/rembg/rembg:Z"
    fi

    podman run -d \
        --name "${CONTAINER_NAME}" \
        --restart unless-stopped \
        -p "${PORT}:7000" \
        -v "${VOLUME_NAME}:/root/.u2net:Z" \
        ${EXTRA_VOLS} \
        "${IMAGE_NAME}:latest"

    # Port'un açılmasını bekle (max 30sn)
    echo -e "${YELLOW}⏳ Web sunucusu başlatılıyor...${NC}"
    local tries=0
    while [ $tries -lt 30 ]; do
        if podman exec "${CONTAINER_NAME}" curl -sf http://localhost:7000/api &>/dev/null; then
            break
        fi
        sleep 1
        tries=$((tries + 1))
        echo -n "."
    done
    echo ""

    if podman exec "${CONTAINER_NAME}" curl -sf http://localhost:7000/api &>/dev/null; then
        echo -e "\n${GREEN}${BOLD}✅ Rembg hazır!${NC}"
    else
        echo -e "\n${YELLOW}⚠️  Sunucu henüz yanıt vermiyor — log için: ./podman-run.sh --logs${NC}"
    fi

    echo -e ""
    echo -e "  ${CYAN}🎨 Web Arayüzü : ${BOLD}http://localhost:${PORT}${NC}"
    echo -e "  ${CYAN}🔌 REST API    : ${BOLD}http://localhost:${PORT}/api${NC}"
    echo -e "  ${CYAN}📋 Loglar      : ${BOLD}./podman-run.sh --logs${NC}"
    echo -e ""
}

# ── Durum ───────────────────────────────────────────────────────────────────
show_status() {
    echo -e "\n${BOLD}${PURPLE}📊 Container Durumu${NC}"
    echo "─────────────────────────────────────────"

    if podman container exists "${CONTAINER_NAME}" 2>/dev/null; then
        STATUS=$(podman inspect "${CONTAINER_NAME}" --format "{{.State.Status}}" 2>/dev/null)
        STARTED=$(podman inspect "${CONTAINER_NAME}" --format "{{.State.StartedAt}}" 2>/dev/null)

        if [ "${STATUS}" = "running" ]; then
            echo -e "  Durum  : ${GREEN}● Çalışıyor${NC}"
        else
            echo -e "  Durum  : ${RED}● ${STATUS}${NC}"
        fi
        echo -e "  Port   : ${CYAN}http://localhost:${PORT}${NC}"
        echo -e "  Image  : ${IMAGE_NAME}:latest"
        echo -e "  Başladı: ${STARTED}"
        echo ""
        echo -e "${BOLD}Son 30 log satırı:${NC}"
        podman logs --tail 30 "${CONTAINER_NAME}" 2>&1 || true
    else
        echo -e "  ${RED}❌ '${CONTAINER_NAME}' container'ı bulunamadı.${NC}"
        echo -e "  ${YELLOW}Başlatmak için: ./podman-run.sh${NC}"
    fi
}

# ── Temizlik ─────────────────────────────────────────────────────────────────
clean_all() {
    echo -e "${RED}${BOLD}⚠️  TÜM rembg verileri silinecek (container, image, model cache)!${NC}"
    read -r -p "Emin misiniz? (evet/hayır): " confirm
    if [ "$confirm" = "evet" ]; then
        podman stop "${CONTAINER_NAME}" 2>/dev/null || true
        podman rm "${CONTAINER_NAME}" 2>/dev/null || true
        podman rmi "${IMAGE_NAME}:latest" 2>/dev/null || true
        podman volume rm "${VOLUME_NAME}" 2>/dev/null || true
        echo -e "${GREEN}✅ Temizlik tamamlandı.${NC}"
    else
        echo "İptal edildi."
    fi
}

# ── Ana akış ────────────────────────────────────────────────────────────────
main() {
    check_podman

    case "${1:-}" in
        --stop)
            echo -e "${YELLOW}⏹️  Durduruluyor...${NC}"
            podman stop "${CONTAINER_NAME}" && echo -e "${GREEN}✅ Durduruldu.${NC}"
            ;;
        --restart)
            echo -e "${YELLOW}🔄 Yeniden başlatılıyor...${NC}"
            podman restart "${CONTAINER_NAME}" && echo -e "${GREEN}✅ Yeniden başlatıldı.${NC}"
            echo -e "  ${CYAN}🎨 http://localhost:${PORT}${NC}"
            ;;
        --logs)
            echo -e "${CYAN}📋 Loglar (Ctrl+C ile çık):${NC}"
            podman logs -f "${CONTAINER_NAME}"
            ;;
        --status)
            show_status
            ;;
        --shell)
            echo -e "${CYAN}🐚 Container içine giriliyor...${NC}"
            podman exec -it "${CONTAINER_NAME}" /bin/bash
            ;;
        --clean)
            clean_all
            ;;
        --rebuild)
            ensure_volume
            remove_existing
            build_image
            start_container "false"
            ;;
        --update)
            # ⚡ HIZLI GÜNCELLEME: Rebuild olmadan kodu güncelle
            echo -e "${BOLD}${CYAN}⚡ Hızlı güncelleme (rebuild yok)...${NC}"
            remove_existing
            ensure_volume
            start_container "true"
            echo -e "${GREEN}✅ Kod güncellendi — rebuild yapılmadı!${NC}"
            ;;
        --help|-h)
            usage
            ;;
        "")
            ensure_volume
            if ! podman image exists "${IMAGE_NAME}:latest" 2>/dev/null; then
                build_image
            else
                echo -e "${GREEN}✅ Image mevcut (yeniden build için --rebuild kullanın)${NC}"
            fi
            remove_existing
            start_container "false"
            ;;
        *)
            echo -e "${RED}❌ Bilinmeyen seçenek: ${1}${NC}"
            usage
            exit 1
            ;;
    esac
}

main "$@"
