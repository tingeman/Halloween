# server/dashboard/Dockerfile
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY server/dashboard/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the runtime modules we need (no build/install step)
# Keep layout similar to dashboard: copy module files into /app
COPY server/dashboard/app.py ./app.py
COPY server/dashboard/mqtt_service.py ./mqtt_service.py
COPY server/dashboard/plugin_loader.py ./plugin_loader.py
COPY server/dashboard/jarvis ./jarvis
COPY server/dashboard/assets ./assets
COPY server/dashboard/plugin_base /opt/plugin_base

# Not needed since we include it on the PYTHONPATH in docker-compose.override.yml
#RUN python -m pip install --no-cache-dir /opt/plugin_base

# Built-in plugins shipped with the image
COPY server/dashboard/builtin_plugins ./builtin_plugins

EXPOSE 8050
CMD ["python", "app.py"]
