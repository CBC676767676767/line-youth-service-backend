FROM node:22-alpine AS frontend-build
WORKDIR /srv/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
# A LIFF ID is public client configuration, never a channel secret or access token.
ARG VITE_LIFF_ID=""
ENV VITE_LIFF_ID=${VITE_LIFF_ID}
RUN npm run build

FROM python:3.12-slim AS runtime
WORKDIR /srv/app
COPY pyproject.toml ./
COPY requirements.lock ./
COPY app ./app
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home youth && mkdir -p /data && chown youth:youth /data
COPY alembic.ini ./
COPY migrations ./migrations
COPY --from=frontend-build /srv/frontend/dist ./frontend/dist
ENV YOUTH_FRONTEND_DIST=/srv/app/frontend/dist
USER youth
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
