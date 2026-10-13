FROM node:22-alpine

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

EXPOSE 5173
# Dev server inside the container; bind to all interfaces so the published
# 127.0.0.1:5173 port mapping in docker-compose.yml can reach it.
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
