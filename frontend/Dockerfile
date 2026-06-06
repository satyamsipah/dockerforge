# ── Stage 1: Build ────────────────────────────────────────────────────────────
FROM node:20-alpine AS build

WORKDIR /app

# Install deps first so this layer is cached separately from source changes
COPY package*.json ./
RUN npm ci --silent

# Build args are baked into the JS bundle at build time by Vite.
# In the composed stack the frontend calls /api/* relative to its own origin,
# so VITE_API_BASE_URL is intentionally left empty (nginx proxies /api).
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

COPY . .
RUN npm run build

# ── Stage 2: Serve ────────────────────────────────────────────────────────────
FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
