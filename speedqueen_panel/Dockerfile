ARG BUILD_FROM
FROM $BUILD_FROM

# numpy and pillow come from apk — building them from source on a Pi is slow.
# paho-mqtt is pure Python and not reliably packaged, so it comes from pip.
RUN apk add --no-cache python3 py3-pip py3-numpy py3-pillow \
    && pip3 install --no-cache-dir --break-system-packages "paho-mqtt>=1.6,<3"

COPY decoder.py /decoder.py
COPY sq-calibrate.html /sq-calibrate.html
COPY run.sh /run.sh
RUN chmod a+x /run.sh

LABEL io.hass.type="addon"

CMD ["/run.sh"]
