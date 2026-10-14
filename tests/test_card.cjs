/* Minimal DOM/HA fixtures: frontend contract tests, not real HA rendering. */
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const path = require("node:path");
class Node {
  constructor() { this.children=[]; this.textContent=""; this.value=""; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren() { this.children=[]; }
  replaceWith(value) { Object.assign(this,value); }
  setAttribute(name,value) { this[name]=value; }
}
let Card; const classes=new Map();
const context = {HTMLElement:class {}, customElements:{define:(name,value)=>{classes.set(name,value);if(name==='household-voice-manager-card') Card=value;},get:name=>classes.get(name)},
  window:{customCards:[],confirm:()=>true,prompt:()=>"new room"}, document:{createElement:()=>new Node()},
  crypto:{randomUUID:()=>"fixture-request-uuid"}, Date, Intl, setInterval, clearInterval};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,"../custom_components/household_voice/www/household-voice-panel.js"),"utf8"),context);
(async () => {
  const card = new Card();
  const nodes = Object.fromEntries(["#rows","#mode","#status","#events","form button","#pairing-rows","#pairing-configure","#pairing-guidance","#speaker-preview","#timer-history-count"].map(key=>[key,new Node()]));
  nodes.form={elements:Object.fromEntries(['source_room','target_room','kind','target_assistant_id'].map(key=>[key,new Node()]))};
  nodes.form.elements.kind.value='timer';
  card.shadowRoot={querySelector:key=>nodes[key]};
  card.config={entity:"sensor.test"};
  const calls=[];
  card._hass={states:{"sensor.test":{state:"1",attributes:{mode:"dry_run",config_entry_id:"fixture-entry",
    schedules:[{id:"id-1",revision:7,label:"<img src=x onerror=alert(1)>",kind:"timer",state:"active",source_room:"kitchen",target_room:"bedroom",due:Date.now()/1000+60}],events:[]}}},
    callService:async (...args)=>calls.push(args)};
  card.renderRows();
  assert.equal(nodes["#mode"].textContent,"Alarm audio is disabled. Schedules are saved.");
  const attrs=card._hass.states["sensor.test"].attributes;
  attrs.ring_execution_enabled=true; card.renderRows();
  assert(nodes["#mode"].textContent.includes("this Home Assistant worker is disabled"));
  attrs.ring_worker_enabled=true; card.renderRows();
  assert(nodes["#mode"].textContent.includes("Verify your selected speakers"));
  attrs.ring_worker_error="coordinator_unavailable"; card.renderRows();
  assert(nodes["#mode"].textContent.includes("delivery needs attention"));
  delete attrs.ring_worker_error;
  const row=nodes["#rows"].children[0];
  assert.equal(row.children[0].textContent,"<img src=x onerror=alert(1)> · timer");
  const pause=row.children[2].children.find(node=>node.textContent==="Pause");
  await pause.onclick();
  assert.equal(calls[0][0],"household_voice"); assert.equal(calls[0][1],"command");
  assert.equal(calls[0][2].identifier,"id-1"); assert.equal(calls[0][2].expected_revision,7);
  assert.equal(calls[0][2].config_entry_id,"fixture-entry"); assert.equal(calls[0][2].request_id,"fixture-request-uuid");
  assert(!Object.hasOwn(calls[0][2],"token"));
  card._hass.states["sensor.test"].state="unavailable"; card.renderRows();
  assert.equal(nodes["form button"].disabled,true);
  await card.call("command",{}); assert.equal(calls.length,1);
  console.log("PASS: dynamic pause preserves ID/revision; backend HA service only; labels are text; unavailable disables mutation; disabled/configured/worker-error alarm disclosure");
})().catch(error=>{console.error(error);process.exitCode=1;});
