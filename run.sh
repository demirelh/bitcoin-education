#!/usr/bin/env bash
#
# run.sh - Production deployment script for btcedu
#
# This script safely updates the bitcoin-education application by:
# 1. Pulling latest code from git
# 2. Installing/updating Python dependencies
# 3. Running database migrations
# 4. Restarting the web service
#
# Usage: ./run.sh
#
# Requirements:
# - Must be run from the project root directory
# - Python virtual environment must exist at .venv/
# - User must have sudo privileges for systemctl commands

set -euo pipefail  # Exit on error, undefined variables, and pipe failures

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${SCRIPT_DIR}"
VENV_PATH="${PROJECT_ROOT}/.venv"
SERVICE_NAME="btcedu-web"
LOG_PREFIX="[run.sh]"

# Colors for output (optional, falls back gracefully if not supported)
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Logging functions
log_info() {
    echo -e "${GREEN}${LOG_PREFIX}${NC} $*"
}

log_warn() {
    echo -e "${YELLOW}${LOG_PREFIX}${NC} $*"
}

log_error() {
    echo -e "${RED}${LOG_PREFIX}${NC} $*" >&2
}

# Error handler
error_exit() {
    log_error "FAILED: $1"
    exit 1
}

# Check prerequisites
check_prerequisites() {
    log_info "Checking prerequisites..."

    # Check if we're in a git repository
    if ! git rev-parse --git-dir > /dev/null 2>&1; then
        error_exit "Not a git repository. Please run this script from the project root."
    fi

    # Check if virtual environment exists
    if [ ! -d "${VENV_PATH}" ]; then
        error_exit "Virtual environment not found at ${VENV_PATH}. Please create it first with: python -m venv .venv"
    fi

    # Check if Python is available in venv
    if [ ! -f "${VENV_PATH}/bin/python" ]; then
        error_exit "Python not found in virtual environment."
    fi

    # Check if btcedu is installed
    if ! "${VENV_PATH}/bin/python" -c "import btcedu" 2>/dev/null; then
        log_warn "btcedu package not installed in venv. Will install dependencies."
    fi

    log_info "Prerequisites check passed."
}

# Pull latest code from git
pull_latest_code() {
    log_info "Pulling latest code from git..."

    # Get current branch
    CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
    log_info "Current branch: ${CURRENT_BRANCH}"

    # Check for uncommitted changes
    if ! git diff-index --quiet HEAD -- 2>/dev/null; then
        log_warn "WARNING: You have uncommitted changes. Stashing them..."
        git stash push -m "Auto-stash by run.sh at $(date)"
    fi

    # Pull latest changes
    if ! git pull origin "${CURRENT_BRANCH}"; then
        error_exit "Failed to pull latest code from git."
    fi

    # Show latest commit
    LATEST_COMMIT=$(git log -1 --oneline)
    log_info "Updated to: ${LATEST_COMMIT}"
}

# Install/update dependencies
install_dependencies() {
    log_info "Installing/updating Python dependencies..."

    # Activate virtual environment and install
    if ! "${VENV_PATH}/bin/pip" install -e ".[web]" --quiet; then
        error_exit "Failed to install dependencies."
    fi

    log_info "Dependencies installed successfully."
}

# Run database migrations
run_migrations() {
    log_info "Running database migrations..."

    # Check migration status first
    if ! "${VENV_PATH}/bin/btcedu" migrate-status > /dev/null 2>&1; then
        log_warn "Could not check migration status. Proceeding with migration anyway."
    fi

    # Run migrations
    if ! "${VENV_PATH}/bin/btcedu" migrate; then
        error_exit "Database migration failed."
    fi

    log_info "Database migrations completed successfully."
}

# Restart web service
restart_service() {
    log_info "Restarting ${SERVICE_NAME} service..."

    # Check if service exists
    if ! systemctl list-unit-files | grep -q "${SERVICE_NAME}.service"; then
        log_warn "Service ${SERVICE_NAME} not found. Skipping restart."
        log_warn "To enable the service, run: sudo cp deploy/${SERVICE_NAME}.service /etc/systemd/system/ && sudo systemctl enable --now ${SERVICE_NAME}"
        return 0
    fi

    # Restart service
    if ! sudo systemctl restart "${SERVICE_NAME}"; then
        error_exit "Failed to restart ${SERVICE_NAME} service."
    fi

    # Wait a moment for service to start
    sleep 2

    # Check service status
    if sudo systemctl is-active --quiet "${SERVICE_NAME}"; then
        log_info "Service ${SERVICE_NAME} restarted successfully and is running."
    else
        log_error "Service ${SERVICE_NAME} failed to start. Check logs with: sudo journalctl -u ${SERVICE_NAME} -n 50"
        exit 1
    fi
}

# Main execution
main() {
    log_info "Starting deployment process..."
    log_info "Project root: ${PROJECT_ROOT}"

    # Change to project root
    cd "${PROJECT_ROOT}"

    # Execute deployment steps
    check_prerequisites
    pull_latest_code
    install_dependencies
    run_migrations
    restart_service

    log_info "=========================================="
    log_info "Deployment completed successfully!"
    log_info "=========================================="
    log_info "Check service status: sudo systemctl status ${SERVICE_NAME}"
    log_info "View logs: sudo journalctl -u ${SERVICE_NAME} -f"
}

# Run main function
main "$@"
