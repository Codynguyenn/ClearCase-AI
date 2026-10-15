FROM node:22-alpine

WORKDIR /app
# The image ships an unprivileged "node" user; the dev server has no need to run as root.
RUN chown node:node /app
USER node

COPY --chown=node:node package.json package-lock.json ./
RUN npm ci

COPY --chown=node:node . .

EXPOSE 5173
# Healthy = the dev server answers (busybox wget is built into alpine).
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD wget -q --spider http://127.0.0.1:5173/ || exit 1
# Dev server inside the container; bind to all interfaces so the published
# 127.0.0.1:5173 port mapping in docker-compose.yml can reach it.
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
