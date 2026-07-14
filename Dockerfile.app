# Fast rebuild — just copies updated app files on top of compiled base
# HandBrake is already compiled in the base image on DockerHub
FROM danthewhale/handbrake:v0.1
WORKDIR /app
COPY server.py .
COPY static/ ./static/
