// Kept readable because this export contains built frontend assets only.
let active = false;

export function confirmAccountCleanup({title, options, loadPreview, initialPreview}) {
  // A second click must not create a second deletion operation.
  if (active) return Promise.resolve(false);
  active = true;
  if (!document.querySelector('[data-account-cleanup-style]')) {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = new URL('./accountCleanupPreview-v1.css', import.meta.url).href;
    link.dataset.accountCleanupStyle = '';
    document.head.append(link);
  }
  const previousFocus = document.activeElement;
  const dialog = document.createElement('dialog');
  dialog.className = 'account-cleanup-preview';
  dialog.setAttribute('aria-labelledby', 'account-cleanup-title');
  // Only static markup: all API values below are inserted with textContent.
  dialog.innerHTML = `
    <header><div><h2 id="account-cleanup-title"></h2><p>删除前核对账号与额度</p></div><button type="button" data-close aria-label="关闭预览">×</button></header>
    <div class="cleanup-body">
      <p data-summary role="status" aria-live="polite">正在读取账号状态...</p>
      <p class="cleanup-warning">删除不可恢复。确认会清理所有符合当前条件的账号，不仅是本页；执行时会重新检查状态。未知额度不等于没有额度。</p>
      <p data-error role="alert" hidden></p>
      <div class="cleanup-table-scroll"><table><thead><tr><th>账号</th><th>状态</th><th>清理原因</th><th>剩余额度</th></tr></thead><tbody></tbody></table></div>
      <div class="cleanup-paging"><span data-page></span><div><button type="button" data-previous>上一页</button><button type="button" data-next>下一页</button><button type="button" data-refresh>刷新预览</button></div></div>
    </div>
    <footer><button type="button" data-cancel autofocus>取消</button><button type="button" class="cleanup-confirm" data-confirm disabled>确认删除</button></footer>`;
  const find = selector => dialog.querySelector(selector);
  find('h2').textContent = title;
  let done = false, busy = false, offset = 0, count = 0, hasMore = false, serial = 0;
  const pageSize = 50;
  const setDisabled = () => {
    find('[data-confirm]').disabled = busy || count <= 0 || !find('[data-error]').hidden;
    find('[data-previous]').disabled = busy || offset <= 0;
    find('[data-next]').disabled = busy || !hasMore;
    find('[data-refresh]').disabled = busy;
  };
  const render = preview => {
    if (!Array.isArray(preview.items)) throw new Error('服务尚未更新到支持账号预览的版本，请先更新服务。');
    count = Math.max(0, Number(preview.total_removed) || 0);
    offset = Math.max(0, Number(preview.offset) || 0);
    hasMore = preview.has_more === true;
    find('[data-summary]').textContent = count ? `符合条件：${count} 个账号` : '当前没有符合删除条件的账号';
    find('[data-confirm]').textContent = `确认删除全部 ${count} 个`;
    find('[data-page]').textContent = count ? `第 ${Math.floor(offset / pageSize) + 1} 页 · 每页 ${pageSize} 个` : '暂无账号';
    const rows = preview.items.map(account => {
      const row = document.createElement('tr');
      for (const value of [account.email || account.id || '未命名账号',
        account.status_label || account.status || '未知',
        account.cleanup_reason || account.status_label || account.status || '未知',
        account.quota_label ?? (account.quota_unknown ? '未知' : String(account.quota ?? 0))]) {
        const cell = document.createElement('td');
        cell.textContent = String(value);
        row.append(cell);
      }
      return row;
    });
    find('tbody').replaceChildren(...rows);
    find('.cleanup-table-scroll').scrollTop = 0;
  };
  async function load(nextOffset, initial) {
    const current = ++serial;
    busy = true;
    find('[data-error]').hidden = true;
    find('[data-summary]').textContent = '正在读取账号状态...';
    setDisabled();
    try {
      const preview = initial || await loadPreview({...options, preview_limit: pageSize, preview_offset: nextOffset});
      if (done || current !== serial) return;
      render(preview);
    } catch (error) {
      if (done || current !== serial) return;
      find('[data-summary]').textContent = '预览未完成，暂不能删除';
      find('[data-error]').textContent = String(error?.message || '读取失败，请重试');
      find('[data-error]').hidden = false;
    } finally {
      if (!done && current === serial) { busy = false; setDisabled(); }
    }
  }
  return new Promise(resolve => {
    function finish(confirmed) {
      if (done) return;
      done = true;
      serial++;
      dialog.close();
      dialog.remove();
      window.removeEventListener('hashchange', cancel);
      active = false;
      if (previousFocus?.isConnected) previousFocus.focus();
      resolve(confirmed);
    }
    const cancel = () => finish(false);
    for (const selector of ['[data-close]', '[data-cancel]']) find(selector).addEventListener('click', cancel);
    dialog.addEventListener('cancel', event => {event.preventDefault(); cancel();});
    find('[data-confirm]').addEventListener('click', () => {
      if (!find('[data-confirm]').disabled) finish(true);
    });
    find('[data-previous]').addEventListener('click', () => load(Math.max(0, offset - pageSize)));
    find('[data-next]').addEventListener('click', () => load(offset + pageSize));
    find('[data-refresh]').addEventListener('click', () => load(offset));
    window.addEventListener('hashchange', cancel);
    document.body.append(dialog);
    dialog.showModal();
    load(0, initialPreview);
  });
}
