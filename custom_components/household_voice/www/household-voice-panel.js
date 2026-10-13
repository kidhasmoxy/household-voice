/* Bundled management panel. Data and actions use authenticated Home Assistant. */
class HouseholdVoiceManagerCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity) throw new Error("Set the Household Voice schedules sensor entity");
    this.config = config;
    if (!this.shadowRoot) this.attachShadow({mode: "open"});
    this.build();
  }
  set hass(value) { this._hass = value; this.renderRows(); }
  connectedCallback() { this.tick = setInterval(() => this.updateCountdowns(), 1000); }
  disconnectedCallback() { clearInterval(this.tick); }
  getCardSize() { return 7; }
  static getStubConfig() { return {entity: "sensor.household_voice_schedules"}; }
  build() {
    this.roomChoicesKey = null;
    this.shadowRoot.innerHTML = `<style>
      ha-card{display:block;padding:20px;background:var(--card-background-color);border-radius:var(--ha-card-border-radius,16px)} @media(max-width:600px){ha-card{padding:14px}form>label{flex:1 1 130px}input,select{min-width:0;max-width:100%;box-sizing:border-box}fieldset{flex-wrap:wrap}} h2{margin:0 0 8px;font-size:22px} .muted{color:var(--secondary-text-color)}
      form,.controls{display:flex;flex-wrap:wrap;gap:10px} form{margin:16px 0} label{display:flex;flex-direction:column;gap:4px;font-size:13px}
      label.check{flex-direction:row;align-items:center} fieldset{border:0;padding:0;display:flex;gap:8px;align-items:center} fieldset legend{font-size:13px;margin-bottom:6px} input,select,button{font:inherit;padding:9px;border:1px solid var(--divider-color);border-radius:8px;background:var(--card-background-color);color:var(--primary-text-color)}
      button{cursor:pointer} button:disabled{opacity:.45;cursor:wait} .row{border-top:1px solid var(--divider-color);padding:14px 0}.title{font-weight:600}.details{margin:5px 0 10px} .error{color:var(--error-color)} [hidden]{display:none!important}
    </style><ha-card><h2>Household timers & alarms</h2><div id="mode" class="muted"></div>
    <form><label>Type<select name="kind"><option value="timer">Timer</option><option value="alarm">Alarm</option></select></label>
    <label>Label<input name="label" required maxlength="120" placeholder="Pasta"></label>
    <label id="source_room_label">Source room<input name="source_room" required maxlength="120" placeholder="Choose exact room ID"></label>
    <label id="target_room_label">Ring room<input name="target_room" maxlength="120" placeholder="Same as source"></label>
    <label id="minutes">Minutes<input name="minutes" type="number" min="0.01" max="10080" step="any" value="5"></label>
    <label class="alarm" hidden>Time<input name="local_time" type="time" value="07:00"></label>
    <label class="alarm" hidden>Alarm timezone<input name="timezone"></label>
    <fieldset class="alarm" hidden><legend>Days</legend>${["Mon","Tue","Wed","Thu","Fri","Sat","Sun"].map((name,index)=>`<label class="check"><input name="day_${index}" type="checkbox" checked>${name}</label>`).join("")}</fieldset>
    <label class="alarm check" hidden><input name="one_shot" type="checkbox" checked>Only next occurrence</label>
    <button type="submit">Create</button></form><div id="status" role="status" aria-live="polite"></div><div id="rows"></div><div id="events" class="muted"></div></ha-card>`;
    const form = this.shadowRoot.querySelector("form");
    form.elements.timezone.value = Intl.DateTimeFormat().resolvedOptions().timeZone;
    form.elements.source_room.value = this.config.default_source_room || "";
    form.elements.kind.onchange = () => {
      const alarm = form.elements.kind.value === "alarm";
      this.shadowRoot.querySelector("#minutes").hidden = alarm;
      for (const node of this.shadowRoot.querySelectorAll(".alarm")) node.hidden = !alarm;
    };
    form.onsubmit = async (event) => {
      event.preventDefault();
      if (!form.reportValidity() || this.busy) return;
      const kind = form.elements.kind.value;
      const payload = {label: form.elements.label.value.trim(), source_room: form.elements.source_room.value.trim()};
      if (form.elements.target_room.value.trim()) payload.target_room = form.elements.target_room.value.trim();
      if (kind === "timer") payload.seconds = Number(form.elements.minutes.value) * 60;
      else Object.assign(payload, {local_time: form.elements.local_time.value, timezone: form.elements.timezone.value.trim(),
        weekdays: Array.from({length:7},(_,index)=>index).filter(index=>form.elements[`day_${index}`].checked), one_shot: form.elements.one_shot.checked});
      await this.call(`create_${kind}`, payload);
    };
    this.renderRows();
  }
  state() { return this._hass?.states[this.config?.entity]; }
  async call(service, payload) {
    const state = this.state();
    if (!state || ["unavailable", "unknown"].includes(state.state) || this.busy) return;
    this.busy = true; this.message("Sending…"); this.renderRows();
    try {
      await this._hass.callService("household_voice", service, {...payload, config_entry_id: state.attributes.config_entry_id, request_id: crypto.randomUUID()});
      this.message("Saved. The coordinator remains the authority; speaker delivery depends on its connected adapters.");
    } catch (error) { this.message(error.message || "Request failed. Refresh before retrying.", true); }
    finally { this.busy = false; this.renderRows(); }
  }
  message(text, error=false) {
    const node = this.shadowRoot.querySelector("#status"); node.textContent = text; node.className = error ? "error" : "muted";
  }
  detailText(row) {
    const remaining = row.state === "paused" ? row.remaining : row.due == null ? null : Math.max(0, Math.ceil(row.due - Date.now()/1000));
    const timing = row.kind === "timer" && remaining != null ? `${Math.floor(remaining/60)}m ${Math.floor(remaining%60)}s` : row.due ? new Date(row.due*1000).toLocaleString() : "No due time";
    const labels = {active: row.kind === "alarm" ? "Scheduled" : "Running", paused:"Paused", ringing:"Ringing", completed:"Completed", cancelled:"Cancelled", disabled:"Disabled", missed:"Missed"};
    const rooms = this.state()?.attributes.rooms || [];
    const roomName = id => rooms.find(room=>room.id===id)?.name || id;
    let spec = {}; try { spec = typeof row.spec === "string" ? JSON.parse(row.spec) : row.spec || {}; } catch (_) {}
    const zone = row.kind === "alarm" ? ` · displayed in ${Intl.DateTimeFormat().resolvedOptions().timeZone}${spec.timezone ? ` (alarm: ${spec.timezone})` : ""}` : "";
    return `${labels[row.state] || row.state} · ${roomName(row.source_room)} → ${roomName(row.target_room)} · ${timing}${zone}`;
  }
  updateRoomChoices(rooms) {
    const key = JSON.stringify(rooms);
    if (key === this.roomChoicesKey) return;
    this.roomChoicesKey = key;
    const form = this.shadowRoot.querySelector("form");
    if (!form) return;
    for (const name of ["source_room","target_room"]) {
      const previous = form.elements[name].value;
      const element = document.createElement(rooms.length ? "select" : "input");
      element.name = name; element.required = name === "source_room";
      if (rooms.length) {
        const blank = document.createElement("option"); blank.value=""; blank.textContent=name==="source_room"?"Choose a room":"Same as source"; element.append(blank);
        for (const room of rooms) { const option=document.createElement("option"); option.value=room.id; option.textContent=room.name; element.append(option); }
      } else { element.maxLength=120; element.placeholder=name==="source_room"?"Exact room ID":"Same as source"; }
      element.value = previous;
      form.elements[name].replaceWith(element);
    }
  }
  updateCountdowns() {
    // Preserve keyboard focus and pending clicks while only the clock changes.
    for (const node of this.shadowRoot?.querySelectorAll(".details") || []) node.textContent = this.detailText(node.schedule);
  }
  renderRows() {
    if (!this.shadowRoot?.querySelector("#rows")) return;
    const state = this.state();
    const available = state && !["unavailable", "unknown"].includes(state.state);
    const attrs = state?.attributes || {};
    const maintenanceFailed = attrs.maintenance_healthy === false || Boolean(attrs.maintenance_error);
    const mode = this.shadowRoot.querySelector("#mode");
    mode.textContent = !available ? "Coordinator unavailable. Controls are disabled." : maintenanceFailed ? `Scheduler needs attention${attrs.maintenance_error ? `: ${attrs.maintenance_error}` : ""}. Timers and alarms may not fire. Check the integration before relying on them.` : !attrs.ring_execution_enabled ? "Alarm audio is disabled. Schedules are saved." : !attrs.ring_worker_enabled ? "Alarm audio is configured, but this Home Assistant worker is disabled." : attrs.ring_worker_error ? `Alarm delivery needs attention: ${attrs.ring_worker_error}.` : "Alarm audio is enabled. Verify your selected speakers before relying on alarms.";
    mode.className = !available || maintenanceFailed || attrs.ring_worker_error ? "error" : "muted";
    this.shadowRoot.querySelector("form button").disabled = !available || this.busy;
    const root = this.shadowRoot.querySelector("#rows"); root.replaceChildren();
    if (!available) return;
    this.updateRoomChoices(state.attributes.rooms || []);
    const rows = state.attributes.schedules || [];
    if (!rows.length) { const empty = document.createElement("p"); empty.textContent = "No household schedules."; root.append(empty); }
    for (const row of rows) {
      const box = document.createElement("section"); box.className = "row";
      const title = document.createElement("div"); title.className = "title"; title.textContent = `${row.label} · ${row.kind}`; box.append(title);
      const details = document.createElement("div"); details.className = "details muted";
      details.schedule = row; details.textContent = this.detailText(row); box.append(details);
      const controls = document.createElement("div"); controls.className = "controls";
      const button = (label, operation, extra={}) => {
        const node = document.createElement("button"); node.type = "button"; node.textContent = label; node.disabled = this.busy;
        node.onclick = async () => {
          if (operation === "delete" && !window.confirm(`Delete only “${row.label}”?`)) return;
          if (operation === "rename") { const value = window.prompt("New schedule label", row.label); if (!value?.trim()) return; extra = {label: value.trim()}; }
          if (operation === "move") { const value = window.prompt("Exact destination room ID", row.target_room); if (!value?.trim()) return; extra = {target_room: value.trim()}; }
          await this.call("command", {identifier: row.id, expected_revision: row.revision, operation, ...extra});
        }; controls.append(node);
      };
      if (row.kind === "timer" && row.state === "active") button("Pause", "pause");
      if (row.state === "paused") button("Resume", "resume");
      if (row.kind === "timer" && ["active", "paused"].includes(row.state)) { button("+1 minute", "add", {seconds:60}); button("−1 minute", "add", {seconds:-60}); }
      if (row.state === "ringing") { button("Dismiss", "dismiss"); button("Snooze 5 min", "snooze", {seconds:300}); }
      if (row.kind === "alarm") { if (row.state === "disabled") button("Enable", "enable"); else if (["active", "ringing"].includes(row.state)) button("Disable", "disable"); }
      if (["active", "paused", "ringing"].includes(row.state)) button("Cancel", "cancel");
      button("Rename", "rename"); button("Move room", "move"); button("Delete", "delete");
      box.append(controls); root.append(box);
    }
    const events = state.attributes.events || [];
    this.shadowRoot.querySelector("#events").textContent = `Recent occurrences: ${events.slice(-5).map(event => event.status).join(", ") || "none"}. Countdown is approximate between polls.`;
  }
}
if (!customElements.get("household-voice-manager-card")) customElements.define("household-voice-manager-card", HouseholdVoiceManagerCard);

class HouseholdVoicePanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: "open"});
    this._registry = null;
    this._selected = null;
    this._registryGeneration = 0;
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;height:100%;overflow:auto;background:var(--primary-background-color);color:var(--primary-text-color)}
      header{display:flex;align-items:center;gap:8px;padding:12px 16px;background:var(--app-header-background-color);color:var(--app-header-text-color)}
      h1{font-size:20px;font-weight:500;margin:0}main{max-width:1000px;margin:0 auto;padding:20px;box-sizing:border-box}
      .settings{display:flex;align-items:center;flex-wrap:wrap;gap:14px;margin-bottom:18px}
      a{color:var(--primary-color);text-underline-offset:3px}label{display:flex;align-items:center;flex-wrap:wrap;gap:8px}
      select,button{font:inherit;padding:9px;border:1px solid var(--divider-color);border-radius:8px;background:var(--card-background-color);color:var(--primary-text-color)}
      p{line-height:1.5;color:var(--secondary-text-color)}[hidden]{display:none!important}
      @media(max-width:600px){main{padding:12px}.settings{align-items:flex-start;flex-direction:column}}
    </style><header><ha-menu-button></ha-menu-button><h1>Household Voice</h1></header><main>
      <div class="settings"><label id="selector-label" hidden>Household<select aria-label="Household schedules"></select></label>
      <a id="configure" href="/config/integrations/integration/household_voice" hidden>Configure speakers &amp; voice</a></div>
      <p id="guidance" hidden>Open the integration’s Configure menu to set speaker pairings, broadcast defaults, phone notifications and voice features.</p>
      <p id="status" role="status" aria-live="polite">Loading household schedules…</p>
      <button id="retry" hidden>Retry loading schedules</button><div id="card"></div>
    </main>`;
    this.shadowRoot.querySelector("select").onchange = event => {
      this._selected = event.target.value; this.render();
    };
    this.shadowRoot.querySelector("#retry").onclick = () => this.loadRegistry();
  }

  set hass(value) {
    const changedConnection = this._hass?.connection !== value?.connection || this._hass?.user?.id !== value?.user?.id;
    this._hass = value;
    if (changedConnection || !this._attempted) this.loadRegistry();
    this.render();
  }
  set narrow(value) { this._narrow = value; this.render(); }
  set panel(value) { this._panel = value; }
  connectedCallback() {
    if (this._hass) this.loadRegistry();
    // Refresh entity renames/removals while preserving form contents otherwise.
    this._refresh = setInterval(() => this.loadRegistry(), 60000);
  }
  disconnectedCallback() {
    clearInterval(this._refresh);
    this._registryGeneration++;
    this._loading = false;
  }
  async loadRegistry() {
    if (!this._hass?.callWS) return;
    this._attempted = true;
    const generation = ++this._registryGeneration;
    this._loading = true; this._error = false;
    try {
      const registry = await this._hass.callWS({type: "config/entity_registry/list"});
      if (generation !== this._registryGeneration) return;
      this._registry = registry.filter(row => row.platform === "household_voice" && row.entity_id.startsWith("sensor.") && !row.disabled_by);
    } catch (_) {
      if (generation !== this._registryGeneration) return;
      this._registry = null; this._error = true;
    } finally {
      if (generation === this._registryGeneration) { this._loading = false; this.render(); }
    }
  }
  render() {
    if (!this.shadowRoot || !this._hass) return;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    menu.hass = this._hass; menu.narrow = this._narrow;
    const admin = this._hass.user?.is_admin === true;
    this.shadowRoot.querySelector("#configure").hidden = !admin;
    this.shadowRoot.querySelector("#guidance").hidden = !admin;
    const rows = (this._registry || []).filter(row => {
      const attrs = this._hass.states[row.entity_id]?.attributes;
      return attrs?.config_entry_id && Array.isArray(attrs.schedules);
    });
    const selector = this.shadowRoot.querySelector("select");
    const key = JSON.stringify(rows.map(row => [row.entity_id, this._hass.states[row.entity_id]?.attributes.friendly_name]));
    if (this._choicesKey !== key) {
      this._choicesKey = key; selector.replaceChildren();
      for (const row of rows) {
        const option = document.createElement("option"); option.value = row.entity_id;
        option.textContent = this._hass.states[row.entity_id].attributes.friendly_name || row.entity_id;
        selector.append(option);
      }
    }
    if (!rows.some(row => row.entity_id === this._selected)) this._selected = rows[0]?.entity_id;
    selector.value = this._selected || "";
    this.shadowRoot.querySelector("#selector-label").hidden = rows.length < 2;
    const status = this.shadowRoot.querySelector("#status");
    status.hidden = rows.length > 0;
    status.textContent = this._error ? "Unable to load household schedules. Check your Home Assistant connection and retry." : this._loading && !this._registry ? "Loading household schedules…" : "No loaded Household Voice schedules sensor. Configure the integration or check its status in Settings.";
    this.shadowRoot.querySelector("#retry").hidden = !this._error;
    const root = this.shadowRoot.querySelector("#card");
    root.hidden = !this._selected;
    if (this._selected) {
      if (!this._card || this._card.config?.entity !== this._selected) {
        this._card = document.createElement("household-voice-manager-card");
        this._card.setConfig({entity: this._selected}); root.replaceChildren(this._card);
      }
      this._card.hass = this._hass;
    }
  }
}
if (!customElements.get("household-voice-panel")) customElements.define("household-voice-panel", HouseholdVoicePanel);

