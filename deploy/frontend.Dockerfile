FROM nginx:1.28-alpine
COPY deploy/nginx.container.conf /etc/nginx/conf.d/default.conf
COPY thccb-frontend/dist/ /usr/share/nginx/html/
RUN mkdir -p /var/cache/nginx/thccb && chown -R nginx:nginx /var/cache/nginx/thccb
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1
EXPOSE 80
