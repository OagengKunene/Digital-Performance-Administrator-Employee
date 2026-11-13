FROM python:3.12-slim-bookworm
WORKDIR /app
COPY requirements.lock.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY pae ./pae
COPY ui ./ui
COPY scripts ./scripts
RUN pip install --no-deps . && useradd --create-home app && mkdir .local && chown -R app:app /app
USER app
EXPOSE 8000 8501
CMD ["python", "scripts/start.py"]
