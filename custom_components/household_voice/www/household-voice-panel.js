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
    <label>Assistant<select name="target_assistant_id"><option value="">Choose an assistant</option></select><span id="speaker-preview" class="muted"></span></label>
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
      for (const name of ["source_room","target_room"]) this.shadowRoot.querySelector(`#${name}_label`).hidden = alarm;
      form.elements.source_room.required = !alarm && !form.elements.target_assistant_id.value;
      form.elements.target_assistant_id.required = alarm;
    };
    form.elements.target_assistant_id.onchange = () => { this.updateSpeakerPreview(); form.elements.source_room.required=form.elements.kind.value!=="alarm" && !form.elements.target_assistant_id.value; };
    form.onsubmit = async (event) => {
      event.preventDefault();
      if (!form.reportValidity() || this.busy) return;
      const kind = form.elements.kind.value;
      const payload = {label: form.elements.label.value.trim(), source_room: form.elements.source_room.value.trim()};
      if (kind === "timer" && form.elements.target_room.value.trim()) payload.target_room = form.elements.target_room.value.trim();
      if (kind === "timer") {
        payload.seconds = Number(form.elements.minutes.value) * 60;
        const assistant=(this.state()?.attributes.assistants || []).find(row=>row.id===form.elements.target_assistant_id.value);
        if (assistant) {
          if (!this.scheduleReady(assistant)) { this.message("Choose an assistant with an available output.",true);return; }
          if (payload.target_room && payload.target_room!==assistant.area_id) { this.message("Choose either an assistant or a different ring room.",true);return; }
          Object.assign(payload,{target_assistant_id:assistant.id,source_room:assistant.area_id});
        }
      }
      else {
        const assistant = (this.state()?.attributes.assistants || []).find(row=>row.id===form.elements.target_assistant_id.value);
        if (!assistant || !this.scheduleReady(assistant)) { this.message("Choose an assistant with an available built-in or paired speaker.", true); return; }
        Object.assign(payload, {target_assistant_id:assistant.id, source_room:assistant.area_id,
          local_time: form.elements.local_time.value, timezone: form.elements.timezone.value.trim(),
          weekdays: Array.from({length:7},(_,index)=>index).filter(index=>form.elements[`day_${index}`].checked), one_shot: form.elements.one_shot.checked});
      }
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
    const assistant = (this.state()?.attributes.assistants || []).find(item=>item.id===row.target_assistant_id);
    const output = row.target_assistant_id ? `${assistant?.name || row.target_assistant_id} → ${this.scheduleSpeaker(assistant)} (${assistant?.status || "assistant missing"})` : row.kind === "alarm" ? "Legacy alarm — choose an assistant; existing routing retained" : `${roomName(row.source_room)} → ${roomName(row.target_room)}`;
    return `${labels[row.state] || row.state} · ${output} · ${timing}${zone}`;
  }
  scheduleReady(row) { return Boolean(row?.schedule_output) || row?.status==='ready'; }
  scheduleSpeaker(row) { return row?.schedule_output?.kind==='native' ? 'Built-in speaker' : row?.schedule_output?.entity_id || row?.speaker_id || 'Output unavailable'; }
  updateSpeakerPreview() {
    const selected=this.shadowRoot.querySelector("form").elements.target_assistant_id.value;
    const row=(this.state()?.attributes.assistants || []).find(item=>item.id===selected);
    this.shadowRoot.querySelector("#speaker-preview").textContent=row ? `${this.scheduleSpeaker(row)} · ${this.scheduleReady(row) ? "Available" : row.status}` : "Choose an assistant for built-in or paired speaker output. Leave blank for a room timer.";
  }
  updateAssistantChoices(assistants) {
    const key=JSON.stringify(assistants); if (this.assistantChoicesKey===key) return;
    this.assistantChoicesKey=key;
    const select=this.shadowRoot.querySelector("form").elements.target_assistant_id, previous=select.value;
    select.replaceChildren(); const blank=document.createElement("option"); blank.value=""; blank.textContent="Choose an assistant"; select.append(blank);
    for (const row of assistants) { const node=document.createElement("option"); node.value=row.id; node.textContent=`${row.name || row.id} — ${this.scheduleSpeaker(row)}`; node.disabled=!this.scheduleReady(row); select.append(node); }
    select.value=previous; this.updateSpeakerPreview();
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
      element.name = name; element.required = name === "source_room" && form.elements.kind.value!=="alarm" && !form.elements.target_assistant_id.value;
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
    this.updateAssistantChoices(state.attributes.assistants || []);
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
      button("Rename", "rename");
      if (row.kind === "alarm" || row.target_assistant_id) {
        const choice=document.createElement("select"); choice.setAttribute("aria-label",`Assistant for ${row.label}`);
        const blank=document.createElement("option"); blank.value=""; blank.textContent="Choose alarm assistant"; choice.append(blank);
        for (const assistant of state.attributes.assistants || []) { const item=document.createElement("option"); item.value=assistant.id; item.textContent=`${assistant.name || assistant.id} → ${this.scheduleSpeaker(assistant)}`; item.disabled=!this.scheduleReady(assistant); choice.append(item); }
        choice.value=row.target_assistant_id || ""; controls.append(choice);
        const move=document.createElement("button"); move.textContent=row.target_assistant_id ? "Move assistant" : "Bind legacy alarm"; move.disabled=this.busy;
        move.onclick=()=> { if (choice.value) this.call("command",{identifier:row.id,expected_revision:row.revision,operation:"move",target_assistant_id:choice.value}); }; controls.append(move);
      } else button("Move room", "move");
      button("Delete", "delete");
      box.append(controls); root.append(box);
    }
    const events = state.attributes.events || [];
    this.shadowRoot.querySelector("#events").textContent = `Recent occurrences: ${events.slice(-5).map(event => event.status).join(", ") || "none"}. Countdown is approximate between polls.`;
  }
}
if (!customElements.get("household-voice-manager-card")) customElements.define("household-voice-manager-card", HouseholdVoiceManagerCard);


/* Source management is admin-only; the server independently enforces authority. */
class HouseholdVoiceEchoMuseManager extends HTMLElement {
  constructor() {
    super(); this.attachShadow({mode:"open"}); this.generation=0;
    this.shadowRoot.innerHTML=`<style>
      :host{display:block;margin-top:24px}section{padding:20px;background:var(--card-background-color);border-radius:16px}
      form,.controls{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0}label{display:flex;flex-direction:column;gap:5px}
      input,select,button{font:inherit;padding:9px;border:1px solid var(--divider-color);border-radius:8px;background:var(--card-background-color);color:var(--primary-text-color)}
      p{line-height:1.5;color:var(--secondary-text-color)}pre{white-space:pre-wrap;overflow-wrap:anywhere;user-select:text}.row{border-top:1px solid var(--divider-color);padding:12px 0}[hidden]{display:none!important}
    </style><section><h2>EchoMuse voice devices</h2>
      <p>Pairing is optional: an unpaired assistant uses its own speaker. A paired assistant uses its selected room speaker. Enable the EchoMuse endpoint in the integration’s Configure menu when ready.</p>
      <p>Set a trusted HTTPS external URL in Home Assistant’s network settings first. Enter the exact device ID shown by your EchoMuse controller. Creating a source leaves it disabled and does not install anything on the controller.</p>
      <form><label>Assistant<select name="assistant" aria-label="Assistant" required></select></label>
      <label>EchoMuse controller device ID<input name="controller" required maxlength="128" pattern="[A-Za-z0-9_.:\\-]{1,128}" autocomplete="off" spellcheck="false"></label>
      <button type="submit">Create disabled source</button></form>
      <div id="echo-secret" hidden><p>Copy this configuration into the selected controller’s Household Voice audio settings. The key is shown only here, once. Keep it private. Enable the source after configuring the controller.</p><pre id="echo-config"></pre><button id="echo-forget">I saved it — hide key</button></div>
      <p id="echo-status" role="status" aria-live="polite"></p><button id="echo-refresh">Refresh sources</button><p id="echo-mode"></p><div id="echo-rows"></div>
    </section>`;
    this.shadowRoot.querySelector('#echo-forget').onclick=()=>this.forgetKey();
    this.shadowRoot.querySelector('#echo-refresh').onclick=()=>this.load();
    this.shadowRoot.querySelector('form').onsubmit=async event=>{
      event.preventDefault(); const form=event.target;
      if (!form.reportValidity() || this.busy || this.hasKey) return;
      const assistant=form.elements.assistant.value;
      if (!(this.assistants || []).some(row=>row.id===assistant)) return;
      const controller=form.elements.controller.value.trim();
      if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(controller)) return;
      await this.action('provision',{assistant_id:assistant,controller_device_id:controller,enabled:false});
    };
  }
  configure(hass,entry,assistants) {
    const changed=this.entry!==entry || this._hass?.connection!==hass?.connection || this._hass?.user?.id!==hass?.user?.id;
    this._hass=hass; this.entry=entry; this.assistants=assistants;
    this.hidden=!this.authorized();
    if (changed || !this.authorized()) { this.generation++; this.busy=false; this.data=null; this.forgetKey(); }
    this.choices(); this.render();
    if (changed && this.authorized()) this.load();
  }
  authorized() { return this._hass?.user?.is_admin===true && Boolean(this.entry); }
  disconnectedCallback() { this.generation++; this.busy=false; this.forgetKey(); }
  forgetKey() {
    this.hasKey=false; this.shadowRoot.querySelector('#echo-config').textContent='';
    this.shadowRoot.querySelector('#echo-secret').hidden=true; this.render();
  }
  message(text) { this.shadowRoot.querySelector('#echo-status').textContent=text; }
  choices() {
    const select=this.shadowRoot.querySelector('form').elements.assistant, previous=select.value;
    const key=JSON.stringify(this.assistants || []); if (key===this.choicesKey) return; this.choicesKey=key;
    select.replaceChildren(); const blank=document.createElement('option'); blank.value=''; blank.textContent='Choose an assistant'; select.append(blank);
    for (const row of this.assistants || []) { const item=document.createElement('option'); item.value=row.id; item.textContent=`${row.name || row.id} — ${row.speaker_id || 'Own speaker (unpaired)'}`; select.append(item); }
    select.value=previous;
  }
  async load() {
    if (!this.authorized() || this.busy) return;
    const generation=++this.generation; this.busy=true; this.render();
    try {
      const data=await this._hass.callWS({type:'household_voice/echomuse',entry_id:this.entry,operation:'list'});
      if (generation!==this.generation || !this.authorized()) return;
      this.data=data; this.message('Sources refreshed. Status does not confirm audible playback.');
    } catch (_) { if (generation===this.generation) { this.data=null; this.message('Unable to load EchoMuse sources. Check the integration and your connection, then refresh.'); } }
    finally { if (generation===this.generation) { this.busy=false; this.render(); } }
  }
  async action(operation,fields) {
    if (!this.authorized() || this.busy) return;
    const generation=++this.generation; this.busy=true; this.render(); this.message('Saving…');
    try {
      const result=await this._hass.callWS({type:'household_voice/echomuse',entry_id:this.entry,operation,...fields});
      if (generation!==this.generation || !this.authorized()) return;
      if (operation==='provision') {
        this.shadowRoot.querySelector('#echo-config').textContent=JSON.stringify(result.controller_config,null,2);
        this.shadowRoot.querySelector('#echo-secret').hidden=false; this.hasKey=true;
        this.shadowRoot.querySelector('form').elements.controller.value='';
      }
      this.message(operation==='recover' ? 'Home Assistant recovery saved. Also recover the controller before retrying audio.' : 'Saved. No test audio was played.');
      const data=await this._hass.callWS({type:'household_voice/echomuse',entry_id:this.entry,operation:'list'});
      if (generation===this.generation && this.authorized()) this.data=data;
    } catch (_) { if (generation===this.generation) this.message('Request failed or its result is uncertain. Refresh before retrying. Check the HTTPS URL, source registration, active work and recovery status.'); }
    finally { if (generation===this.generation) { this.busy=false; this.render(); } }
  }
  render() {
    if (!this.shadowRoot) return;
    this.shadowRoot.querySelector('form').querySelector('button').disabled=this.busy || this.hasKey || !this.authorized();
    this.shadowRoot.querySelector('#echo-refresh').disabled=this.busy || !this.authorized();
    this.shadowRoot.querySelector('#echo-mode').textContent=this.data ? `Endpoint ${this.data.enabled ? 'enabled' : 'disabled'}. Each source has its own enable switch.` : '';
    const root=this.shadowRoot.querySelector('#echo-rows'); root.replaceChildren();
    for (const row of this.data?.sources || []) {
      const box=document.createElement('div'); box.className='row';
      const title=document.createElement('div'); const assistant=(this.assistants || []).find(item=>item.id===row.assistant_id);
      title.textContent=`${assistant?.name || row.assistant_id} · ${row.controller_device_id} · ${row.enabled ? 'Enabled' : 'Disabled'}${row.active ? ' · Active' : ''}${row.recovery_required ? ' · Recovery required' : ''}`; box.append(title);
      const controls=document.createElement('div'); controls.className='controls';
      const add=(label,operation,fields,confirm,disabled=false)=>{const button=document.createElement('button');button.textContent=label;button.disabled=this.busy || disabled;button.onclick=()=>{if (confirm && !window.confirm(confirm)) return;return this.action(operation,{key_id:row.key_id,...fields});};controls.append(button);};
      add(row.enabled?'Disable':'Enable','enable',{enabled:!row.enabled},null,row.active || (!row.enabled && row.recovery_required));
      add('Revoke','revoke',{},'Revoke this source key? Its controller will need a new configuration.',row.active || row.recovery_required);
      if (row.recovery_required) add('Confirm output stopped & recover','recover',{confirm_external_output_stopped:true},'Have you confirmed that this controller’s external speaker output has stopped? This clears uncertain Home Assistant delivery state; it does not stop audio. You must also recover the controller.',row.active);
      box.append(controls);root.append(box);
    }
  }
}
if (!customElements.get("household-voice-echomuse-manager")) customElements.define("household-voice-echomuse-manager",HouseholdVoiceEchoMuseManager);

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
      <button id="retry" hidden>Retry loading schedules</button><div id="card"></div><div id="echomuse"></div>
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
    this._echo?.disconnectedCallback();
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
    const echoRoot=this.shadowRoot.querySelector('#echomuse'); echoRoot.hidden=!admin || !this._selected;
    if (admin && this._selected) {
      if (!this._echo) { this._echo=document.createElement('household-voice-echomuse-manager'); echoRoot.replaceChildren(this._echo); }
      const attrs=this._hass.states[this._selected].attributes;
      this._echo.configure(this._hass,attrs.config_entry_id,attrs.assistants || []);
    } else this._echo?.configure(this._hass,null,[]);
  }
}
if (!customElements.get("household-voice-panel")) customElements.define("household-voice-panel", HouseholdVoicePanel);

