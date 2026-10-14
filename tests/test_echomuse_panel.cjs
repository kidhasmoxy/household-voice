/* Native DOM contract fixtures: no controller, live HA, audio or browser storage. */
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
class Node {
 constructor(){this.children=[];this.textContent='';this.hidden=false;this.value='';}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(...nodes){this.children=nodes;}
 querySelector(){return this.button;}
 reportValidity(){return true;}
}
const classes=new Map();let confirmation=true,confirmCalls=[];
const context={HTMLElement:class{attachShadow(){const form=new Node();form.elements={assistant:new Node(),controller:new Node()};form.button=new Node();this.shadowRoot={nodes:Object.fromEntries(['#echo-secret','#echo-config','#echo-forget','#echo-refresh','#echo-status','#echo-mode','#echo-rows'].map(key=>[key,new Node()])),querySelector(key){return key==='form'?form:this.nodes[key];}};}},
 customElements:{define:(name,value)=>classes.set(name,value),get:name=>classes.get(name)},
 document:{createElement:()=>new Node()},window:{confirm:message=>{confirmCalls.push(message);return confirmation;}},Date,Intl,setInterval:()=>1,clearInterval(){}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../custom_components/household_voice/www/household-voice-panel.js'),'utf8'),context);
const Manager=classes.get('household-voice-echomuse-manager'),settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 const manager=new Manager(),requests=[];
 const rows=[{key_id:'opaque-key-id',assistant_id:'assist_satellite.kitchen',controller_device_id:'controller.12',enabled:false,active:false,recovery_required:false}];
 const hass={user:{id:'admin',is_admin:true},connection:{},callWS:async request=>{requests.push(request);return request.operation==='provision'?{controller_config:{endpoint:'https://fixture.invalid/api',key_id:'opaque-key-id',key_hex:'private-one-time-key'}}:{enabled:false,sources:rows};}};
 const assistants=[{id:'assist_satellite.kitchen',name:'<img onerror=alert(1)>',speaker_id:null,status:'choice_required'}];
 manager.configure(hass,'entry',assistants);await settle();
 assert.equal(requests[0].operation,'list');
 const form=manager.shadowRoot.querySelector('form');
 assert(form.elements.assistant.children[1].textContent.includes('Own speaker (unpaired)'));
 assert(!form.elements.assistant.children[1].disabled,'Unpaired assistants may use native audio');
 form.elements.assistant.value='assist_satellite.kitchen';form.elements.controller.value='controller.12';
 await form.onsubmit({preventDefault(){},target:form});
 const provision=requests.find(row=>row.operation==='provision');
 assert.equal(provision.enabled,false);assert.equal(provision.controller_device_id,'controller.12');assert.equal(provision.entry_id,'entry');
 assert.equal(manager.hasKey,true);assert(manager.shadowRoot.querySelector('#echo-config').textContent.includes('private-one-time-key'));
 assert.equal(manager.shadowRoot.querySelector('#echo-secret').hidden,false);assert.equal(form.button.disabled,true);
 assert(!JSON.stringify(manager.data).includes('private-one-time-key'),'List state never retains key');
 const before=requests.length;await form.onsubmit({preventDefault(){},target:form});assert.equal(requests.length,before,'Cannot overwrite unsaved one-time configuration');
 manager.shadowRoot.querySelector('#echo-forget').onclick();assert.equal(manager.shadowRoot.querySelector('#echo-config').textContent,'');assert.equal(manager.hasKey,false);
 let controls=manager.shadowRoot.querySelector('#echo-rows').children[0].children[1].children;
 await controls[0].onclick();assert(requests.some(row=>row.operation==='enable' && row.enabled===true));
 rows[0].recovery_required=true;await manager.load();
 controls=manager.shadowRoot.querySelector('#echo-rows').children[0].children[1].children;
 assert.equal(controls[0].disabled,true);assert.equal(controls[1].disabled,true);
 confirmation=false;const count=requests.length;await controls[2].onclick();assert.equal(requests.length,count);
 confirmation=true;await controls[2].onclick();assert(requests.some(row=>row.operation==='recover' && row.confirm_external_output_stopped===true));
 assert(confirmCalls.at(-1).includes('does not stop audio'));assert(manager.shadowRoot.querySelector('#echo-status').textContent.includes('recover the controller'));
 rows[0].recovery_required=false;await manager.load();controls=manager.shadowRoot.querySelector('#echo-rows').children[0].children[1].children;
 confirmation=false;const preRevoke=requests.length;await controls[1].onclick();assert.equal(requests.length,preRevoke);
 confirmation=true;await controls[1].onclick();assert(requests.some(row=>row.operation==='revoke'));
 manager.shadowRoot.querySelector('#echo-config').textContent='private-one-time-key';manager.hasKey=true;
 manager.configure({...hass,user:{id:'family',is_admin:false}},'entry',assistants);assert.equal(manager.hidden,true);assert.equal(manager.shadowRoot.querySelector('#echo-config').textContent,'');
 const noAuth=requests.length;await manager.action('enable',{key_id:'opaque-key-id',enabled:true});await manager.load();assert.equal(requests.length,noAuth);
 let resolve;const delayed={...hass,connection:{},callWS:()=>new Promise(done=>resolve=done)};
 manager.configure(delayed,'entry',assistants);manager.disconnectedCallback();resolve({enabled:true,sources:rows});await settle();assert.equal(manager.data,null,'Disconnected stale list cannot apply');
 manager.configure({...hass,connection:{},callWS:async()=>{throw Error('private-one-time-key');}},'entry',assistants);await settle();assert(!manager.shadowRoot.querySelector('#echo-status').textContent.includes('private-one-time-key'));
 // Provision reply that arrives after switching away cannot display a secret.
 manager.configure(hass,'entry',assistants);await settle();
 manager._hass={...hass,callWS:()=>new Promise(done=>resolve=done)};
 const pending=manager.action('provision',{assistant_id:assistants[0].id,controller_device_id:'controller.12',enabled:false});
 manager.configure({...hass,user:{id:'family',is_admin:false}},null,[]);
 resolve({controller_config:{key_hex:'private-one-time-key'}});await pending;
 assert.equal(manager.shadowRoot.querySelector('#echo-config').textContent,'');
 console.log('PASS: admin EchoMuse UI; disabled explicit provisioning; transient key; unpaired native choice; source controls; recovery and revocation confirmation; generic errors; stale/disconnect/user-switch guards');
})().catch(error=>{console.error(error);process.exitCode=1;});
