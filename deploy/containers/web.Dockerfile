ARG NODE_IMAGE
ARG CADDY_IMAGE
FROM ${NODE_IMAGE} AS build
WORKDIR /app
COPY apps/web/package.json apps/web/package-lock.json apps/web/.npmrc ./apps/web/
WORKDIR /app/apps/web
RUN npm ci --ignore-scripts --no-audit
WORKDIR /app
COPY apps/web ./apps/web
COPY contracts ./contracts
COPY docs ./docs
COPY scripts/build_project_docs.mjs ./scripts/build_project_docs.mjs
COPY plugins/extensions ./plugins/extensions
WORKDIR /app/apps/web
RUN npm run build

FROM ${CADDY_IMAGE} AS web
RUN addgroup -g 10001 tracehunter && adduser -D -u 10001 -G tracehunter tracehunter \
    && mkdir -p /srv /data /config && chown -R 10001:10001 /srv /data /config
COPY --from=build /app/apps/web/dist/ /srv/
COPY deploy/Caddyfile /etc/caddy/Caddyfile
ENV SITE_ADDRESS=:8766 WEB_HOST=0.0.0.0 WEB_ROOT=/srv API_UPSTREAM=api:8767 \
    TRACE_HUNTER_AUTH_CONFIG=/run/secrets/web_auth XDG_DATA_HOME=/data XDG_CONFIG_HOME=/config
USER 10001:10001
EXPOSE 8766
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
