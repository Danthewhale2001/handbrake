#!/bin/bash
# ============================================================
# start.sh — Run this to start/restart/update the app.
#             Takes seconds — HandBrake is already compiled.
#             Run this after any UI or server code changes.
# ============================================================

# Get the directory where this script lives
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Create local folders if they don't exist
mkdir -p "$SCRIPT_DIR/input"
mkdir -p "$SCRIPT_DIR/output"
mkdir -p "$SCRIPT_DIR/config"

# Check base image exists locally, pull from DockerHub if not
if ! docker image inspect handbrake-mobile:base &>/dev/null; then
  echo ""
  echo "  Base image not found locally — pulling from DockerHub..."
  docker pull danthewhale/handbrake:v0.1
  docker tag danthewhale/handbrake:v0.1 handbrake-mobile:base
  if [ $? -ne 0 ]; then
    echo "  Could not pull base image. Run ./build.sh to compile from scratch."
    exit 1
  fi
fi

echo "  Updating app files..."

# Stop and remove existing container
docker rm -f handbrake-mobile 2>/dev/null

# Build a thin app layer on top of the compiled base (takes seconds)
docker build \
  -t handbrake-mobile:latest \
  -f Dockerfile.app .

if [ $? -ne 0 ]; then
  echo "  App build failed."
  exit 1
fi

# Start the container
docker run -d \
  --name handbrake-mobile \
  --restart unless-stopped \
  -p 8888:8888 \
  -e OUTPUT_PATH=/output \
  -v "$SCRIPT_DIR/input":/storage \
  -v "$SCRIPT_DIR/output":/output \
  -v "$SCRIPT_DIR/config":/config \
  -v "/mnt/SSD_1TB/SSD_1TB/(SSD 1TB) Handbrake Encode Folder":/storage/encodes \
  handbrake-mobile:latest

echo ""
echo "  Done! Open http://$(hostname -I | awk '{print $1}'):8888"
echo ""
