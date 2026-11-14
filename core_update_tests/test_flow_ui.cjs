const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('deye_ai_bridge/local_updates.html','utf8');const script=html.split('<script>')[1].split('</script>')[0];
for(const route of ['updates','maintenance']){
 const ids={};const el=id=>ids[id]??={checked:false,disabled:false,value:'',textContent:'',setAttribute(){}};
 const mem=new Map();const ctx={document:{getElementById:el},location:{pathname:'/api/hassio_ingress/session/api/private/'+route+'/ui'},sessionStorage:{getItem:k=>mem.get(k),setItem:(k,v)=>mem.set(k,v),removeItem:k=>mem.delete(k)},fetch:()=>new Promise(()=>{}),AbortController,setTimeout,clearTimeout,Date,JSON,Object,Error,Promise};
 vm.createContext(ctx);vm.runInContext(script,ctx);
 assert.equal(ids.back.href,'/api/hassio_ingress/session/');
 vm.runInContext("statusValue={installation_blockers:['active']};reviewed={};packageValue={};el('cloudOff').checked=true;controls()",ctx);assert.equal(ids.install.disabled,false);
 vm.runInContext("el('cloudOff').checked=false;controls()",ctx);assert.equal(ids.install.disabled,true);
 for(const phase of ['accepted','pausing','verifying_restore','installing','checking_startup','checking_measurements','resuming','verifying_operation']){
  const result=vm.runInContext(`operation({update_flow:{package_sha256:'x',phase:'${phase}'}},'x')`,ctx);assert.equal(result,'working');assert(!ids.headline.textContent.includes('installeret og bekræftet'));
 }
 assert.equal(vm.runInContext("operation({update_flow:{package_sha256:'x',phase:'failed',error:'test',stop_unconfirmed:true}},'x')",ctx),'failed');assert(ids.detail.textContent.includes('IKKE bekræftet'));
 assert.equal(vm.runInContext("operation({update_flow:{package_sha256:'x',phase:'complete',resumed:true}},'x')",ctx),'installed');assert(ids.detail.textContent.includes('genoptaget'));
}
console.log('UI: ingress return link, consent, eight progress phases, failure and completion passed');
