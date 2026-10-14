/* Admin UI contract only: no live microphone, HA, browser storage or audio. */
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
class Node {
 constructor(){this.children=[];this.textContent='';this.value='';this.checked=false;this.hidden=false;}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(...nodes){this.children=nodes;}
}
const classes=new Map();
const context={HTMLElement:class{attachShadow(){this.shadowRoot={nodes:Object.fromEntries(['#recording-mode','#recording-source','#recording-source-status','#recording-confirm','#recording-refresh','#recording-recover','#recording-status'].map(key=>[key,new Node()])),querySelector(key){return this.nodes[key];}};}},
 customElements:{define:(name,value)=>classes.set(name,value),get:name=>classes.get(name)},document:{createElement:()=>new Node()},Date,Intl,setInterval:()=>1,clearInterval(){}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../custom_components/household_voice/www/household-voice-panel.js'),'utf8'),context);
const Recovery=classes.get('household-voice-recording-recovery'),settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 const ui=new Recovery(),requests=[],rows=[{assistant_id:'assist_satellite.kitchen',available:true,busy:false,recovery_required:true}];
 const data={enabled:true,paused:false,worker_connected:false,sources:rows,physical_stop_confirmed:false};
 const hass={user:{id:'admin',is_admin:true},connection:{},states:{'assist_satellite.kitchen':{attributes:{friendly_name:'<img> Kitchen voice'}}},callWS:async request=>{requests.push(request);if(request.operation==='recover'){rows[0].recovery_required=false;return {cleared:true,physical_stop_confirmed:false};}return data;}};
 ui.configure(hass,'entry',false);assert.equal(requests.length,0,'Disabled mode never calls the recording backend');
 ui.configure(hass,'entry',true);await settle();
 assert.equal(JSON.stringify(requests[0]),JSON.stringify({type:'household_voice/native_recording',operation:'list'}));
 const source=ui.shadowRoot.querySelector('#recording-source'),confirm=ui.shadowRoot.querySelector('#recording-confirm'),button=ui.shadowRoot.querySelector('#recording-recover');
 assert.equal(source.children[1].textContent,'<img> Kitchen voice','Registered names use text, never HTML');
 source.value=rows[0].assistant_id;source.onchange();assert.equal(button.disabled,true);
 const count=requests.length;await ui.recover();assert.equal(requests.length,count,'No implicit confirmation');
 confirm.checked=true;confirm.onchange();assert.equal(button.disabled,false);
 ui.configure({...hass,states:{...hass.states}},'entry',true);assert.equal(requests.length,count,'State updates do not poll repeatedly');
 await ui.recover();
 assert.equal(JSON.stringify(requests[count]),JSON.stringify({type:'household_voice/native_recording',operation:'recover',assistant_id:rows[0].assistant_id,confirm_microphone_stopped:true}));
 assert.equal(confirm.checked,false);assert.equal(button.disabled,true);
 assert(ui.shadowRoot.querySelector('#recording-status').textContent.includes('did not stop or test'));
 rows[0].recovery_required=true;await ui.load();source.value=rows[0].assistant_id;confirm.checked=true;
 for(const key of ['available','busy']){const old=rows[0][key];rows[0][key]=key==='busy';ui.render();assert.equal(button.disabled,true);rows[0][key]=old;}
 data.paused=true;ui.render();assert.equal(button.disabled,true);data.paused=false;
 await ui.load();assert.equal(confirm.checked,false,'Every fresh status requires renewed physical confirmation');
 source.value=rows[0].assistant_id;confirm.checked=true;
 ui.configure({...hass,user:{id:'family',is_admin:false}},'entry',true);
 assert.equal(ui.hidden,true);assert.equal(ui.data,null);assert.equal(confirm.checked,false);
 const denied=requests.length;await ui.load();await ui.recover();assert.equal(requests.length,denied);
 let resolve;
 ui.configure({...hass,connection:{},callWS:()=>new Promise(done=>resolve=done)},'entry',true);
 ui.disconnectedCallback();resolve(data);await settle();assert.equal(ui.data,null,'Late disconnected response cannot repopulate state');
 ui.configure({...hass,connection:{},callWS:async()=>{throw Error('private audio content');}},'entry',true);await settle();
 assert(!ui.shadowRoot.querySelector('#recording-status').textContent.includes('private audio content'));
 ui.configure(hass,'entry',true);await settle();source.value=rows[0].assistant_id;confirm.checked=true;
 ui._hass={...hass,callWS:()=>new Promise(done=>resolve=done)};
 const pending=ui.recover();ui.configure({...hass,user:{id:'family',is_admin:false}},null,false);
 resolve({cleared:true});await pending;assert.equal(ui.data,null);assert.equal(ui.shadowRoot.querySelector('#recording-status').textContent,'','Late recovery cannot update another user');
 console.log('PASS: recording dropdown; admin and enabled guards; explicit physical confirmation; unavailable/busy/paused denial; no polling or replay; generic errors; stale-disconnect/user-change protection');
})().catch(error=>{console.error(error);process.exitCode=1;});
