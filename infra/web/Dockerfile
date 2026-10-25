# A10 production web image. Build context: repository root.
#   docker build -f infra/web/Dockerfile .
#
# Stage 1 builds apps/web in LIVE mode (vite.config.ts refuses fixture mode in
# a build) and runs its production-bundle guard, which needs the compact
# fixture from data/generator to prove no fixture value leaked into dist/.
# Stage 2 is nginx serving dist/ only and reverse-proxying /api and /health to
# the API, so the browser talks to ONE origin (no CORS, no API URL baked in).
#
# (apps/web/Dockerfile, lane B's, builds with apps/web as context and so cannot
# see ../../data/generator; it is not used for deployment.)
FROM node:20.18.1-slim AS build
WORKDIR /repo/apps/web
COPY apps/web/package.json apps/web/package-lock.json apps/web/.npmrc ./
RUN npm ci
COPY apps/web/ ./
COPY data/generator/fixtures/compact_v1 /repo/data/generator/fixtures/compact_v1
# Public, build-time values only (compiled into the bundle). Empty origin =
# same origin, which is what the nginx proxy below provides.
ARG VITE_ENCORE_API_ORIGIN=
ARG VITE_ENCORE_AUTH_LOGIN_URL=
ENV VITE_ENCORE_DATA_MODE=live \
    VITE_ENCORE_API_ORIGIN=${VITE_ENCORE_API_ORIGIN} \
    VITE_ENCORE_AUTH_LOGIN_URL=${VITE_ENCORE_AUTH_LOGIN_URL}
RUN npm run build

FROM nginx:1.27.3-alpine AS serve
COPY infra/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /repo/apps/web/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/nginx-health || exit 1
