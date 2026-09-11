const {readFileSync} = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');
const html = readFileSync(path.join(__dirname, '../static/manage.html'), 'utf8');
function definition(first, next) {
  return html.slice(html.indexOf(`        ${first}=`), html.indexOf(`        ${next}=`)).trim().replace(/,$/, '');
}
const sandbox = {Date};
vm.runInNewContext(`const ${definition('formatExpiry', 'formatPlanType')}; const ${definition('renderTokenActions', 'renderTokens')}; this.formatTokenExpiry=formatTokenExpiry; this.formatExpiry=formatExpiry; this.renderTokenActions=renderTokenActions;`, sandbox);
const future = {id:1, auth_mode:'flow', flow_cookie_expires_at:'2100-01-01T00:00:00Z', flow_cookie_expiry_status:'known'};

test('new Flow expiry shows an exact date and Cookie source, never old AT', () => {
  const rendered = sandbox.formatTokenExpiry({...future, at_expires:'2000-01-01T00:00:00Z'});
  assert.match(rendered, /2100-/);
  assert.match(rendered, /Flow Cookie/);
  assert.match(rendered, /不代表会话一定有效/);
  assert.doesNotMatch(rendered, /已过期/);
  assert.match(html, /const expiryDisplay=formatTokenExpiry\(t\)/);
});
test('expired cookies are red and labeled as Cookie expiry', () => {
  const rendered = sandbox.formatTokenExpiry({...future, flow_cookie_expires_at:'2000-01-01T00:00:00Z', flow_cookie_expiry_status:'expired'});
  assert.match(rendered, /text-red-600/);
  assert.match(rendered, /Flow Cookie · 已到期/);
  assert.match(rendered, /2000-/);
});
test('unknown or session expiry never falls back to legacy dates', () => {
  for(const [state,label] of [['session','会话 Cookie'],['invalid','到期未知'],['incomplete','登录凭据不完整'],['unavailable','未同步 Cookie'],[undefined,'到期未知']]) {
    const rendered=sandbox.formatTokenExpiry({...future, flow_cookie_expiry_status:state, flow_cookie_expires_at:null, at_expires:future.flow_cookie_expires_at});
    assert.ok(rendered.includes(label));
    assert.doesNotMatch(rendered, /<time|2100-/);
  }
  assert.match(sandbox.formatTokenExpiry({...future, flow_cookie_expires_at:'bad-date'}), /到期未知/);
});
test('mixed session credentials disclose the unknown lifetime component', () => {
  assert.match(sandbox.formatTokenExpiry({...future, flow_cookie_has_session_cookies:true}), /含会话项/);
});
test('Labs keeps the previous AT display and Flow gets a verify action', () => {
  assert.equal(sandbox.formatTokenExpiry({auth_mode:'labs', at_expires:future.flow_cookie_expires_at}), sandbox.formatExpiry(future.flow_cookie_expires_at));
  assert.match(sandbox.renderTokenActions(future), /验证会话/);
  assert.doesNotMatch(sandbox.renderTokenActions(future), />刷新AT</);
  assert.match(sandbox.renderTokenActions({id:2,auth_mode:'labs'}), />刷新AT</);
});
test('Flow validation toast does not claim to renew an AT or Cookie', async () => {
  const messages=[];
  const context={allTokens:[future],showToast:message=>messages.push(message),
    apiRequest:async()=>({json:async()=>({success:true,token:future})}),refreshTokens:async()=>{}};
  vm.runInNewContext(`const ${definition('refreshTokenAT','refreshTokens')}; this.refreshTokenAT=refreshTokenAT;`,context);
  await context.refreshTokenAT(1);
  assert.equal(messages.at(-1),'Flow 新站会话验证成功');
  assert.ok(messages.every(message=>!message.includes('AT')));
});
