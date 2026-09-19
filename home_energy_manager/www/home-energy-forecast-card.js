class HomeEnergyForecastCard extends HTMLElement {
  constructor() {
    super();

    this.attachShadow({ mode: 'open' });

    this._config = {
      title: 'Home Energy Forecast',
      rows: 16,
      edit_entity: 'text.home_energy_manager_tariff_edit',
      result_entity: 'sensor.home_energy_manager_tariff_edit_result'
    };

    this._hass = null;

    this._lastEntity = null;
    this._lastEntityId = null;
    this._lastRenderedConfigKey = null;

    this._initialised = false;

    this._titleEl = null;
    this._summaryEl = null;
    this._rowsEl = null;
    this._errorEl = null;
    this._pendingEdit = null;
    this._editTimer = null;
    this._editDialog = null;
    this._lastResult = null;
  }

  static getConfigForm() {
    return {
      schema: [
        {
          name: 'entity',
          required: true,
          selector: {
            entity: {
              domain: 'sensor'
            }
          }
        },
        {
          name: 'title',
          selector: {
            text: {}
          }
        },
        { name: 'edit_entity', selector: { entity: { domain: 'text' } } },
        { name: 'result_entity', selector: { entity: { domain: 'sensor' } } },
        {
          name: 'rows',
          selector: {
            number: {
              min: 1,
              max: 96,
              step: 1,
              mode: 'box'
            }
          }
        }
      ],

      computeLabel: (schema) => {
        const labels = {
          entity: 'Forecast entity',
          title: 'Title',
          rows: 'Number of rows',
          edit_entity: 'Tariff edit command entity',
          result_entity: 'Tariff edit result sensor'
        };

        return labels[schema.name] || schema.name;
      }
    };
  }

  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error('Please define an entity');
    }

    this._config = {
      title: 'Home Energy Forecast',
      rows: 16,
      edit_entity: 'text.home_energy_manager_tariff_edit',
      result_entity: 'sensor.home_energy_manager_tariff_edit_result',
      ...config
    };

    this._ensureStructure();

    // Force one redraw after config changes.
    this._lastRenderedConfigKey = null;

    if (this._hass) {
      this._updateCard(true);
    }
  }

  set hass(hass) {
    this._hass = hass;

    this._ensureStructure();
    this._updateCard(false);
  }

  connectedCallback() {
    this._ensureStructure();

    if (this._hass) {
      this._updateCard(true);
    }
  }

  getCardSize() {
    const rows = Number(this._config?.rows) || 16;

    return Math.max(
      4,
      Math.ceil(rows / 4)
    );
  }

  _configKey() {
    return [
      this._config?.entity || '',
      this._config?.title || '',
      Number(this._config?.rows) || 16,
      this._config?.edit_entity || '',
      this._config?.result_entity || ''
    ].join('|');
  }

  _ensureStructure() {
    if (
      this._initialised ||
      !this.shadowRoot
    ) {
      return;
    }

    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          container-type: inline-size;

          --hef-font-size: 13px;
          --hef-cell-x: 5px;
          --hef-cell-y: 4px;

          --hef-columns:
            1.00fr
            0.2fr
            1.0fr
            1fr
            .95fr
            1.55fr
            0.90fr
            1.10fr;
        }

        ha-card {
          overflow: hidden;
        }

        .header {
          display: flex;
          align-items: baseline;
          justify-content: space-between;
          gap: 10px;
          padding: 14px 16px 8px;
        }

        .title {
          font-size: 16px;
          font-weight: 500;
          min-width: 0;
        }

        .summary {
          font-size: 12px;
          color: var(--secondary-text-color);
          white-space: nowrap;
        }

        .table-wrap,
        .forecast-grid {
          width: 100%;
        }

        .table-wrap {
          overflow: hidden;
        }

        .forecast-grid {
          font-size: var(--hef-font-size);
          line-height: 1.25;
        }

        .grid-row {
          display: grid;

          border-top:
            1px solid
            var(
              --divider-color,
              rgba(127,127,127,.2)
            );

          grid-template-columns:
            var(--hef-columns);

          align-items: center;
          width: 100%;
        }

        .grid-cell {
          box-sizing: border-box;
          min-width: 0;

          padding:
            var(--hef-cell-y)
            var(--hef-cell-x);

          overflow: hidden;
          white-space: nowrap;
        }

        .grid-header .grid-cell {
          color:
            var(--secondary-text-color);

          font-weight: 500;
          text-align: right;
        }

        .time,
        .hp {
          text-align: center !important;
        }

        .num,
        .soc,
        .time {
          font-variant-numeric:
            tabular-nums;

          font-feature-settings:
            "tnum" 1;
        }

        .num,
        .soc {
          text-align: right;
        }

        .decimal {
          display: inline-grid;

          grid-template-columns:
            .65ch
            minmax(1ch, auto)
            .45ch
            2ch;

          justify-content: end;
          align-items: baseline;
          column-gap: 0;
          max-width: 100%;
        }

        .decimal .sign {
          text-align: center;
        }

        .decimal .int {
          text-align: right;
        }

        .decimal .dot {
          text-align: center;
        }

        .decimal .frac {
          text-align: left;
        }

        .pence {
          display: inline-grid;

          grid-template-columns:
            .65ch
            minmax(1.5ch, auto)
            1ch;

          justify-content: end;
          align-items: baseline;
          column-gap: 0;
        }

        .pence .sign {
          text-align: center;
        }

        .pence .int {
          text-align: right;
        }

        .pence .unit {
          text-align: left;
        }

        .soc-grid {
          width: 100%;
          display: grid;

          grid-template-columns:
            34px
            minmax(2ch, 1fr)
            1ch;

          align-items: center;
          column-gap: 1px;
        }

        .soc-direction {
          display: inline-grid;

          grid-template-columns:
            17px
            17px;

          align-items: center;
          justify-content: start;
          color: currentColor;
        }

        .battery-icon {
          --mdc-icon-size: 16px;

          width: 16px;
          height: 16px;
          justify-self: center;
          color: currentColor;
        }

        .soc-arrow {
          width: 17px;
          text-align: center;
          justify-self: center;
          align-self: center;
          line-height: 1;
          font-size: 1.15em;
          font-weight: 600;
          color: currentColor;
        }

        .soc-number {
          text-align: right;
        }

        .soc-unit {
          text-align: left;
        }

        .hp {
          padding-left: 0;
          padding-right: 0;
        }

        .hp-symbol {
          display: inline-flex;
          width: 1.25em;
          height: 1.25em;
          align-items: center;
          justify-content: center;
          line-height: 1;
          font-size: 1.05em;
        }

        .hp-symbol.empty {
          opacity: 0;
        }

        .price { text-align: right; cursor: pointer; color: var(--primary-color); }
        .price:focus-visible { outline: 2px solid var(--primary-color); outline-offset: -2px; }
        .price-override { font-weight: 700; text-decoration: underline dotted; }
        dialog { color: var(--primary-text-color); background: var(--card-background-color, white); border: 1px solid var(--divider-color); border-radius: 10px; padding: 18px; max-width: min(90vw, 360px); }
        dialog::backdrop { background: rgba(0,0,0,.45); }
        dialog input { width: 100%; box-sizing: border-box; margin: 12px 0; padding: 8px; font: inherit; }
        dialog .actions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }
        dialog button { padding: 7px 10px; cursor: pointer; }
        dialog .message { color: var(--error-color); min-height: 1.2em; }
        .load {
          color:
            var(--primary-text-color);
        }

        .pv-active {
          color:
            var(--warning-color, #f9a825);
        }

        .soc-low {
          color:
            var(--error-color, #db4437);
        }

        .soc-normal {
          color:
            var(--primary-text-color);
        }

        .soc-high {
          color:
            var(--success-color, #43a047);
        }

        .battery-charge .soc-direction {
          color:
            var(--success-color, #43a047);
        }

        .battery-discharge .soc-direction {
          color:
            var(--warning-color, #f9a825);
        }

        .battery-stable .soc-direction {
          color: currentColor;
        }

        .cost-charge {
          color:
            var(--error-color, #db4437);
        }

        .cost-credit {
          color:
            var(--success-color, #43a047);
        }

        .dhw {
          color:
            var(--info-color, #039be5);
        }

        .ch {
          color:
            var(--warning-color, #f9a825);
        }

        .error {
          padding: 16px;
          color:
            var(--error-color);
        }

        @container (max-width: 520px) {
          :host {
            --hef-font-size: 11.5px;
            --hef-cell-x: 2px;
            --hef-cell-y: 3px;

            --hef-columns:
              1.05fr
              .9fr
              1.25fr
              .95fr
              .95fr
              1.55fr
              .85fr
              1.05fr;
          }

          .header {
            padding: 11px 10px 6px;
          }

          .title {
            font-size: 14px;
          }

          .summary {
            font-size: 11px;
          }

          .soc-grid {
            grid-template-columns:
              28px
              minmax(2ch, 1fr)
              1ch;

            column-gap: 0;
          }

          .soc-direction {
            grid-template-columns:
              14px
              14px;
          }

          .battery-icon {
            --mdc-icon-size: 14px;

            width: 14px;
            height: 14px;
          }

          .soc-arrow {
            width: 14px;
            font-size: 1.1em;
          }
        }

        @container (max-width: 390px) {
          :host {
            --hef-font-size: 10.5px;
            --hef-cell-x: 1px;

            --hef-columns:
              1.05fr
              .9fr
              1.30fr
              .90fr
              .95fr
              1.55fr
              .80fr
              1fr;
          }

          .header {
            padding-left: 8px;
            padding-right: 8px;
          }

          .summary {
            display: none;
          }
        }
      </style>

      <ha-card>

        <div class="header">

          <div class="title"></div>

          <div class="summary"></div>

        </div>

        <div class="table-wrap">

          <div class="forecast-grid">

            <div class="grid-row grid-header">

              <div class="grid-cell time">
                Time
              </div>

              <div class="grid-cell hp">
                HP
              </div>

              <div class="grid-cell">
                Load
              </div>

              <div class="grid-cell">
                PV
              </div>

              <div class="grid-cell">
                Price
              </div>

              <div class="grid-cell">
                SoC
              </div>

              <div class="grid-cell">
                Cost
              </div>

              <div class="grid-cell">
                Total
              </div>

            </div>

            <div class="forecast-rows"></div>

          </div>

        </div>

      </ha-card>
      <dialog class="price-dialog"><div class="edit-title"></div><label>Import price (p/kWh)<input type="number" step="0.0001" min="0" max="10000" required></label><div class="message" role="status"></div><div class="actions"><button type="button" class="restore">Restore supplier price</button><button type="button" class="cancel">Cancel</button><button type="button" class="save">Save</button></div></dialog>
    `;

    this._titleEl =
      this.shadowRoot.querySelector('.title');

    this._summaryEl =
      this.shadowRoot.querySelector('.summary');

    this._rowsEl =
      this.shadowRoot.querySelector('.forecast-rows');

    this._editDialog = this.shadowRoot.querySelector('.price-dialog');
    this._editDialog.querySelector('.cancel').addEventListener('click', () => this._editDialog.close());
    this._editDialog.querySelector('.save').addEventListener('click', () => this._submitPrice(false));
    this._editDialog.querySelector('.restore').addEventListener('click', () => this._submitPrice(true));
    this._rowsEl.addEventListener('click', event => { const cell = event.target.closest('.price[data-start]'); if (cell) this._openPrice(cell); });
    this._rowsEl.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { const cell = event.target.closest('.price[data-start]'); if (cell) { event.preventDefault(); this._openPrice(cell); } } });
    this._initialised = true;
  }

  _updateCard(force = false) {
    if (
      !this._hass ||
      !this._config?.entity ||
      !this._rowsEl
    ) {
      return;
    }

    const entityId = this._config.entity;

    const entity =
      this._hass.states[entityId];

    const configKey =
      this._configKey();

    const result = this._hass.states[this._config.result_entity];
    if (result !== this._lastResult) { this._lastResult = result; this._handlePriceResult(result); }

    const entityChanged =
      entity !== this._lastEntity ||
      entityId !== this._lastEntityId;

    const configChanged =
      configKey !== this._lastRenderedConfigKey;

    if (
      !force &&
      !entityChanged &&
      !configChanged
    ) {
      return;
    }

    this._lastEntity = entity;
    this._lastEntityId = entityId;
    this._lastRenderedConfigKey = configKey;

    this._updateTitle();

    if (!entity) {
      this._summaryEl.textContent = '';

      this._rowsEl.innerHTML = `
        <div class="error">
          Entity not found: ${entityId}
        </div>
      `;

      return;
    }

    this._updateForecast(entity);
  }

  _updateTitle() {
    if (!this._titleEl) {
      return;
    }

    this._titleEl.textContent =
      this._config.title ||
      'Home Energy Forecast';
  }

  _updateForecast(entity) {
    const allForecast =
      Array.isArray(
        entity.attributes?.forecast
      )
        ? entity.attributes.forecast
        : [];

    const configuredRows =
      Number(this._config.rows);

    const rowCount =
      Number.isFinite(configuredRows) &&
      configuredRows > 0
        ? Math.floor(configuredRows)
        : 16;

    const forecast =
      allForecast.slice(0, rowCount);

    const overnight =
      entity.attributes?.overnight_start_soc;

    if (this._summaryEl) {
      this._summaryEl.textContent =
        Number.isFinite(Number(overnight))
          ? `Overnight ${Number(overnight).toFixed(0)}%`
          : '';
    }

    if (!forecast.length) {
      this._rowsEl.innerHTML = `
        <div class="error">
          No forecast data
        </div>
      `;

      return;
    }

    const todayActualCost =
      Number(
        entity.attributes?.today?.actual?.cost_p
      );

    let runningCost =
      Number.isFinite(todayActualCost)
        ? todayActualCost
        : 0;

    this._rowsEl.innerHTML =
      forecast
        .map((slot, i) => {
          const next =
            forecast[i + 1];
          const price = Number(slot.import_rate_p);
          const override = (entity.attributes?.manual_import_overrides || []).some(item => item.start === slot.start);

          const soc =
            Number(slot.soc);

          const arrow =
            next
              ? this._socArrow(
                  slot.soc,
                  next.soc
                )
              : '';

          const slotCost =
            Number(slot.cost_p);

          if (
            Number.isFinite(slotCost)
          ) {
            runningCost += slotCost;
          }

          const socClass =
            Number.isFinite(soc)
              ? soc <= 10
                ? 'soc-low'
                : soc >= 90
                  ? 'soc-high'
                  : 'soc-normal'
              : 'soc-normal';

          const directionClass =
            arrow === '↑'
              ? 'battery-charge'
              : arrow === '↓'
                ? 'battery-discharge'
                : 'battery-stable';

          const pv =
            Number(slot.pv_kwh);

          const pvClass =
            Number.isFinite(pv) &&
            Number(pv.toFixed(2)) !== 0
              ? 'pv-active'
              : '';

          const roundedCost =
            Number.isFinite(slotCost)
              ? Math.round(slotCost)
              : 0;

          const costClass =
            roundedCost < 0
              ? 'cost-credit'
              : roundedCost > 0
                ? 'cost-charge'
                : '';

          const totalClass =
            runningCost < -0.5
              ? 'cost-credit'
              : runningCost > 0.5
                ? 'cost-charge'
                : '';

          return `
            <div class="grid-row">

              <div class="grid-cell time">
                ${this._time(slot.start)}
              </div>

              <div class="grid-cell hp">
                ${this._hpMarkup(slot)}
              </div>

              <div class="grid-cell num load">
                ${this._decimalMarkup(
                  slot.load_kwh,
                  2
                )}
              </div>

              <div class="grid-cell num pv ${pvClass}">
                ${this._pvMarkup(
                  slot.pv_kwh
                )}
              </div>

              <div class="grid-cell price ${override ? 'price-override' : ''}" role="button" tabindex="0" data-start="${slot.start}" data-price="${Number.isFinite(price) ? price : ''}" title="Edit import price">${Number.isFinite(price) ? price.toFixed(2) + 'p' : '—'}</div>

              <div class="grid-cell soc ${socClass} ${directionClass}">
                ${this._socMarkup(
                  slot.soc,
                  arrow
                )}
              </div>

              <div class="grid-cell num cost ${costClass}">
                ${this._penceMarkup(
                  slot.cost_p,
                  true
                )}
              </div>

              <div class="grid-cell num total ${totalClass}">
                ${this._penceMarkup(
                  runningCost,
                  false
                )}
              </div>

            </div>
          `;
        })
        .join('');
  }

  _openPrice(cell) {
    if (this._pendingEdit?.saving) return;
    const dialog = this._editDialog;
    dialog.dataset.start = cell.dataset.start;
    dialog.querySelector('.edit-title').textContent = 'Import price: ' + this._time(cell.dataset.start);
    dialog.querySelector('input').value = cell.dataset.price;
    dialog.querySelector('.message').textContent = '';
    dialog.querySelector('.restore').disabled = !cell.dataset.price;
    dialog.showModal();
  }

  async _submitPrice(restore) {
    const dialog = this._editDialog;
    const input = dialog.querySelector('input');
    const rate = restore ? null : Number(input.value);
    if (!restore && (!input.value.trim() || !Number.isFinite(rate) || rate < 0 || rate > 10000)) {
      dialog.querySelector('.message').textContent = 'Enter a valid price in p/kWh.';
      return;
    }
    if (!this._hass.states[this._config.edit_entity]) {
      dialog.querySelector('.message').textContent = 'Tariff edit entity is unavailable.';
      return;
    }
    const id = 'price-' + Date.now() + '-' + Math.random().toString(36).slice(2);
    this._pendingEdit = { id, saving: true };
    dialog.querySelector('.message').textContent = 'Saving…';
    dialog.querySelectorAll('button').forEach(button => button.disabled = true);
    clearTimeout(this._editTimer);
    this._editTimer = setTimeout(() => this._finishPriceEdit('No response from tariff editor; check MQTT and add-on logs.'), 15000);
    try {
      await this._hass.callService('text', 'set_value', {
        entity_id: this._config.edit_entity,
        value: JSON.stringify({ id, start: dialog.dataset.start, rate_p: rate })
      });
    } catch (error) {
      this._finishPriceEdit(error?.message || 'Could not send tariff edit.');
    }
  }

  _finishPriceEdit(error) {
    clearTimeout(this._editTimer);
    this._editTimer = null;
    this._pendingEdit = null;
    const dialog = this._editDialog;
    if (!dialog) return;
    dialog.querySelectorAll('button').forEach(button => button.disabled = false);
    if (error) dialog.querySelector('.message').textContent = error;
    else if (dialog.open) dialog.close();
  }

  _handlePriceResult(result) {
    if (!this._pendingEdit || result?.attributes?.id !== this._pendingEdit.id) return;
    if (result.attributes.status === 'saved') this._finishPriceEdit(null);
    else if (result.attributes.status === 'error') this._finishPriceEdit(result.attributes.error || 'Tariff edit rejected.');
  }

  _time(value) {
    const d =
      new Date(value);

    if (
      Number.isNaN(d.getTime())
    ) {
      return '—';
    }

    return d.toLocaleTimeString(
      [],
      {
        hour: '2-digit',
        minute: '2-digit',
        hour12: false
      }
    );
  }

  _socArrow(current, next) {
    const a =
      Number(current);

    const b =
      Number(next);

    if (
      !Number.isFinite(a) ||
      !Number.isFinite(b)
    ) {
      return '';
    }

    if (b > a) {
      return '↑';
    }

    if (b < a) {
      return '↓';
    }

    return '→';
  }

  _batteryIcon(soc) {
    const n =
      Number(soc);

    if (!Number.isFinite(n)) {
      return 'mdi:battery-unknown';
    }

    if (n >= 95) return 'mdi:battery';
    if (n >= 85) return 'mdi:battery-90';
    if (n >= 75) return 'mdi:battery-80';
    if (n >= 65) return 'mdi:battery-70';
    if (n >= 55) return 'mdi:battery-60';
    if (n >= 45) return 'mdi:battery-50';
    if (n >= 35) return 'mdi:battery-40';
    if (n >= 25) return 'mdi:battery-30';
    if (n >= 15) return 'mdi:battery-20';
    if (n >= 5) return 'mdi:battery-10';

    return 'mdi:battery-outline';
  }

  _hpMarkup(slot) {
    const ch =
      Number(slot?.ch_kwh || 0);

    const dhw =
      Number(slot?.dhw_kwh || 0);

    if (dhw > 0) {
      return `
        <span
          class="hp-symbol dhw"
          title="DHW"
        >
          💧
        </span>
      `;
    }

    if (ch > 0) {
      return `
        <span
          class="hp-symbol ch"
          title="Space heating"
        >
          ♨️
        </span>
      `;
    }

    return `
      <span class="hp-symbol empty">
        &nbsp;
      </span>
    `;
  }

  _decimalMarkup(
    value,
    digits = 2
  ) {
    const n =
      Number(value);

    if (!Number.isFinite(n)) {
      return `
        <span class="decimal">
          <span></span>
          <span class="int">—</span>
          <span></span>
          <span></span>
        </span>
      `;
    }

    const [
      integer,
      fraction = ''
    ] =
      Math.abs(n)
        .toFixed(digits)
        .split('.');

    return `
      <span class="decimal">
        <span class="sign">
          ${n < 0 ? '−' : ''}
        </span>

        <span class="int">
          ${integer}
        </span>

        <span class="dot">
          ${digits ? '.' : ''}
        </span>

        <span class="frac">
          ${fraction}
        </span>
      </span>
    `;
  }

  _pvMarkup(value) {
    const n =
      Number(value);

    if (
      !Number.isFinite(n) ||
      Number(n.toFixed(2)) === 0
    ) {
      return '';
    }

    return this._decimalMarkup(
      n,
      2
    );
  }

  _penceMarkup(
    value,
    suppressZero = false
  ) {
    const n =
      Number(value);

    if (!Number.isFinite(n)) {
      return '—';
    }

    const rounded =
      Math.round(n);
    if (
      suppressZero &&
      rounded === 0
    ) {
      return '';
    }

    return `
      <span class="pence">

        <span class="sign">
          ${rounded < 0 ? '−' : ''}
        </span>

        <span class="int">
          ${Math.abs(rounded)}
        </span>

        <span class="unit">
          p
        </span>

      </span>
    `;
  }

  _socMarkup(value, arrow) {
    const n =
      Number(value);

    const shown =
      Number.isFinite(n)
        ? Math.round(n)
        : '—';

    return `
      <span class="soc-grid">

        <span class="soc-direction">

          <ha-icon
            class="battery-icon"
            icon="${this._batteryIcon(n)}">
          </ha-icon>

          <span class="soc-arrow">
            ${arrow || ''}
          </span>

        </span>

        <span class="soc-number">
          ${shown}
        </span>

        <span class="soc-unit">
          %
        </span>

      </span>
    `;
  }
}

if (
  !customElements.get(
    'home-energy-forecast-card'
  )
) {
  customElements.define(
    'home-energy-forecast-card',
    HomeEnergyForecastCard
  );
}

window.customCards =
  window.customCards || [];

if (
  !window.customCards.some(
    card =>
      card.type ===
      'home-energy-forecast-card'
  )
) {
  window.customCards.push({
    type:
      'home-energy-forecast-card',

    name:
      'Home Energy Forecast Card',

    description:
      'Responsive Home Energy Forecaster table'
  });
}