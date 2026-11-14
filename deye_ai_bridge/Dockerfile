FROM ghcr.io/home-assistant/base:3.24

RUN apk add --no-cache python3

WORKDIR /app
COPY update_flow.py maintenance.py managed_runtime.py maintenance.html image_recovery.py local_updates.py local_updates.html update_frontdoor.py bridge_child.py /control/
COPY private_reads.py ./
COPY automatic_control.py app.py charge_test.py test_suite.py charge20_test.py shadow_planning.py energy_accounting.py monthly_register.py modbus_telemetry.py index.html run.sh tesla_icon.b64 ./
RUN chmod a+x /app/run.sh

CMD [ "/app/run.sh" ]

