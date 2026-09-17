/* The only account state is Flow2API's existing token/session API. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let accounts = [], session = null, working = false, available = false, shownSession = null;
  let selected = new URLSearchParams(location.search).get('token_id');
  const labels = {verified:'验证通过', refresh_required:'需重新登录', verification_failed:'检测失败',
    checking:'正在检测', stale:'需重新检测', unknown:'尚未检测'};
  function message(text, error = false) {
    $('message').textContent = text;
    $('message').dataset.error = String(error);
  }
  async function api(path, body) {
    const credential = localStorage.getItem('adminToken');
    if (!credential) { location.href = '/login'; throw new Error('请先登录控制台'); }
    const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST',
      headers: {'Authorization': `Bearer ${credential}`, 'Content-Type':'application/json'},
      ...(body === undefined ? {} : {body: JSON.stringify(body)}), cache:'no-store'});
    if (response.status === 401) {
      hideViewer(); localStorage.removeItem('adminToken'); location.href = '/login';
      throw new Error('控制台登录已过期');
    }
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请检查输入或稍后重试');
    return data;
  }
  function hideViewer() {
    $('viewer').hidden = true; $('viewer').removeAttribute('src'); $('empty').hidden = false;
    shownSession = null;
  }
  function render() {
    const account = accounts.find(t => String(t.id) === $('account').value);
    $('accountState').textContent = account ?
      `${account.session_owner === 'local' ? '服务器统一管理' : '导入账号 · 首次打开后转为服务器管理'}\n${labels[account.session_status] || '尚未检测'} · ${account.is_active ? '已启用' : '暂停接单'}${account.session_checked_at ? '\n最近检测：' + new Date(account.session_checked_at).toLocaleString() : ''}` : '请选择或新增账号';
    const own = session && session.owned;
    $('phase').textContent = session ?
      `${{starting:'启动中',login:'等待人工登录',verifying:'正在保存并验证',closing:'正在关闭'}[session.phase] || session.phase} · 剩余 ${Math.ceil(session.remaining_seconds / 60)} 分钟` : '未打开';
    const activeAccount = session && accounts.find(t => t.id === session.token_id);
    $('title').textContent = activeAccount ? activeAccount.email : '远程登录工作台';
    $('account').disabled = working || !!session;
    $('start').disabled = working || !available || !account || !!session;
    $('create').disabled = working || !!session;
    $('finish').disabled = working || !own || session.phase !== 'login';
    $('cancel').disabled = working || !own || session.phase !== 'login';
    $('resume').hidden = !own || session.phase !== 'login' || shownSession === session.session_id;
    $('resume').disabled = working;
    if (!session || session.session_id !== shownSession || session.phase !== 'login') hideViewer();
  }
  async function refresh() {
    const [state, tokens] = await Promise.all([api('/api/account-login'), api('/api/tokens')]);
    accounts = tokens;
    const value = selected || $('account').value;
    $('account').replaceChildren(...tokens.map(token => {
      const option = document.createElement('option'); option.value = token.id; option.textContent = token.email; return option;
    }));
    if (tokens.some(t => String(t.id) === String(value))) $('account').value = value;
    selected = null; available = state.available; session = state.active;
    if (session) $('account').value = session.token_id;
    render();
  }
  async function action(fn) {
    if (working) return;
    working = true; render();
    try { await fn(); }
    catch (error) { message(error.message, true); }
    finally {
      try { await refresh(); } catch (error) { message(error.message, true); }
      working = false; render();
    }
  }
  async function showViewer() {
    const data = await api(`/api/account-login/${session.session_id}/viewer`, {});
    // The backend returns a relative same-origin path, never a raw :6080 URL.
    if (data.url !== '/account-login/viewer') throw new Error('登录入口无效');
    shownSession = session.session_id;
    $('viewer').src = data.url; $('viewer').hidden = false; $('empty').hidden = true;
  }
  $('account').addEventListener('change', render);
  $('start').addEventListener('click', () => action(async () => {
    if (!confirm('此账号将转为服务器统一管理并暂停接单。完成登录和验证后恢复；外部插件或 tupdater 不能再覆盖其会话。继续？')) return;
    message('正在保存原浏览器并启动独立登录画面…');
    const data = await api(`/api/accounts/${$('account').value}/login`, {});
    session = data.active; await showViewer();
    message('请在画面中登录所选邮箱，进入 flow.google.com 后点击“完成登录并验证”。');
  }));
  $('resume').addEventListener('click', () => action(showViewer));
  $('finish').addEventListener('click', () => action(async () => {
    hideViewer(); message('正在保存 Profile 并在生成浏览器中验证身份、额度和项目接口…');
    const data = await api(`/api/account-login/${session.session_id}/finish`, {});
    message(`验证通过，账号已恢复接单。当前额度：${data.credits}。后续检测与生成共用此 Profile。`);
  }));
  $('cancel').addEventListener('click', () => action(async () => {
    await api(`/api/account-login/${session.session_id}/cancel`, {}); hideViewer();
    message('登录窗口已结束，Profile 已保留，账号保持暂停。重新打开并验证后才能恢复接单。');
  }));
  $('createForm').addEventListener('submit', event => {
    event.preventDefault();
    action(async () => {
      const data = await api('/api/accounts', {email: $('email').value.trim(),
        captcha_proxy_url: $('proxy').value.trim(), remark: $('remark').value.trim()});
      selected = String(data.token_id); $('createForm').reset(); $('newAccount').open = false;
      message('账号已创建，尚未启用。请打开登录窗口完成认证。');
    });
  });
  refresh().then(() => message(available ? '选择账号后打开登录窗口，或继续当前窗口。' : '此环境未启用登录桌面；请部署新版 headed 镜像并选择 native_cdp。'))
    .catch(error => message(error.message, true));
  setInterval(() => { if (!working) refresh().catch(error => message(error.message, true)); }, 5000);
})();
