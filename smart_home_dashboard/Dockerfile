ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3
COPY app.py index.html run.sh /app/
RUN chmod a+x /app/run.sh
CMD ["/app/run.sh"]
