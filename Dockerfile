FROM python:3.12-slim
WORKDIR /app
COPY serve.py ui.html ./
# Do NOT COPY out/ — runtime data arrives via /api/sync (deploy must not clobber)
RUN mkdir -p out narrative
ENV PORT=8080 HOST=0.0.0.0 RAILWAY=1
EXPOSE 8080
CMD ["python", "serve.py"]
