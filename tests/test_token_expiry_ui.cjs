const {readFileSync} = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');
const html = readFileSync(path.join(__dirname, '../static/manage.html'), 'utf8');
function definition(first, next) {
  return html.slice(html.indexOf('        '+first+'='), html.indexOf('        '+next+'=')).trim().replace(/,$/, '');
}
const sandbox = {Date};
vm.runInNewContext('const '+definition('formatExpiry','formatPlanType')+';const '+definition('renderTokenActions','renderTokens')+';this.formatTokenExpiry=formatTokenExpiry;this.formatExpiry=formatExpiry;this.renderTokenActions=renderTokenActions;',sandbox);
const future = {id:1, auth_mode:'flow', flow_cookie_expires_at:'2100-01-01T00:00:00Z', flow_cookie_expiry_status:'known'};
const visibleText = rendered => rendered.replace(/<[^>]*>/g, '');
test('new Flow never substitutes cookie retention or legacy AT for a session deadline',()=>{
  for(const state of ['known','expired','session','invalid','incomplete','unavailable']){
    const rendered=sandbox.formatTokenExpiry({...future,flow_cookie_expiry_status:state,at_expires:future.flow_cookie_expires_at});
    assert.equal(visibleText(rendered),'待验证 · 到期未知');
    assert.doesNotMatch(rendered,/2100|Flow Cookie|\d+天/);
  }
});
test('actual verification states and last verification time are displayed',()=>{
  for(const [state,label] of [['verified','验证通过'],['refresh_required','需刷新会话'],['verification_failed','验证失败']]){
    const rendered=sandbox.formatTokenExpiry({...future,session_status:state,session_checked_at:'2026-09-12T14:00:00Z'});
    assert.equal(visibleText(rendered),label+' · 到期未知');
    assert.match(rendered,/2026/);
    assert.match(rendered,/仅代表当时可用/);
    assert.match(rendered,state==='verified'?/text-green/:/text-red/);
  }
});
test('untrusted diagnostic fields never become HTML',()=>{
  const rendered=sandbox.formatTokenExpiry({...future,session_checked_at:'" onmouseover="alert(1)',session_reason:'<script>alert(1)</script>'});
  assert.doesNotMatch(rendered,/onmouseover|<script>/);
});
test('enabled counter is not described as schedulable',()=>{
  assert.match(html,/已启用 \/ 全部账号/);
  assert.doesNotMatch(html,/可调度 \/ 全部账号/);
  assert.match(html,/id="statSessionHealth"/);
});
test('Labs retains its countdown and Flow keeps the verify action',()=>{
  assert.equal(sandbox.formatTokenExpiry({auth_mode:'labs',at_expires:future.flow_cookie_expires_at}),sandbox.formatExpiry(future.flow_cookie_expires_at));
  assert.match(sandbox.renderTokenActions(future),/验证会话/);
  assert.doesNotMatch(sandbox.renderTokenActions(future),/>刷新AT</);
});
test('validation toast does not claim credential renewal',async()=>{
  const messages=[];
  const context={allTokens:[future],showToast:message=>messages.push(message),apiRequest:async()=>({json:async()=>({success:true,token:future})}),refreshTokens:async()=>{}};
  vm.runInNewContext('const '+definition('refreshTokenAT','refreshTokens')+';this.refreshTokenAT=refreshTokenAT;',context);
  await context.refreshTokenAT(1);
  assert.equal(messages.at(-1),'Flow 新站会话验证成功');
});
