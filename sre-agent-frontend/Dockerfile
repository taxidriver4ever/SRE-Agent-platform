FROM node:22-alpine AS build

ARG VITE_AGENT_API_BASE_URL=""
ARG GIT_COMMIT=unknown
ENV VITE_AGENT_API_BASE_URL=${VITE_AGENT_API_BASE_URL} \
    VITE_UI_PREVIEW=false

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine

ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.source="https://github.com/taxidriver4ever/SRE-Agent-platform" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.title="sre-agent-frontend"

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["wget", "-q", "--spider", "http://127.0.0.1:8080/healthz"]
