FROM python:3.13-alpine

ARG BUILD_VERSION
ARG BUILD_ARCH
ENV HOME_ENERGY_MANAGER_VERSION="${BUILD_VERSION}"
LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="${BUILD_ARCH}"

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY launcher.py /app/launcher.py
COPY config_migration.py /app/config_migration.py
COPY public_log.py /app/public_log.py
COPY public_log_runner.py /app/public_log_runner.py
# Copy component entry points explicitly into a stable runtime layout.
RUN mkdir -p /app/runtime/ashp_forecaster /app/runtime/home_forecaster /app/runtime/axle /app/runtime/controller
COPY components/ashp_forecaster/app/main.py /app/runtime/ashp_forecaster/main.py
COPY components/ashp_forecaster/app/active_dd_training.py /app/runtime/ashp_forecaster/active_dd_training.py
COPY components/ashp_forecaster/app/forecast_runner.py /app/runtime/ashp_forecaster/forecast_runner.py
COPY components/ashp_forecaster/app/runner.py /app/runtime/ashp_forecaster/runner.py
COPY components/ashp_forecaster/app/dhw_model.py /app/runtime/ashp_forecaster/dhw_model.py
COPY components/ashp_forecaster/app/dhw_simulator.py /app/runtime/ashp_forecaster/dhw_simulator.py
COPY components/ashp_forecaster/app/dhw_draw_detector.py /app/runtime/ashp_forecaster/dhw_draw_detector.py
COPY components/ashp_forecaster/app/dhw_passive_learner.py /app/runtime/ashp_forecaster/dhw_passive_learner.py
COPY components/ashp_forecaster/app/dhw_cycle_tracker.py /app/runtime/ashp_forecaster/dhw_cycle_tracker.py
COPY components/ashp_forecaster/app/dhw_cycle_learner.py /app/runtime/ashp_forecaster/dhw_cycle_learner.py
COPY components/ashp_forecaster/app/dhw_efficiency_learner.py /app/runtime/ashp_forecaster/dhw_efficiency_learner.py
COPY components/ashp_forecaster/app/dhw_startup_learning.py /app/runtime/ashp_forecaster/dhw_startup_learning.py
COPY components/ashp_forecaster/app/dhw_demand_learner.py /app/runtime/ashp_forecaster/dhw_demand_learner.py
COPY components/ashp_forecaster/app/dhw_history_bootstrap.py /app/runtime/ashp_forecaster/dhw_history_bootstrap.py
COPY components/ashp_forecaster/app/dhw_shadow_forecast.py /app/runtime/ashp_forecaster/dhw_shadow_forecast.py
COPY components/ashp_forecaster/app/dhw_shadow_runner.py /app/runtime/ashp_forecaster/dhw_shadow_runner.py
COPY components/ashp_forecaster/app/dhw_validation.py /app/runtime/ashp_forecaster/dhw_validation.py
COPY components/ashp_forecaster/app/dhw_validation_runner.py /app/runtime/ashp_forecaster/dhw_validation_runner.py
COPY components/ashp_forecaster/app/dhw_diagnostics_runner.py /app/runtime/ashp_forecaster/dhw_diagnostics_runner.py
COPY components/ashp_forecaster/app/dhw_state_validation.py /app/runtime/ashp_forecaster/dhw_state_validation.py
COPY components/ashp_forecaster/app/dhw_collector.py /app/runtime/ashp_forecaster/dhw_collector.py
COPY components/ashp_forecaster/app/dhw_production_selector.py /app/runtime/ashp_forecaster/dhw_production_selector.py
COPY components/home_forecaster/app/main.py /app/runtime/home_forecaster/main.py
COPY components/home_forecaster/app/axle_pricing_runner.py /app/runtime/home_forecaster/axle_pricing_core.py
COPY components/home_forecaster/app/health_runtime.py /app/runtime/home_forecaster/axle_pricing_runner.py
COPY components/axle/main.py /app/runtime/axle/main.py
# Preserve the established runtime entrypoint contract, keep a compatibility
# controller core, then compose extracted responsibilities and policy layers.
COPY components/controller/controller_legacy_core.py /app/runtime/controller/controller_legacy_core.py
COPY components/controller/app.py /app/runtime/controller/app_core.py
COPY components/controller/controller_discovery.py /app/runtime/controller/controller_discovery.py
COPY components/controller/controller_export_generated.py /app/runtime/controller/controller_export_generated.py
COPY components/controller/controller_power_down.py /app/runtime/controller/controller_power_down.py
COPY components/controller/controller_utils.py /app/runtime/controller/controller_utils.py
COPY components/controller/controller_db.py /app/runtime/controller/controller_db.py
COPY components/controller/controller_ha.py /app/runtime/controller/controller_ha.py
COPY components/controller/minimise_export_runtime.py /app/runtime/controller/minimise_export_runtime.py
COPY components/controller/active_runtime.py /app/runtime/controller/app.py
COPY components/controller/forecast_only.py /app/runtime/controller/forecast_only.py
COPY components/controller/axle_only.py /app/runtime/controller/axle_only_core.py
COPY components/controller/axle_only_runtime.py /app/runtime/controller/axle_only.py
COPY common /app/common
COPY run.sh /run.sh
RUN chmod a+x /run.sh /app/launcher.py

CMD ["/run.sh"]
