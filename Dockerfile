FROM python:3.12-slim
WORKDIR /app
# Scanner is stdlib-only (urllib fallback); bake it so Railway self-refreshes without Harbor.
COPY serve.py ui.html scan_gc_radar.py ./
COPY data/ ./data/
# Do NOT COPY out/*.json — runtime data lives on volume / Harbor /api/sync
RUN mkdir -p out narrative
ENV PORT=8080 HOST=0.0.0.0 RAILWAY=1 OTR_SCHEDULER=1
EXPOSE 8080
CMD ["python", "serve.py"]
