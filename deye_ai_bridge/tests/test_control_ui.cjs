// Run with node tests/test_control_ui.cjs. No browser or external dependencies.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(require('path').join(__dirname,'../index.html'),'utf8');
assert(!html.includes('id="teslaGridOn"'));assert(!html.includes('tesla_grid_on'));
const js=html.split('<script>').pop().split('</script>')[0];
const code=js.slice(js.indexOf('let controlRequest='),js.indexOf('\nlet operationAutomation='));
const nodes={};function element(id){return nodes[id]||(nodes[id]={disabled:false,textContent:'',className:'',checked:true,value:'1',setAttribute(){}})}
let resolvePost,posts=0;
const context=vm.createContext({document:{querySelector:s=>element(s),getElementById:id=>element('#'+id)},console,Date,JSON,call:async(path,args)=>{if(args){posts++;return new Promise(r=>resolvePost=r)}return {status:'disabled',restored:true}},loadShadow(){}});
vm.runInContext(`let operationAutomation={status:'disabled',restored:true};function renderAutomatic(a){operationAutomation=a;updateAutomaticControls(a)}\n${code}`,context);
(async()=>{
 const start=vm.runInContext("automaticAction('enable')",context);
 assert(nodes['#autoEnable'].disabled);assert(nodes['#autoDisable'].disabled);
 assert(nodes['#autoFeedback'].textContent.includes('tryk er registreret'));
 vm.runInContext("renderAutomatic({status:'disabled',restored:true});automaticAction('enable')",context);
 assert.strictEqual(posts,1);assert(nodes['#autoEnable'].disabled);
 resolvePost({status:'active'});await start;
 assert.strictEqual(nodes['#autoEnable'].textContent,'Automatisk drift er aktiv');
 const stop=vm.runInContext("automaticAction('disable')",context);
 vm.runInContext("automaticAction('disable')",context);assert.strictEqual(posts,2);
 assert(nodes['#autoDisable'].textContent.includes('gendanner'));
 resolvePost({status:'verifying_restore',restored:false});await stop;
 assert(nodes['#autoDisable'].disabled);assert(nodes['#autoEnable'].disabled);
 assert(nodes['#autoDisable'].textContent.includes('efterkontrol'));
 vm.runInContext("renderAutomatic({status:'disabled',restored:true})",context);
 assert(!nodes['#autoEnable'].disabled);assert(nodes['#autoDisable'].disabled);
 assert(nodes['#autoFeedback'].textContent.includes('indstillingerne er gendannet'));
 assert(nodes['#autoDisable'].textContent.includes('bekræftet'));
 console.log('Control UI: immediate acknowledgement, duplicate-click prevention, polling lock and restore completion passed.');
})().catch(e=>{console.error(e);process.exitCode=1});
