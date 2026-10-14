/* Authenticated panel discovery/lifecycle fixtures, not physical HA rendering. */
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const assert = require("node:assert/strict");
class Node {
  constructor() { this.children=[]; this.textContent=""; this.hidden=false; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=nodes; }
  setConfig(value) { this.config=value; }
  configure() {}
  disconnectedCallback() {}
}
const selectors = ["select", "#retry", "ha-menu-button", "#configure", "#guidance", "#selector-label", "#status", "#card", "#echomuse", "#recording"];
const classes = new Map();
const context = {HTMLElement:class { attachShadow() { this.shadowRoot = {nodes: Object.fromEntries(selectors.map(key=>[key,new Node()])), querySelector(key) { return this.nodes[key]; }};} },
  customElements:{define:(name,value)=>classes.set(name,value),get:name=>classes.get(name)},
  window:{}, document:{createElement:()=>new Node()}, crypto:{randomUUID:()=>"test-uuid"}, Date, Intl, setInterval:()=>1, clearInterval() {}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,"../custom_components/household_voice/www/household-voice-panel.js"),"utf8"),context);
const Panel = classes.get("household-voice-panel");
const settle = () => new Promise(resolve=>setImmediate(resolve));
(async () => {
  const Card=classes.get('household-voice-manager-card');
  const card=new Card(), select=new Node(), preview=new Node(); select.value='assist_satellite.kitchen';
  card.shadowRoot={querySelector:key=>key==='form'?{elements:{target_assistant_id:select}}:preview};
  card.config={entity:'sensor.fixture'};
  card._hass={states:{'sensor.fixture':{attributes:{assistants:[
    {id:'assist_satellite.kitchen',name:'Kitchen voice',speaker_id:'media_player.kitchen',status:'ready'},
    {id:'assist_satellite.bedroom',name:'Bedroom voice',speaker_id:null,status:'choice_required'}],rooms:[]}}}};
  card.updateAssistantChoices(card.state().attributes.assistants);
  assert.equal(select.children[2].disabled,true,'Ambiguous speakers cannot become an alarm default');
  assert.equal(select.value,'assist_satellite.kitchen','Current explicit selection survives refresh');
  assert(preview.textContent.includes('media_player.kitchen'));
  assert(card.detailText({kind:'alarm',state:'active',target_assistant_id:'assist_satellite.kitchen'}).includes('Kitchen voice → media_player.kitchen'));
  assert(card.detailText({kind:'alarm',state:'disabled'}).includes('Legacy alarm'));
  const native={id:'assist_satellite.bedroom',name:'Bedroom voice',speaker_id:null,status:'explicitly_unpaired',schedule_output:{kind:'native',entity_id:'assist_satellite.bedroom',device_id:'bedroom'}};
  card._hass.states['sensor.fixture'].attributes.assistants[1]=native;
  card.updateAssistantChoices(card.state().attributes.assistants);
  assert.equal(select.children[2].disabled,false,'Native-capable unpaired assistant accepts schedules');
  assert(card.detailText({kind:'timer',state:'active',target_assistant_id:native.id}).includes('Built-in speaker'));

  const schedules=[
    {id:'running',kind:'timer',state:'active'}, {id:'paused',kind:'timer',state:'paused'},
    {id:'ringing',kind:'timer',state:'ringing'}, {id:'missed',kind:'timer',state:'missed'},
    {id:'finished',kind:'timer',state:'completed'}, {id:'cancelled',kind:'timer',state:'cancelled'},
    {id:'dismissed',kind:'timer',state:'dismissed'}, {id:'recurring',kind:'alarm',state:'active'},
    {id:'old-alarm',kind:'alarm',state:'completed'}];
  const original=JSON.stringify(schedules);
  assert.equal(card.visibleSchedules(schedules).map(row=>row.id).join(','),'running,paused,ringing,missed,recurring,old-alarm');
  assert.equal(JSON.stringify(schedules),original,'Hiding history must not mutate durable schedule data');
  card.showTimerHistory=true;
  assert.equal(card.visibleSchedules(schedules).length,9,'History remains optionally visible');
  card.showTimerHistory=false;

  const attributes=card.state().attributes;
  attributes.area_pairings={areas:[{id:'hashed-area-id',name:'Living Room'}]};
  attributes.assistants[0]={id:'assist_satellite.kitchen',name:'Assist satellite',area_id:'hashed-area-id',speaker_id:'media_player.living',status:'ready'};
  card._hass.states['media_player.living']={attributes:{friendly_name:'Living Room Sonos'}};
  assert.equal(card.assistantName(attributes.assistants[0]),'Living Room — Assist satellite');
  assert.equal(card.scheduleSpeaker(attributes.assistants[0]),'Living Room Sonos');
  const sourceRoom={value:'old-room'},targetRoom={value:'other-room'};
  card.shadowRoot={querySelector:key=>key==='form'?{elements:{target_assistant_id:select,source_room:sourceRoom,target_room:targetRoom}}:preview};
  card.useAssistantRoom();
  assert.equal(sourceRoom.value,'hashed-area-id','Selecting an assistant uses its HA room without typing an ID');
  assert.equal(targetRoom.value,'','A stale room override must not conflict with the selected assistant');
  assert(preview.textContent.includes('Living Room Sonos'));

  const pairingRoot=new Node(),pairingConfigure=new Node(),pairingGuidance=new Node();
  card.shadowRoot={querySelector:key=>({'#pairing-rows':pairingRoot,'#pairing-configure':pairingConfigure,'#pairing-guidance':pairingGuidance})[key]};
  card._hass.user={is_admin:false}; card.renderPairings(attributes.assistants);
  assert(pairingRoot.children[0].textContent.includes('Living Room — Assist satellite → Living Room Sonos'));
  assert(pairingConfigure.hidden && pairingGuidance.hidden);
  card._hass.user={is_admin:true}; card.renderPairings(attributes.assistants);
  assert(!pairingConfigure.hidden && !pairingGuidance.hidden);

  const panel = new Panel();
  const requests = [];
  const hass = {user:{id:"admin",is_admin:true},connection:{},
    states:{"sensor.renamed_timer":{state:"1",attributes:{friendly_name:"<img> household",config_entry_id:"entry",schedules:[]}},
      "sensor.unrelated":{state:"0",attributes:{config_entry_id:"other",schedules:[]}}},
    callWS:async request=>{requests.push(request); return [
      {entity_id:"sensor.renamed_timer",platform:"household_voice"},
      {entity_id:"sensor.unrelated",platform:"another_integration"}];}};
  panel.hass=hass; await settle();
  assert.equal(requests[0].type,"config/entity_registry/list");
  assert.equal(panel._card.config.entity,"sensor.renamed_timer");
  assert.equal(panel._card.hass,hass);
  assert.equal(panel.shadowRoot.querySelector("select").children.length,1);
  assert.equal(panel.shadowRoot.querySelector("select").children[0].textContent,"<img> household");
  assert.equal(panel.shadowRoot.querySelector("#configure").hidden,false);
  const existingCard = panel._card;
  panel.hass={...hass,states:{...hass.states}};
  assert.equal(panel._card,existingCard,"State updates must preserve draft forms");
  assert.equal(requests.length,1,"State updates must not repeatedly load registry");
  panel.hass={...hass,user:{id:"household",is_admin:false}}; await settle();
  assert.equal(panel.shadowRoot.querySelector("#configure").hidden,true);
  panel.hass={...hass,connection:{},callWS:async()=>{throw new Error("private response");}}; await settle();
  assert.equal(panel.shadowRoot.querySelector("#retry").hidden,false);
  assert(!panel.shadowRoot.querySelector("#status").textContent.includes("private response"));
  assert.equal(panel.shadowRoot.querySelector("#card").hidden,true);
  let resolve;
  panel.hass={...hass,connection:{},callWS:()=>new Promise(done=>resolve=done)};
  panel.disconnectedCallback(); resolve([{entity_id:"sensor.renamed_timer",platform:"household_voice"}]); await settle();
  assert.equal(panel._registry,null,"Disconnected stale request must not apply");
  console.log("PASS: authenticated registry discovery; renamed entity and foreign sensor filtering; persistent form; admin settings link; generic failure; stale-disconnect protection");
})().catch(error=>{console.error(error);process.exitCode=1;});
