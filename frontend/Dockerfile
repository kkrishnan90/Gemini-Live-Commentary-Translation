FROM node:22-slim AS build
WORKDIR /w
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
# The official image renders /etc/nginx/templates/*.template with envsubst at start-up
# (only variables that exist in the environment are replaced, so nginx's own $vars survive).
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /w/dist /usr/share/nginx/html
ENV PORT=8080
