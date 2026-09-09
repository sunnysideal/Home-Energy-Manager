FROM python:3.13-alpine

ARG BUILD_VERSION
ARG BUILD_ARCH
LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="${BUILD_ARCH}"

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY launcher.py /app/launcher.py
# Copy component entry points explicitly into a stable runtime layout.
# This avoids relying on nested component source layout at runtime.
RUN mkdir -p /app/runtime/ashp_forecaster /app/runtime/home_forecaster /app/runtime/controller
COPY components/ashp_forecaster/app/main.py /app/runtime/ashp_forecaster/main.py
COPY components/ashp_forecaster/app/runner.py /app/runtime/ashp_forecaster/runner.py
COPY components/ashp_forecaster/app/dhw_model.py /app/runtime/ashp_forecaster/dhw_model.py
COPY components/ashp_forecaster/app/dhw_draw_detector.py /app/runtime/ashp_forecaster/dhw_draw_detector.py
COPY components/ashp_forecaster/app/dhw_passive_learner.py /app/runtime/ashp_forecaster/dhw_passive_learner.py
COPY components/ashp_forecaster/app/dhw_cycle_tracker.py /app/runtime/ashp_forecaster/dhw_cycle_tracker.py
COPY components/ashp_forecaster/app/dhw_cycle_learner.py /app/runtime/ashp_forecaster/dhw_cycle_learner.py
COPY components/ashp_forecaster/app/dhw_demand_learner.py /app/runtime/ashp_forecaster/dhw_demand_learner.py
COPY components/ashp_forecaster/app/dhw_collector.py /app/runtime/ashp_forecaster/dhw_collector.py
COPY components/home_forecaster/app/main.py /app/runtime/home_forecaster/main.py
COPY components/controller/app.py /app/runtime/controller/app.py
COPY components/controller/forecast_only.py /app/runtime/controller/forecast_only.py
COPY common /app/common
COPY run.sh /run.sh
RUN chmod a+x /run.sh /app/launcher.py

CMD ["/run.sh"]
