ARG BUILD_ARCH
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base:3.22
ARG BUILD_VERSION
ARG BUILD_ARCH
LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="${BUILD_ARCH}"
RUN apk add --no-cache python3
COPY run.sh /run.sh
COPY app /app
RUN chmod a+x /run.sh
CMD ["/run.sh"]
