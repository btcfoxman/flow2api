const {readFileSync}=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const test=require('node:test');
const assert=require('node:assert/strict');
const html=readFileSync(path.join(__dirname,'../static/manage.html'),'utf8');
function definition(first,next){return html.slice(html.indexOf(`        ${first}=`),html.indexOf(`        ${next}=`)).trim().replace(/,$/,'');}

test('all inline page scripts compile',()=>{
  for(const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g))new vm.Script(match[1]);
});
function harness({confirmed=true,ok=true}={}){
  const records={requests:[],toasts:[],downloads:0};
  const ctx={allTokens:[{st:'flow:internal-marker'}],confirm:()=>confirmed,Date,
    apiRequest:async url=>{records.requests.push(url);return{ok,json:async()=>[{auth_mode:'flow',session_token:null,google_cookies:'fixture-cookie-json'}]};},
    showToast:(text,kind)=>records.toasts.push({text,kind}),
    Blob:class{constructor(parts){records.download=JSON.parse(parts[0]);}},
    URL:{createObjectURL:()=> 'blob:local-test',revokeObjectURL:()=>{}},
    document:{createElement:()=>({click:()=>records.downloads++}),body:{appendChild:()=>{},removeChild:()=>{}}}};
  vm.runInNewContext(`const ${definition('exportTokens','submitImportTokens')}; this.run=exportTokens;`,ctx);
  return{ctx,records};
}
test('exports real native payload from explicit endpoint, not list ST',async()=>{
  const {ctx,records}=harness();await ctx.run();
  assert.deepEqual(records.requests,['/api/tokens/export']);
  assert.equal(records.download[0].session_token,null);
  assert.equal(records.download[0].google_cookies,'fixture-cookie-json');
  assert.equal(records.downloads,1);
});
test('canceling credential warning makes no export request',async()=>{
  const {ctx,records}=harness({confirmed:false});await ctx.run();
  assert.equal(records.requests.length,0);assert.equal(records.downloads,0);
});
test('old server failure never falls back to exporting fake ST',async()=>{
  const {ctx,records}=harness({ok:false});await ctx.run();
  assert.equal(records.downloads,0);assert.equal(records.toasts.at(-1).kind,'error');
});
test('partial import does not show an all-success toast',async()=>{
  const toasts=[],elements={importFile:{files:[{name:'test.json',text:async()=>'[{"auth_mode":"flow"}]'}]},importBtn:{},importBtnText:{},importBtnSpinner:{classList:{add(){},remove(){}}}};
  const ctx={$:id=>elements[id],showToast:(text,kind)=>toasts.push({text,kind}),closeImportModal(){},refreshTokens:async()=>{},
    apiRequest:async()=>({json:async()=>({success:true,added:0,updated:1,errors:['会话验证未通过']})})};
  vm.runInNewContext(`const ${definition('submitImportTokens','submitSora2Activate')};this.run=submitImportTokens;`,ctx);
  await ctx.run();assert.equal(toasts.at(-1).kind,'error');assert.match(toasts.at(-1).text,/失败 1/);
});
