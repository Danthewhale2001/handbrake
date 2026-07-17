#!/bin/bash
# ============================================================
# build.sh — Run this ONLY when you want to update HandBrake
#             or set up for the first time.
#             Takes 20-40 minutes.
# ============================================================

HB_VERSION="1.11.2"  # Change this to update HandBrake version

echo ""
echo "  Building HandBrake ${HB_VERSION} from source..."
echo "  This will take 20-40 minutes. Go make a cup of tea!"
echo ""

docker build \
  --build-arg HB_VERSION=${HB_VERSION} \
  -t handbrake-mobile:base \
  -f Dockerfile .

if [ $? -eq 0 ]; then
  echo ""
  echo "  Build complete! HandBrake ${HB_VERSION} is ready."
  echo "  Now run ./start.sh to launch the app."
  echo ""
else
  echo ""
  echo "  Build failed. Check the output above for errors."
  echo ""
  exit 1
fi
