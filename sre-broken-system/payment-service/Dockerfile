# Build stage installs TypeScript tooling, runs tests and emits JavaScript.
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json tsconfig.json ./
RUN npm install
COPY src ./src
COPY tests ./tests
RUN npm test && npm prune --omit=dev

# Runtime stage contains only compiled code and production dependencies.
FROM node:22-alpine
ARG VCS_REF=unknown
ARG VCS_SOURCE=local/payment-service
LABEL org.opencontainers.image.revision=$VCS_REF org.opencontainers.image.source=$VCS_SOURCE
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app/package.json ./
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
USER node
EXPOSE 8083
CMD ["node", "dist/src/index.js"]
