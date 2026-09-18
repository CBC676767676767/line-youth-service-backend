FROM python:3.12-slim
WORKDIR /srv/app
COPY pyproject.toml ./
COPY requirements.lock ./
COPY app ./app
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home youth && mkdir -p /data && chown youth:youth /data
COPY alembic.ini ./
COPY migrations ./migrations
USER youth
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
